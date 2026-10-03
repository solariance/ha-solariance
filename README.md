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
`grid_kwh`, `pv_coverage_percent` and, with a price, `estimated_saving_eur`. Example: start the
dishwasher at the window's start with a time trigger on `window.window_start`.

**`solariance.get_forecast`** returns the forecast per day (hourly or quarter-hourly) as response data.

## Plans and limits

The free plan covers today and tomorrow; Plus and above cover five days. Forecast requests are limited
per account (30 per hour on Free and Plus) and the limit is shared with the Solariance app. At the
default 30-minute interval the integration uses 2 requests per hour per system. If the limit is hit,
the integration waits as long as the API asks and the sensors show *unavailable* meanwhile.

## Troubleshooting

- **"Solariance has not computed a forecast for this system yet"**: a new system gets its first forecast
  with the next model run; Home Assistant retries on its own.
- **Re-authentication requested**: the token was revoked or expired. Create a new one and paste it in
  the repair dialog.
- **Diagnostics**: the integration's ⋮ menu → Download diagnostics (the token is removed).

## Removal

Delete the integration under Settings → Devices & services, then revoke the token at
[solariance.de/user](https://www.solariance.de/user/).

## Privacy

The integration talks only to `api.solariance.de` with your token. The token is stored in Home
Assistant's configuration; nothing else leaves your Home Assistant.

This is an integration by Solariance for Home Assistant. It is not affiliated with or endorsed by the
Open Home Foundation.

## License

MIT, see [LICENSE](LICENSE).
