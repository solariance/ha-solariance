"""The forecast arithmetic, without Home Assistant."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.solariance.forecast import (
    ForecastFormatError,
    find_surplus_window,
    parse_power_forecast,
)

from .conftest import BERLIN, NOW, SYSTEM_ID, make_day, make_payload


def test_parse_two_days():
    forecast = parse_power_forecast(make_payload())
    assert forecast.system_id == SYSTEM_ID
    assert str(forecast.time_zone) == "Europe/Berlin"
    assert [d.date for d in forecast.days] == [date(2026, 7, 1), date(2026, 7, 2)]
    assert len(forecast.days[0].intervals) == 96
    first = forecast.days[0].intervals[0]
    # Local midnight in summer is 22:00 UTC the day before; rows are START-labelled.
    assert first.start == datetime(2026, 6, 30, 22, 0, tzinfo=UTC)
    assert first.end - first.start == timedelta(minutes=15)


def test_today_is_found_by_date_not_by_position():
    # The API leaves out a day it has not computed: here "today" is missing.
    payload = make_payload(first=date(2026, 7, 2), days=1)
    forecast = parse_power_forecast(payload)
    assert forecast.day(0, NOW) is None
    assert forecast.day(1, NOW).date == date(2026, 7, 2)


def test_day_totals_and_peak():
    forecast = parse_power_forecast(make_payload())
    today = forecast.day(0, NOW)
    rows = make_day(date(2026, 7, 1))
    assert today.energy_kwh == pytest.approx(sum(r["energy_in_time_interval"] for r in rows))
    # The bell peaks at 13:00 local; the interval 12:45-13:00 or 13:00-13:15.
    assert today.peak.start.astimezone(BERLIN).hour in (12, 13)
    assert today.production_start.astimezone(BERLIN).hour == 5
    assert today.production_end.astimezone(BERLIN) <= datetime(2026, 7, 1, 21, 15, tzinfo=BERLIN)


def test_remaining_today_prorates_the_current_interval():
    forecast = parse_power_forecast(make_payload())
    today = forecast.day(0, NOW)
    # 10:07 lies in 10:00-10:15: 8 of its 15 minutes are still to come.
    current = next(i for i in today.intervals if i.start <= NOW < i.end)
    later = sum(i.energy_kwh for i in today.intervals if i.start >= current.end)
    assert forecast.energy_remaining_today(NOW) == pytest.approx(
        later + current.energy_kwh * 8 / 15)


def test_power_at_uses_the_interval_containing_the_moment():
    forecast = parse_power_forecast(make_payload())
    current = next(i for i in forecast.days[0].intervals if i.start <= NOW < i.end)
    assert forecast.power_at(NOW) == current.power_w
    assert forecast.power_at(NOW + timedelta(days=5)) is None


def test_wh_hours_are_utc_hours_and_add_up():
    forecast = parse_power_forecast(make_payload(days=3))
    hours = forecast.wh_hours()
    assert all(h.minute == 0 and h.tzinfo == UTC for h in hours)
    total_kwh = sum(d.energy_kwh for d in forecast.days)
    assert sum(hours.values()) / 1000 == pytest.approx(total_kwh, rel=1e-6)


def test_long_rows_are_spread_over_their_hours():
    payload = make_payload(days=1)
    payload["forecast"] = [make_day(date(2026, 7, 3), step=180)]
    hours = parse_power_forecast(payload).wh_hours()
    noon = datetime(2026, 7, 3, 10, 0, tzinfo=UTC)   # 12:00 Berlin, inside 11:00-14:00
    assert hours[noon] == pytest.approx(hours[noon + timedelta(hours=1)])


def test_autumn_repeated_hour_keeps_both_intervals():
    day = date(2026, 10, 25)   # 03:00 CEST -> 02:00 CET
    payload = make_payload(days=1)
    payload["forecast"] = [make_day(day)]
    forecast = parse_power_forecast(payload)
    starts = [i.start for i in forecast.days[0].intervals]
    assert len(starts) == 100 and len(set(starts)) == 100
    assert starts == sorted(starts)


@pytest.mark.parametrize("bad", [None, {}, {"forecast": "x"},
                                 {"forecast": [[{"step": 15}]]},
                                 {"forecast": [], "time_zone": "Mars/Olympus"}])
def test_unreadable_answers_raise(bad):
    with pytest.raises(ForecastFormatError):
        parse_power_forecast(bad)


def test_surplus_window_takes_the_earliest_fully_covered_window():
    # A 2 kW load is covered from mid-morning on; like the assistant's tool,
    # the first best window wins, so the device runs as early as it can.
    forecast = parse_power_forecast(make_payload())
    today = forecast.day(0, NOW)
    window = find_surplus_window(today, load_kw=2.0, duration_hours=2, baseline_kw=0.3)
    assert window.end - window.start == timedelta(hours=2)
    assert window.pv_covered_kwh == pytest.approx(4.0, rel=0.01)
    assert window.grid_kwh == pytest.approx(0.0, abs=0.05)
    assert window.pv_coverage_percent == 100
    assert 6 <= window.start.astimezone(BERLIN).hour <= 10


def test_surplus_window_finds_the_midday_block_for_a_big_load():
    # 7 kW over a 0.3 kW base load fits only around the 8 kW peak at 13:00.
    forecast = parse_power_forecast(make_payload())
    today = forecast.day(0, NOW)
    window = find_surplus_window(today, load_kw=7.0, duration_hours=2, baseline_kw=0.3)
    assert 11 <= window.start.astimezone(BERLIN).hour <= 12
    assert window.start <= today.peak.start < window.end
    # 7.6 kW never fits fully: the best window still takes most from the roof.
    tight = find_surplus_window(today, load_kw=7.6, duration_hours=2, baseline_kw=0.3)
    assert tight.start <= today.peak.start < tight.end
    assert 0 < tight.grid_kwh < tight.load_kwh


def test_surplus_window_respects_not_before():
    forecast = parse_power_forecast(make_payload())
    today = forecast.day(0, NOW)
    late = datetime(2026, 7, 1, 19, 0, tzinfo=BERLIN)
    window = find_surplus_window(today, 2.0, 1, not_before=late)
    assert window.start >= late
    assert window.pv_coverage_percent < 100


def test_surplus_window_none_when_too_long_for_what_is_left():
    forecast = parse_power_forecast(make_payload())
    today = forecast.day(0, NOW)
    assert find_surplus_window(today, 2.0, 3,
                               not_before=datetime(2026, 7, 1, 22, 0, tzinfo=BERLIN)) is None


def test_surplus_window_on_a_day_of_mixed_steps_is_as_long_as_asked():
    # Later days mix 15-, 60- and 180-minute rows (seen in the live API).
    rows = make_day(date(2026, 7, 3), step=15)[:48]          # 00:00-12:00 quarter-hourly
    rows += [r for r in make_day(date(2026, 7, 3), step=60)[12:]]   # 12:00-24:00 hourly
    payload = make_payload(days=1)
    payload["forecast"] = [rows]
    day = parse_power_forecast(payload).days[0]
    window = find_surplus_window(day, load_kw=2.0, duration_hours=2)
    assert window.end - window.start == timedelta(hours=2)
    late = find_surplus_window(day, load_kw=7.6, duration_hours=1.5)
    assert late.end - late.start == timedelta(hours=1, minutes=30)

