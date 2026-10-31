"""What the client makes of the API's refusals (0.1.2)."""
from __future__ import annotations

import pytest

from custom_components.solariance.api import (
    SolarianceApiClient,
    SolarianceAuthError,
    SolarianceRefusedError,
    SolarianceSystemGoneError,
    SolarianceSystemPausedError,
)
from custom_components.solariance.const import API_BASE

URL = f"{API_BASE}/forecast/power"


@pytest.mark.parametrize(
    ("status", "body", "error", "reason"),
    [
        (401, None, SolarianceAuthError, None),
        (403, {"data": {"reason": "system_locked", "locked_reason": "kwp_limit"}},
         SolarianceSystemPausedError, "kwp_limit"),
        (403, {"data": {"reason": "not_owner"}}, SolarianceSystemGoneError, "not_owner"),
        (403, {"data": {"reason": "email_not_verified"}}, SolarianceRefusedError,
         "email_not_verified"),
        (403, {"message": "API token not permitted for this endpoint"},
         SolarianceRefusedError, None),
        (403, None, SolarianceRefusedError, None),
    ],
)
async def test_only_a_401_is_the_tokens_fault(hass, aioclient_mock, status, body, error, reason):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    if body is None:
        aioclient_mock.get(URL, status=status, text="")
    else:
        aioclient_mock.get(URL, status=status, json=body)
    client = SolarianceApiClient(async_get_clientsession(hass), "token")
    with pytest.raises(error) as raised:
        await client.async_get_power_forecast("PV1")
    assert type(raised.value) is error
    if error is not SolarianceAuthError:
        assert not isinstance(raised.value, SolarianceAuthError)
        assert raised.value.reason == reason
