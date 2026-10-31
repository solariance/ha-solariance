"""Entry setup, the API's error answers, and unloading."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.solariance.const import API_BASE

from .conftest import NOW, SYSTEM_ID, envelope, make_payload

FORECAST_URL = f"{API_BASE}/forecast/power"


async def test_setup_and_unload(hass, setup_entry, mock_forecast):
    assert setup_entry.state is ConfigEntryState.LOADED
    _method, url, _data, headers = mock_forecast.mock_calls[0]
    # No `day` parameter: asking for days by number turns a missing day into a 204.
    assert dict(url.query) == {"system_id": SYSTEM_ID}
    assert headers["Authorization"].startswith("Bearer ")
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    assert setup_entry.state is ConfigEntryState.NOT_LOADED


ENTITY = "sensor.roof_estimated_energy_production_today"
PAUSED = {"code": 403, "message": "This system is paused: your plan covers fewer systems "
          "than this account has.", "data": {"reason": "system_locked",
                                              "locked_reason": "system_limit",
                                              "system_id": SYSTEM_ID}}


async def _fire(hass, freezer, delta):
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def _setup_answering(hass, freezer, aioclient_mock, config_entry, **answer):
    freezer.move_to(NOW)
    aioclient_mock.get(FORECAST_URL, **answer)
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_no_forecast_yet_loads_and_fetches_again_later(
        hass, freezer, aioclient_mock, config_entry):
    await _setup_answering(hass, freezer, aioclient_mock, config_entry, status=204)
    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(ENTITY).state == "unavailable"

    aioclient_mock.clear_requests()
    aioclient_mock.get(FORECAST_URL, json=envelope(make_payload()))
    await _fire(hass, freezer, timedelta(minutes=31))
    assert hass.states.get(ENTITY).state != "unavailable"


async def test_a_spent_limit_at_setup_does_not_retry_setup(
        hass, freezer, aioclient_mock, config_entry):
    """Home Assistant would retry setup after 5, 10, 20 ... seconds. Every
    refused call counts against the limit the Solariance app shares, so the
    entry loads without data and asks again only at the next update."""
    await _setup_answering(hass, freezer, aioclient_mock, config_entry,
                           status=429, headers={"Retry-After": "3600"})
    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(ENTITY).state == "unavailable"
    assert aioclient_mock.call_count == 1

    for _ in range(20):  # 20 minutes, a tick a minute
        await _fire(hass, freezer, timedelta(minutes=1))
    assert aioclient_mock.call_count == 1


async def test_a_paused_system_is_not_a_token_problem(
        hass, freezer, aioclient_mock, config_entry):
    await _setup_answering(hass, freezer, aioclient_mock, config_entry,
                           status=403, json=PAUSED)
    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(ENTITY).state == "unavailable"
    # No "enter a new token" flow: a new token would be paused just the same.
    assert hass.config_entries.flow.async_progress() == []


async def test_a_system_paused_later_waits_an_hour_without_reauth(
        hass, freezer, setup_entry, aioclient_mock):
    aioclient_mock.clear_requests()
    aioclient_mock.get(FORECAST_URL, status=403, json=PAUSED)
    await _fire(hass, freezer, timedelta(minutes=31))
    assert hass.states.get(ENTITY).state == "unavailable"
    assert hass.config_entries.flow.async_progress() == []
    calls = aioclient_mock.call_count

    await _fire(hass, freezer, timedelta(minutes=45))  # less than the hour it waits
    assert aioclient_mock.call_count == calls


async def test_a_server_error_at_setup_keeps_the_quick_retry(
        hass, freezer, aioclient_mock, config_entry):
    await _setup_answering(hass, freezer, aioclient_mock, config_entry, status=500)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_a_refused_token_starts_reauth(hass, freezer, aioclient_mock, config_entry):
    freezer.move_to(NOW)
    aioclient_mock.get(FORECAST_URL, status=401)
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_rate_limit_marks_entities_unavailable_until_it_recovers(
        hass, freezer, setup_entry, aioclient_mock):
    entity = "sensor.roof_estimated_energy_production_today"
    assert hass.states.get(entity).state != "unavailable"

    aioclient_mock.clear_requests()
    aioclient_mock.get(FORECAST_URL, status=429, headers={"Retry-After": "120"})
    freezer.tick(timedelta(minutes=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(entity).state == "unavailable"

    aioclient_mock.clear_requests()
    aioclient_mock.get(FORECAST_URL, json=envelope(make_payload()))
    freezer.tick(timedelta(minutes=3))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(entity).state != "unavailable"


# Only a 401 says the token is no good (0.1.2). Until 0.1.1 every unexplained
# 403 started a re-authentication, and Home Assistant stopped polling until
# someone entered a token that could not help.
NOT_OWNER = {"code": 403, "message": "Access denied", "data": {"reason": "not_owner"}}
UNVERIFIED = {"code": 403, "message": "Please verify your email address to use this feature.",
              "data": {"reason": "email_not_verified"}}


def _gone_issue(hass, config_entry):
    from homeassistant.helpers import issue_registry as ir

    from custom_components.solariance.const import DOMAIN
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"system_not_in_account_{config_entry.entry_id}")


async def test_a_system_gone_from_the_account_is_an_issue_not_a_reauth(
        hass, freezer, aioclient_mock, config_entry):
    await _setup_answering(hass, freezer, aioclient_mock, config_entry,
                           status=403, json=NOT_OWNER)
    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(ENTITY).state == "unavailable"
    assert hass.config_entries.flow.async_progress() == []
    issue = _gone_issue(hass, config_entry)
    assert issue is not None
    assert issue.translation_key == "system_not_in_account"
    assert issue.translation_placeholders == {"title": config_entry.title}

    # Home Assistant ignores a wait on the setup refresh, so the regular
    # update asks once more; from then on it is hourly, not every 30 minutes.
    await _fire(hass, freezer, timedelta(minutes=31))
    calls = aioclient_mock.call_count
    assert calls == 2
    assert hass.config_entries.flow.async_progress() == []
    await _fire(hass, freezer, timedelta(minutes=45))
    assert aioclient_mock.call_count == calls

    # ... and the issue closes once the system answers again.
    aioclient_mock.clear_requests()
    aioclient_mock.get(FORECAST_URL, json=envelope(make_payload()))
    await _fire(hass, freezer, timedelta(minutes=20))
    assert hass.states.get(ENTITY).state != "unavailable"
    assert _gone_issue(hass, config_entry) is None


async def test_removing_the_entry_removes_its_issue(
        hass, freezer, aioclient_mock, config_entry):
    await _setup_answering(hass, freezer, aioclient_mock, config_entry,
                           status=403, json=NOT_OWNER)
    assert _gone_issue(hass, config_entry) is not None
    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert _gone_issue(hass, config_entry) is None


async def test_any_other_refusal_waits_without_reauth(
        hass, freezer, setup_entry, aioclient_mock):
    for answer in ({"json": UNVERIFIED}, {"json": {"code": 403, "message": "nope"}}, {}):
        aioclient_mock.clear_requests()
        aioclient_mock.get(FORECAST_URL, status=403, **answer)
        await _fire(hass, freezer, timedelta(minutes=61))
        assert hass.states.get(ENTITY).state == "unavailable"
        assert hass.config_entries.flow.async_progress() == []
        calls = aioclient_mock.call_count
        await _fire(hass, freezer, timedelta(minutes=45))  # less than the hour it waits
        assert aioclient_mock.call_count == calls
    assert _gone_issue(hass, setup_entry) is None
