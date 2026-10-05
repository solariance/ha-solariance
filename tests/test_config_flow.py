"""Setup, re-authentication and options."""
from __future__ import annotations

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.solariance.const import (
    API_BASE,
    CONF_API_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_SYSTEM_ID,
    DOMAIN,
)

from .conftest import SYSTEM_ID, SYSTEMS, TOKEN, envelope

LIST_URL = f"{API_BASE}/system/list"


async def _start(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})


async def test_one_system_is_set_up_without_a_choice(hass, aioclient_mock):
    aioclient_mock.get(LIST_URL, json=envelope({"systems": SYSTEMS[:1]}))
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: f"  {TOKEN}  "})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Roof"
    assert result["data"] == {CONF_API_TOKEN: TOKEN, CONF_SYSTEM_ID: SYSTEM_ID}
    assert result["result"].unique_id == SYSTEM_ID
    # The token travels as a Bearer token: without the prefix the API reads
    # it as a session token and answers 401.
    _method, _url, _data, headers = aioclient_mock.mock_calls[0]
    assert headers["Authorization"] == f"Bearer {TOKEN}"


async def test_a_token_pasted_with_bearer_is_stored_without_it(hass, aioclient_mock):
    """The website's REST guide puts "Bearer <token>" into secrets.yaml; a
    token copied from there must not go out as "Bearer Bearer ..."."""
    aioclient_mock.get(LIST_URL, json=envelope({"systems": SYSTEMS[:1]}))
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: f"Bearer {TOKEN}"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_API_TOKEN] == TOKEN
    _method, _url, _data, headers = aioclient_mock.mock_calls[0]
    assert headers["Authorization"] == f"Bearer {TOKEN}"


async def test_several_systems_offer_a_choice(hass, aioclient_mock):
    aioclient_mock.get(LIST_URL, json=envelope({"systems": SYSTEMS}))
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: TOKEN})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "system"
    options = result["data_schema"].schema[CONF_SYSTEM_ID].config["options"]
    assert [o["value"] for o in options] == [SYSTEM_ID, "PVTEST000002"]
    assert options[0]["label"] == "Roof (Berlin, 9.8 kWp)"
    assert "paused" in options[1]["label"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SYSTEM_ID: "PVTEST000002"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Garage"


async def test_configured_systems_are_not_offered_again(hass, aioclient_mock, config_entry):
    config_entry.add_to_hass(hass)
    aioclient_mock.get(LIST_URL, json=envelope({"systems": SYSTEMS}))
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: TOKEN})
    # Only the garage is left, so it is taken without asking.
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SYSTEM_ID] == "PVTEST000002"


async def test_every_system_configured_aborts(hass, aioclient_mock, config_entry):
    config_entry.add_to_hass(hass)
    aioclient_mock.get(LIST_URL, json=envelope({"systems": SYSTEMS[:1]}))
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: TOKEN})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_form_errors(hass, aioclient_mock):
    cases = (
        ({"status": 401}, "invalid_auth"),
        ({"status": 403}, "invalid_auth"),
        ({"status": 429, "headers": {"Retry-After": "3600"}}, "rate_limited"),
        ({"status": 500}, "cannot_connect"),
        ({"exc": TimeoutError()}, "cannot_connect"),
        ({"json": envelope({"systems": []})}, "no_systems"),
    )
    for answer, error in cases:
        aioclient_mock.clear_requests()
        aioclient_mock.get(LIST_URL, **answer)
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_TOKEN: TOKEN})
        assert result["type"] is FlowResultType.FORM, error
        assert result["errors"] == {"base": error}

    # And it recovers once the token works.
    aioclient_mock.clear_requests()
    aioclient_mock.get(LIST_URL, json=envelope({"systems": SYSTEMS[:1]}))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: TOKEN})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_reauth_stores_the_new_token(hass, aioclient_mock, config_entry):
    config_entry.add_to_hass(hass)
    aioclient_mock.get(LIST_URL, json=envelope({"systems": SYSTEMS}))
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: "new-token"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_API_TOKEN] == "new-token"


async def test_reauth_of_a_loaded_entry_reloads_without_a_deprecation(
        hass, aioclient_mock, setup_entry, caplog):
    """Home Assistant reloads the entry after re-authentication itself; with
    an update listener registered it logs that this breaks in 2026.12."""
    aioclient_mock.get(LIST_URL, json=envelope({"systems": SYSTEMS}))
    result = await setup_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: "new-token"})
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert setup_entry.state is config_entries.ConfigEntryState.LOADED
    assert setup_entry.data[CONF_API_TOKEN] == "new-token"
    assert "update listener" not in caplog.text


async def test_reauth_refuses_a_token_of_another_account(hass, aioclient_mock, config_entry):
    config_entry.add_to_hass(hass)
    aioclient_mock.get(LIST_URL, json=envelope({"systems": SYSTEMS[1:]}))
    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: "someone-elses-token"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "system_not_in_account"}
    assert config_entry.data[CONF_API_TOKEN] == TOKEN


async def test_options_set_the_polling_interval(hass, setup_entry):
    result = await hass.config_entries.options.async_init(setup_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: "60"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert setup_entry.options == {CONF_SCAN_INTERVAL: 60}
    await hass.async_block_till_done()
    assert setup_entry.runtime_data.update_interval.total_seconds() == 3600
