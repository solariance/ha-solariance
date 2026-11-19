"""A small async client for the Solariance REST API.

Only what the integration needs: the account's systems (to set it up), the
power forecast of one system (to run it) and, since 0.3, the upload of the
measured production (POST measured/production/upload, source "ha_sensor":
stored fill-only, never replacing what the owner uploaded) and the upload of
an irradiance sensor (POST measured/irradiance/upload, the same
fill-only source) with the system's roof (GET system/view) to prefill the
sensor's position. The API is documented at https://developer.solariance.de.

AUTH. A personal API token, created at solariance.de/user/entwickler, is sent
as `Authorization: Bearer <token>`. Without the "Bearer " prefix the API reads
it as a session token and answers 401. A read-only token is enough for the
forecast; the uploads need the Upload permission ("Read and upload measured
data" on the account page) or ReadWrite. A read-only token's POST is refused
with a 403 that names no reason.

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
_USER_AGENT = "HomeAssistant-Solariance/0.3.1"
STATUS_HEADER = "X-Solariance-Forecast-Status"
ETA_HEADER = "X-Solariance-Forecast-Eta"


class SolarianceError(Exception):
    """Base error of the client."""


class SolarianceAuthError(SolarianceError):
    """The token was refused (revoked, expired or mistyped)."""


class SolarianceConnectionError(SolarianceError):
    """The API could not be reached or answered with a server error."""


class SolarianceUnavailableError(SolarianceConnectionError):
    """A server error whose body names a reason (0.3): a 503
    "irradiance_uploads_off" is a switch on Solariance's side, not an outage."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(f"HTTP {status} ({reason})")
        self.status = status
        self.reason = reason


class SolarianceRefusedError(SolarianceError):
    """A 403: the account or its plan refuses this request. Not the token's
    fault -- a new one would be refused the same. `feature` is the feature a
    "feature_required" names (0.3.1): "measured_data" when the plan has no
    measured data at all, "ai_irradiance" when it lacks the irradiance upload."""

    def __init__(self, reason: str | None, feature: str | None = None) -> None:
        detail = f"{reason}: {feature}" if reason and feature else reason or "no reason given"
        super().__init__(f"refused ({detail})")
        self.reason = reason
        self.feature = feature


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


class SolarianceRequestError(SolarianceError):
    """A 4xx other than 401/403/429: the request itself was refused (a bad
    payload); data.reason names it."""

    def __init__(self, status: int, reason: str | None) -> None:
        super().__init__(f"HTTP {status} ({reason or 'no reason given'})")
        self.status = status
        self.reason = reason


@dataclass(frozen=True)
class UploadAnswer:
    """POST measured/production/upload: what was stored. A re-report of hours
    already stored stores nothing, which is the normal case."""

    stored: int
    rejected: int


@dataclass(frozen=True)
class IrradianceAnswer:
    """POST measured/irradiance/upload: the totals, and what the answer says
    about the one series sent (its status, a reason, warnings)."""

    stored: int
    kept: int
    replaced: int
    rejected: int
    status: str | None = None
    reason: str | None = None
    warnings: tuple[str, ...] = ()


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

    async def async_upload_measured(self, payload: dict[str, Any]) -> UploadAnswer:
        """Send measured production (the contract's JSON). A read-only token
        is refused with a 403 that names no reason."""
        data = await self._post("measured/production/upload", payload)
        totals = data.get("totals") if isinstance(data, dict) else None
        totals = totals if isinstance(totals, dict) else {}
        return UploadAnswer(stored=int(totals.get("stored") or 0), rejected=int(totals.get("rejected") or 0))

    async def async_upload_irradiance(self, payload: dict[str, Any]) -> IrradianceAnswer:
        """Send an irradiance sensor's values (0.3, the contract's JSON). Beyond
        the production upload's refusals: 503 "irradiance_uploads_off" while
        Solariance has the route switched off, 403 "feature_required" without
        the account's feature, 409 "sensor_changed" when the sensor id already
        holds values with another position."""
        data = await self._post("measured/irradiance/upload", payload)
        data = data if isinstance(data, dict) else {}
        totals = data.get("totals") if isinstance(data.get("totals"), dict) else {}
        series = data.get("series") if isinstance(data.get("series"), list) else []
        first = series[0] if series and isinstance(series[0], dict) else {}
        warnings = first.get("warnings") if isinstance(first.get("warnings"), list) else []
        return IrradianceAnswer(
            stored=_count(totals.get("stored")), kept=_count(totals.get("kept")),
            replaced=_count(totals.get("replaced")), rejected=_count(totals.get("rejected")),
            status=first.get("status") if isinstance(first.get("status"), str) else None,
            reason=first.get("reason") if isinstance(first.get("reason"), str) else None,
            warnings=tuple(str(w) for w in warnings[:5]))

    async def async_get_system(self, system_id: str) -> dict[str, Any]:
        """GET system/view: one system as stored, its roof faces (`modules`)
        included. Only read to prefill an irradiance sensor's position."""
        data = await self._get("system/view", {"system_id": system_id})
        return data if isinstance(data, dict) else {}

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        """The `data` of a POST with a JSON body."""
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        }
        try:
            async with self._session.post(f"{self._base}/{path}", json=payload, headers=headers,
                                          timeout=_TIMEOUT) as resp:
                if resp.status == 401:
                    raise SolarianceAuthError(f"{path}: HTTP 401")
                if resp.status == 403:
                    raise await _refusal(resp)
                if resp.status == 429:
                    raise SolarianceRateLimitError(_retry_after(resp.headers.get("Retry-After")))
                if 400 <= resp.status < 500:
                    raise SolarianceRequestError(resp.status, await _reason(resp))
                if resp.status >= 500:
                    reason = await _reason(resp)
                    if reason:
                        raise SolarianceUnavailableError(resp.status, reason)
                    raise SolarianceConnectionError(f"{path}: HTTP {resp.status}")
                try:
                    body = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError) as err:
                    raise SolarianceConnectionError(f"{path}: not JSON") from err
        except (TimeoutError, aiohttp.ClientError) as err:
            raise SolarianceConnectionError(f"{path}: {err}") from err
        return body.get("data") if isinstance(body, dict) else None


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
    feature = data.get("feature") if isinstance(data, dict) else None
    return SolarianceRefusedError(reason if isinstance(reason, str) else None,
                                  feature if isinstance(feature, str) and feature else None)


async def _reason(resp: aiohttp.ClientResponse) -> str | None:
    try:
        body = await resp.json(content_type=None)
    except (aiohttp.ContentTypeError, ValueError):
        return None
    data = body.get("data") if isinstance(body, dict) else None
    reason = data.get("reason") if isinstance(data, dict) else None
    return reason if isinstance(reason, str) else None


def _count(value: Any) -> int:
    """A count from an answer; 0 when it is missing or not a number."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _retry_after(value: str | None) -> float | None:
    """Retry-After in seconds; None when absent or not a finite number (an
    HTTP date, "nan", ...)."""
    try:
        seconds = float(value) if value is not None else None
    except ValueError:
        return None
    return max(seconds, 0.0) if seconds is not None and math.isfinite(seconds) else None
