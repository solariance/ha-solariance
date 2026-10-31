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

from typing import Any

import aiohttp

from .const import API_BASE

_TIMEOUT = aiohttp.ClientTimeout(total=30)
_USER_AGENT = "HomeAssistant-Solariance/0.1.2"


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


class SolarianceApiClient:
    """One account's access to the API."""

    def __init__(self, session: aiohttp.ClientSession, token: str,
                 base_url: str = API_BASE) -> None:
        self._session = session
        self._token = normalise_token(token)
        self._base = base_url.rstrip("/")

    async def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        """The `data` of a GET, or None for a 204."""
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        }
        try:
            async with self._session.get(f"{self._base}/{path}", params=params,
                                         headers=headers, timeout=_TIMEOUT) as resp:
                if resp.status == 204:
                    return None
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
        return body.get("data")

    async def async_list_systems(self) -> list[dict[str, Any]]:
        """The account's systems: system_id, name, city, country, kWp, ..."""
        data = await self._get("system/list")
        systems = (data or {}).get("systems") if isinstance(data, dict) else None
        return [s for s in systems or [] if isinstance(s, dict) and s.get("system_id")]

    async def async_get_power_forecast(self, system_id: str) -> dict[str, Any] | None:
        """The power forecast of one system for every day its plan covers,
        or None while none is computed yet.

        No `day` parameter on purpose: asking for days 0 and 1 by number turns
        the whole answer into a 204 when one of them is not computed yet.
        """
        data = await self._get("forecast/power", {"system_id": system_id})
        return data if isinstance(data, dict) else None


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
    try:
        return max(float(value), 0.0) if value is not None else None
    except ValueError:
        return None
