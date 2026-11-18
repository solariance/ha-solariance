# Solariance for Home Assistant

PV production forecasts for your [Solariance](https://www.solariance.de) systems in Home Assistant:
sensors for today, tomorrow and the next days, the forecast curve on the **Energy dashboard**, and an
action that finds the best time to run a device on solar power.

## Requirements

- Home Assistant 2026.9 or newer
- A Solariance account with at least one PV system (the free plan works)
- A personal API token: [solariance.de/user](https://www.solariance.de/user/) → API tokens. Read-only is enough.

## Installation

**HACS (custom repository)**

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=solariance&repository=ha-solariance&category=integration)

Or in HACS: ⋮ → Custom repositories → `https://github.com/solariance/ha-solariance`, type *Integration*.
Then install **Solariance** and restart Home Assistant.

**Manual:** copy `custom_components/solariance` into your `config/custom_components/` folder and restart.

## Setup

Settings → Devices & services → Add integration → **Solariance**, then paste the API token. The token
is checked by listing your systems. With several systems you pick one; add the integration again for
each further system. Each system becomes one device.

**Options:** how often the forecast is fetched (15, 30 or 60 minutes; default 30).

## Entities

| Entity | Unit | Notes |
|---|---|---|
| Estimated energy production – today / remaining today / tomorrow | kWh | today and tomorrow carry an `hourly` attribute for chart cards |
| Estimated energy production – this hour / next hour | kWh | clock hours in the system's time zone |
| Estimated power production – now / next hour | W | next 12 h and next 24 h are disabled by default |
| Highest power peak – today / tomorrow | W | |
| Highest power peak time – today / tomorrow | timestamp | |
| Production start / end – today | timestamp | first and last producing quarter hour |
| Estimated energy production – day 3 / 4 / 5 | kWh | disabled by default; available on plans with 5 forecast days |

Names follow the core Forecast.Solar integration, so existing dashboards port over with a rename.
"Now" values are recomputed every quarter hour from the stored forecast, without extra API calls.

*Estimated energy production – today* and *Estimated power production – now* also carry
`forecast_status` (`provisional` while a new system's first forecast still uses the weather of a
nearby point, then `ready`) and `generated_utc` (when Solariance computed the forecast), whenever
Solariance sends them.

## Energy dashboard

Settings → Dashboards → Energy → Solar panels → *Solar production forecast* → choose your Solariance system.

## Actions

**`solariance.find_surplus_window`** returns the window in which a device is covered best by the
forecast solar surplus. For today only windows that have not started yet are considered.

```yaml
action: solariance.find_surplus_window
data:
  config_entry_id: <your Solariance entry>
  load_kw: 2.0            # what the device draws
  duration_hours: 2
  baseline_kw: 0.3        # what the home draws anyway (optional)
  electricity_price_eur_per_kwh: 0.32   # optional, for a saving estimate
  feed_in_tariff_eur_per_kwh: 0.08      # optional
response_variable: window
```

The response holds `found`, `window_start`, `window_end` (ISO time with offset), `pv_covered_kwh`,
`grid_kwh`, `pv_coverage_percent` and, with a price, `estimated_saving_eur`.

Example: plan the dishwasher in the morning and start it at the window's start. The response only
exists inside the automation that called the action, so the start time goes into a date-and-time
helper (Settings → Devices & services → Helpers → Create helper → Date and/or time, with date and
time), and a second automation fires at it:

```yaml
# 1. Plan: every morning at 07:00
triggers:
  - trigger: time
    at: "07:00:00"
actions:
  - action: solariance.find_surplus_window
    data:
      config_entry_id: <your Solariance entry>
      load_kw: 2.0
      duration_hours: 2
    response_variable: window
  - if: "{{ window.found }}"
    then:
      # As a timestamp: the helper would drop the time's offset and store the
      # wrong clock time whenever Home Assistant runs in another time zone.
      - action: input_datetime.set_datetime
        target:
          entity_id: input_datetime.dishwasher_start
        data:
          timestamp: "{{ as_timestamp(window.window_start) }}"

# 2. Start: when that time comes
triggers:
  - trigger: time
    at: input_datetime.dishwasher_start
actions:
  - action: switch.turn_on
    target:
      entity_id: switch.dishwasher_plug
```

**`solariance.get_forecast`** returns the forecast per day (hourly or quarter-hourly) as response data.

## Upload your production (optional)

Since 0.2 the integration can send your PV production to Solariance, so the nightly calibration
learns how your roof differs from the model. Choose your PV energy sensor (kWh, `total_increasing`,
the one your Energy dashboard uses) under Settings → Devices & services → Solariance → Configure.

- Every hour, at seven past, the last 48 full hours of the sensor's long-term statistics go to
  Solariance (`POST measured/production/upload`, source `ha_sensor`).
- Solariance only **fills** hours it does not have yet: an hour you uploaded yourself is never
  replaced, and re-sending the same hours stores nothing new.
- It needs an API token with the **Upload** scope (it reads like a read-only token and writes only
  measured data) or ReadWrite, and the measured-data opt-in in your Solariance account. Until then
  the upload is refused, the integration says why in its diagnostics and the log (once, not every
  hour), and the forecast sensors keep working.
- Leave the field empty to switch the upload off.

## Upload an irradiance sensor (optional)

Since 0.3 the integration can also send what an irradiance sensor on your roof or in your garden
measures, so Solariance learns how much sun actually reached your system. Choose the sensor under
Settings → Devices & services → Solariance → Configure. Only sensors with the device class
*irradiance* (W/m²) are offered: a pyranometer, a reference cell, a weather station's solar
radiation. Light sensors in lux (*illuminance*) measure something else and are not offered. The
sensor needs the state class `measurement` (irradiance sensors have it), because the integration
reads Home Assistant's 5-minute statistics.

- Two more questions follow: whether the sensor lies **horizontal** or in the **module plane**, and
  what kind of sensor it is (weather station by default). For the module plane, its tilt (0° flat,
  90° vertical) and azimuth (clockwise from north: 90° east, 180° south, 270° west), prefilled with
  your system's largest roof face when Solariance has it.
- Every 10 minutes the 5-minute means of the last 2 hours go to Solariance
  (`POST measured/irradiance/upload`, interval 5, label `start`, source `ha_sensor`); the first run
  of each hour sends the last 48 hours instead, to fill what a restart missed. That is 6 requests
  per hour per system. The route allows 120 per hour per account, shared by all its systems, so with
  more than 10 systems on the same token the integration runs every 20, 30 or 60 minutes instead and
  stays at 60 requests per hour at most for up to 60 systems.
- Solariance only **fills** values it does not have yet, so a repeat stores nothing new. Values in
  BTU/(h⋅ft²) are converted to W/m²; any other unit is not sent.
- The sensor goes to Solariance under an id like `ha-1a2b3c4d5e6f-a1b2c3`, derived from the entity
  (not its name) and its position. Solariance fixes a sensor's position once it has values, so when
  you change the position in the options, the integration starts a new series under a new id by
  itself; the values sent before stay with the old one.
- It needs an API token with the **Upload** (or ReadWrite) scope. While Solariance does not take
  irradiance uploads yet, or your account does not include them, the integration says so once in
  the log (as information, not as an error) and asks again every 6 hours; other refusals are kept
  quiet the same way (the diagnostics show the next attempt). If Solariance reports that the id
  already has values with another position, a repair issue *irradiance sensor id taken* appears:
  submitting it continues under a new id.
- None of this affects the forecast sensors or the production upload. Leave the field empty to
  switch the irradiance upload off.

## Plans and limits

The free plan covers today and tomorrow; Plus and above cover five days. Forecast requests are limited
per account (30 per hour on Free and Plus) and the limit is shared with the Solariance app. At the
default 30-minute interval the integration uses 2 requests per hour per system. If the limit is hit,
the integration does not retry early (refused requests count too): it waits for the next update, or
as long as the API asks, and the sensors show *unavailable* meanwhile.

## Troubleshooting

- **"Solariance has not computed a forecast for this system yet"**: a new system usually gets its first
  forecast within a minute. When Solariance says when to ask again, the integration checks back then
  (after 1 to 30 minutes, longer each time while the wait goes on) instead of waiting the full
  interval, and logs one line with the status (`queued`, `delayed`, `failed`, `stale`) and the
  expected time; otherwise it retries at the next update.
- **Re-authentication requested**: the token was revoked or expired. Create a new one and paste it in
  the repair dialog.
- **"Solariance has paused this system"** (also shown under Settings → Repairs as *Solariance system
  paused*): the account has more systems, or a larger one, than its plan covers. The token is fine;
  change the plan or remove a system at solariance.de. The integration checks again every hour, and
  the notice closes on its own once the system is active again.
- **"System no longer in the account"** (a repair issue): Solariance says the system was deleted, or the
  token belongs to another account. A new token for the same account will not bring it back: remove
  the entry, or add the system again with a token of the account that has it. The issue closes on
  its own once the system answers again.
- **"Solariance refused the request"**: the account refused this request (for example, an e-mail
  address not confirmed yet). The token is fine and no new one is asked for; the integration asks
  again every hour.
- **Diagnostics**: the integration's ⋮ menu → Download diagnostics (the token is removed).

## Removal

Delete the integration under Settings → Devices & services, then revoke the token at
[solariance.de/user](https://www.solariance.de/user/).

## Privacy

The integration talks only to `api.solariance.de` with your token. The token is stored in Home
Assistant's configuration. Without an upload sensor nothing else leaves your Home Assistant; with
one, that sensor's hourly energy (kWh per hour, nothing else) goes to Solariance once an hour, for
your own system's calibration. With an irradiance sensor, its 5-minute means in W/m², the plane,
tilt, azimuth and type you entered, and an id derived from the entity (not its name) go to
Solariance every 10 minutes, for your own system's forecast.

This is an integration by Solariance for Home Assistant. It is not affiliated with or endorsed by the
Open Home Foundation.

## License

MIT, see [LICENSE](LICENSE).
