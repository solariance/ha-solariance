"""The Solariance forecast as Home Assistant reads it: pure functions, no I/O.

Everything the sensors, the Energy dashboard and the actions show is computed
here from one `GET /v1/forecast/power` answer, so it is all testable without
Home Assistant and without the network.

THE ROWS. `data.forecast` is a list of local days, each a list of rows:

    {"local_time": "2026-10-04T12:15:00",   naive, the system's zone
     "utc_time":   "2026-10-04T10:15:00",   naive UTC, no "Z"
     "step": 15,                            minutes; 15 for days 0-1, 60/180 later
     "ac_output_of_system": 2140.5,         W, the mean over the interval
     "energy_in_time_interval": 0.535,      kWh in the interval
     "energy_aggregated_sum_intraday": 9.8} kWh through the END of the row

A row is labelled with the START of its interval (the backend's
FORECAST_INTERVAL_LABELS.md): the row labelled 12:15 is 12:15-12:30. Intervals
are placed by `utc_time`, so the autumn's repeated local hour and the spring's
missing one need no special case.

"TODAY" is the day whose date is today's date in the SYSTEM's time zone, never
simply the first list: the API omits a day the model has not computed yet.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Interval:
    """One forecast row: [start, end) in UTC, mean power and energy."""

    start: datetime
    end: datetime
    power_w: float
    energy_kwh: float

    def overlap_kwh(self, start: datetime, end: datetime) -> float:
        """The share of this interval's energy that falls in [start, end)."""
        lo, hi = max(self.start, start), min(self.end, end)
        if hi <= lo:
            return 0.0
        return self.energy_kwh * (hi - lo) / (self.end - self.start)


@dataclass(frozen=True)
class Day:
    """One local day of intervals, in time order."""

    date: date
    intervals: tuple[Interval, ...]

    @property
    def energy_kwh(self) -> float:
        return sum(i.energy_kwh for i in self.intervals)

    @property
    def peak(self) -> Interval | None:
        """The interval with the highest mean power, or None on a day without sun."""
        best = None
        for interval in self.intervals:
            if interval.power_w > 0 and (best is None or interval.power_w > best.power_w):
                best = interval
        return best

    @property
    def production_start(self) -> datetime | None:
        """Start of the first interval that produces anything."""
        return next((i.start for i in self.intervals if i.power_w > 0), None)

    @property
    def production_end(self) -> datetime | None:
        """End of the last interval that produces anything."""
        return next((i.end for i in reversed(self.intervals) if i.power_w > 0), None)


@dataclass(frozen=True)
class SolarForecast:
    """A system's forecast, as of one API answer."""

    system_id: str
    time_zone: ZoneInfo
    days: tuple[Day, ...]

    def local_date(self, moment: datetime) -> date:
        return moment.astimezone(self.time_zone).date()

    def day(self, offset: int, now: datetime) -> Day | None:
        """The day `offset` days after today in the system's zone, or None."""
        wanted = self.local_date(now) + timedelta(days=offset)
        return next((d for d in self.days if d.date == wanted), None)

    def _intervals(self):
        for day in self.days:
            yield from day.intervals

    def covers(self, moment: datetime) -> bool:
        return any(i.start <= moment < i.end for i in self._intervals())

    def power_at(self, moment: datetime) -> float | None:
        """Mean power of the interval containing `moment`, or None outside the forecast."""
        for interval in self._intervals():
            if interval.start <= moment < interval.end:
                return interval.power_w
        return None

    def energy_between(self, start: datetime, end: datetime) -> float:
        """kWh forecast for [start, end), partial intervals prorated."""
        return sum(i.overlap_kwh(start, end) for i in self._intervals())

    def energy_remaining_today(self, now: datetime) -> float | None:
        today = self.day(0, now)
        if today is None:
            return None
        return sum(i.overlap_kwh(now, i.end) for i in today.intervals)

    def wh_hours(self) -> dict[datetime, float]:
        """Wh per UTC clock hour, keyed by the hour's start -- the Energy
        dashboard's solar forecast. An interval longer than an hour (the
        180-minute rows of later days) is spread over the hours it covers."""
        out: dict[datetime, float] = {}
        for interval in self._intervals():
            hour = interval.start.replace(minute=0, second=0, microsecond=0)
            while hour < interval.end:
                wh = 1000.0 * interval.overlap_kwh(hour, hour + timedelta(hours=1))
                if wh:
                    out[hour] = out.get(hour, 0.0) + wh
                hour += timedelta(hours=1)
        return dict(sorted(out.items()))


class ForecastFormatError(ValueError):
    """The API answered 200 with a body this integration cannot read."""


