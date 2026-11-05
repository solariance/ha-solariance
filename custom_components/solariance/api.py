"""A small async client for the Solariance REST API.

Only what the integration needs: the account's systems (to set it up) and the
power forecast of one system (to run it). The API is documented at
https://developer.solariance.de.

AUTH. A personal API token, created at solariance.de/user, is sent as
`Authorization: Bearer <token>`. Without the "Bearer " prefix the API reads it
as a session token and answers 401. A read-only token is enough.

ANSWERS. Every answer is the envelope {"code", "message", "data"}. GET
forecast/power answers 204 while the system has no forecast yet (a new system,
or the first model run of the day not computed), which is not an error.

FORECAST STATUS (API 2026-11, read since 0.1.3; every part optional, an older
API sends none of it). The 204 carries `X-Solariance-Forecast-Status` (queued,
delayed, failed, stale), `Retry-After` (seconds until asking again makes sense)
and `X-Solariance-Forecast-Eta` (ISO UTC, when the forecast is expected). The
200 carries the status header too (provisional, ready), and its body
`generated_utc` and `forecast_status`.

REFUSALS. Only a 401 says the token is no good. A 403 is the account or the
plan refusing this request, and a new token would be refused the same, so it
never starts a re-authentication (until 0.1.1 every unexplained 403 did, and
Home Assistant stopped polling until someone entered a token that could not
help). data.reason names it: "system_locked" -- the plan has paused the
system; "not_owner" -- the system is not in the token's account any more;
anything else is a refusal the integration reports and asks about again
later.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiohttp

from .const import API_BASE
from .forecast import parse_status, parse_utc

_TIMEOUT = aiohttp.ClientTimeout(total=30)
_USER_AGENT = "HomeAssistant-Solariance/0.1.3"
STATUS_HEADER = "X-Solariance-Forecast-Status"
ETA_HEADER = "X-Solariance-Forecast-Eta"


class SolarianceError(Exception):
    """Base error of the client."""


class SolarianceAuthError(SolarianceError):
    """The token was refused (revoked, expired or mistyped)."""


class SolarianceConnectionError(SolarianceError):
    """The API could not be reached or answered with a server error."""


class SolarianceRefusedError(SolarianceError):
    """A 403: the account or its plan refuses this request. Not the token's
    fault -- a new one would be refused the same."""

    def __init__(self, reason: str | None) -> None:
        super().__init__(f"refused ({reason or 'no reason given'})")
        self.reason = reason


class SolarianceSystemGoneError(SolarianceRefusedError):
    """403 "not_owner": the system is not in the token's account any more --
    deleted, or the token belongs to another account."""


class SolarianceSystemPausedError(SolarianceError):
    """The account's plan has paused this system (403 "system_locked")."""

    def __init__(self, reason: str | None) -> None:
        super().__init__(f"system paused by the plan ({reason})")
        self.reason = reason


class SolarianceRateLimitError(SolarianceError):
    """The account's hourly request limit is spent."""

    def __init__(self, retry_after: float | None) -> None:
        super().__init__(f"rate limited, retry after {retry_after} s")
        self.retry_after = retry_after


@dataclass(frozen=True)
class ForecastAnswer:
    """GET forecast/power: the forecast, or (data None) why there is none yet.
    The status fields stay None when the API does not send them."""

    data: dict[str, Any] | None
    status: str | None = None          # X-Solariance-Forecast-Status
    retry_after: float | None = None   # Retry-After of a 204, seconds
    eta: datetime | None = None        # X-Solariance-Forecast-Eta of a 204, UTC


class SolarianceApiClient:
    """One account's access to the API."""

    def __init__(self, session: aiohttp.ClientSession, token: str,
                 base_url: str = API_BASE) -> None:
        self._session = session
        self._token = normalise_token(token)
        self._base = base_url.rstrip("/")

    async def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        """The `data` of a GET, or None for a 204."""
        data, _headers = await self._fetch(path, params)
        return data

    async def _fetch(self, path: str,
                     params: dict[str, str] | None = None) -> tuple[Any, Mapping[str, str]]:
        """The `data` of a GET (None for a 204) and the answer's headers."""
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        }
        try:
            async with self._session.get(f"{self._base}/{path}", params=params,
                                         headers=headers, timeout=_TIMEOUT) as resp:
                answer_headers = resp.headers.copy()
                if resp.status == 204:
                    return None, answer_headers
                if resp.status == 401:
                    raise SolarianceAuthError(f"{path}: HTTP 401")
                if resp.status == 403:
                    raise await _refusal(resp)
                if resp.status == 429:
                    raise SolarianceRateLimitError(_retry_after(resp.headers.get("Retry-After")))
                if resp.status >= 400:
                    raise SolarianceConnectionError(f"{path}: HTTP {resp.status}")
                try:
                    body = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError) as err:
                    raise SolarianceConnectionError(f"{path}: not JSON") from err
        except (TimeoutError, aiohttp.ClientError) as err:
            raise SolarianceConnectionError(f"{path}: {err}") from err
        if not isinstance(body, dict):
            raise SolarianceConnectionError(f"{path}: unexpected answer")
        return body.get("data"), answer_headers

    async def async_list_systems(self) -> list[dict[str, Any]]:
        """The account's systems: system_id, name, city, country, kWp, ..."""
        data = await self._get("system/list")
        systems = (data or {}).get("systems") if isinstance(data, dict) else None
        return [s for s in systems or [] if isinstance(s, dict) and s.get("system_id")]

    async def async_get_power_forecast(self, system_id: str) -> ForecastAnswer:
        """The power forecast of one system for every day its plan covers
        (data None while none is computed yet), with what the API says about
        its status.

        No `day` parameter on purpose: asking for days 0 and 1 by number turns
        the whole answer into a 204 when one of them is not computed yet.
        """
        data, headers = await self._fetch("forecast/power", {"system_id": system_id})
        status = parse_status(headers.get(STATUS_HEADER))
        if isinstance(data, dict):
            return ForecastAnswer(data=data, status=status)
        return ForecastAnswer(data=None, status=status,
                              retry_after=_retry_after(headers.get("Retry-After")),
                              eta=parse_utc(headers.get(ETA_HEADER)))


def normalise_token(token: str) -> str:
    """The token as solariance.de/user issued it. Pasted from a secrets.yaml
    line (the website's REST guide) it can carry "Bearer " in front, which
    would otherwise go out as "Bearer Bearer ..." and be refused."""
    token = token.strip()
    if token[:7].lower() == "bearer ":
        token = token[7:].strip()
    return token


async def _refusal(resp: aiohttp.ClientResponse) -> SolarianceError:
    """What a 403 says, as the error to raise: a paused system, a system
    gone from the account, or any other refusal (with its reason, when the
    body names one)."""
    try:
        body = await resp.json(content_type=None)
    except (aiohttp.ContentTypeError, ValueError):
        body = None
    data = body.get("data") if isinstance(body, dict) else None
    reason = data.get("reason") if isinstance(data, dict) else None
    if reason == "system_locked":
        return SolarianceSystemPausedError(str(data.get("locked_reason") or "") or None)
    if reason == "not_owner":
        return SolarianceSystemGoneError(reason)
    return SolarianceRefusedError(reason if isinstance(reason, str) else None)


def _retry_after(value: str | None) -> float | None:
    """Retry-After in seconds; None when absent or not a finite number (an
    HTTP date, "nan", ...)."""
    try:
        seconds = float(value) if value is not None else None
    except ValueError:
        return None
    return max(seconds, 0.0) if seconds is not None and math.isfinite(seconds) else None