def _naive_utc(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _row_start(row: dict, zone: ZoneInfo) -> datetime:
    if row.get("utc_time"):
        return _naive_utc(str(row["utc_time"]))
    # Older answers carried local time only; the repeated autumn hour then
    # takes its first reading (fold=0), which is the best a local label allows.
    return datetime.fromisoformat(str(row["local_time"])).replace(tzinfo=zone).astimezone(UTC)


def _number(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0   # NaN or inf is no production


def parse_power_forecast(data: dict) -> SolarForecast:
    """The `data` object of GET /v1/forecast/power as a SolarForecast."""
    if not isinstance(data, dict) or not isinstance(data.get("forecast"), list):
        raise ForecastFormatError("no forecast list in the answer")
    try:
        zone = ZoneInfo(str(data.get("time_zone") or "Europe/Berlin"))
    except (KeyError, ValueError) as err:
        raise ForecastFormatError(f"unknown time zone {data.get('time_zone')!r}") from err

    days = []
    for rows in data["forecast"]:
        if not isinstance(rows, list) or not rows:
            continue
        intervals = []
        try:
            for row in rows:
                start = _row_start(row, zone)
                step = timedelta(minutes=int(row.get("step") or 15))
                intervals.append(Interval(
                    start=start,
                    end=start + step,
                    power_w=max(_number(row.get("ac_output_of_system")), 0.0),
                    energy_kwh=max(_number(row.get("energy_in_time_interval")), 0.0),
                ))
            day = date.fromisoformat(str(rows[0]["local_time"])[:10])
        except (KeyError, TypeError, ValueError) as err:
            raise ForecastFormatError(f"unreadable forecast row: {err}") from err
        intervals.sort(key=lambda i: i.start)
        days.append(Day(date=day, intervals=tuple(intervals)))
    days.sort(key=lambda d: d.date)
    return SolarForecast(system_id=str(data.get("system_id") or ""), time_zone=zone,
                         days=tuple(days))


@dataclass(frozen=True)
class SurplusWindow:
    start: datetime
    end: datetime
    load_kwh: float
    pv_covered_kwh: float

    @property
    def grid_kwh(self) -> float:
        return max(self.load_kwh - self.pv_covered_kwh, 0.0)

    @property
    def pv_coverage_percent(self) -> float:
        return round(100.0 * self.pv_covered_kwh / self.load_kwh) if self.load_kwh else 0.0


_SLOT = timedelta(minutes=15)


def _quarter_hours(intervals):
    """The intervals cut into slots of at most 15 minutes, energy prorated."""
    for interval in intervals:
        start = interval.start
        while start < interval.end:
            end = min(start + _SLOT, interval.end)
            yield Interval(start=start, end=end, power_w=interval.power_w,
                           energy_kwh=interval.overlap_kwh(start, end))
            start = end


def find_surplus_window(day: Day, load_kw: float, duration_hours: float,
                        baseline_kw: float = 0.0,
                        not_before: datetime | None = None) -> SurplusWindow | None:
    """The run of consecutive intervals, `duration_hours` long, in which a
    `load_kw` load is covered most by the forecast surplus over `baseline_kw`.

    The same arithmetic as the Solariance assistant's tool of the same name
    (backend mcp_server/tools.py), plus `not_before`: an automation asking at
    10:00 wants a window that has not started yet. None when no such window
    fits in what is left of the day.
    """
    # Quarter-hour slots, whatever the rows' step: later days mix 15-, 60-
    # and 180-minute rows, and a window counted in rows would then be longer
    # than asked for. A long row's power holds for each of its slots.
    intervals = [slot for slot in _quarter_hours(day.intervals)
                 if not_before is None or slot.start >= not_before]
    if not intervals or load_kw <= 0 or duration_hours <= 0:
        return None
    step_h = _SLOT.total_seconds() / 3600.0
    n = max(1, round(duration_hours / step_h))
    if n > len(intervals):
        return None
    covered = [min(max(i.power_w / 1000.0 - baseline_kw, 0.0), load_kw)
               * (i.end - i.start).total_seconds() / 3600.0 for i in intervals]
    best_i, best_kwh = 0, -1.0
    window = sum(covered[:n])
    for i in range(len(intervals) - n + 1):
        if i:
            window += covered[i + n - 1] - covered[i - 1]
        if window > best_kwh + 1e-9:
            best_i, best_kwh = i, window
    return SurplusWindow(start=intervals[best_i].start, end=intervals[best_i + n - 1].end,
                         load_kwh=load_kw * n * step_h, pv_covered_kwh=max(best_kwh, 0.0))
