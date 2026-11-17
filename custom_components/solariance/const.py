"""Constants for the Solariance integration."""
from __future__ import annotations

import logging
from datetime import timedelta

DOMAIN = "solariance"
LOGGER = logging.getLogger(__package__)

API_BASE = "https://api.solariance.de/v1"

CONF_API_TOKEN = "api_token"
CONF_SYSTEM_ID = "system_id"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_UPLOAD_SENSOR = "upload_sensor"

# Minutes between forecast polls. The forecast changes with each weather model
# run, a few times a day, so 30 minutes is plenty. The hourly limit of the free
# and Plus plans (30 forecast calls per hour) is shared with the Solariance app
# itself, so the fastest choice stays far below it even with several systems.
SCAN_INTERVALS = (15, 30, 60)
DEFAULT_SCAN_INTERVAL = 30

# Between polls, sensors that depend on the time of day ("power now",
# "remaining today") are recomputed from the stored forecast on this grid,
# without calling the API. Matches the 15-minute rows of days 0 and 1.
RECOMPUTE_INTERVAL = timedelta(minutes=15)

# Days after today that get an (optional) energy sensor. The free plan covers
# today and tomorrow; Plus and above five days.
EXTRA_DAYS = (2, 3, 4)

APP_URL = "https://app.solariance.de"
TOKEN_URL = "https://www.solariance.de/user/"

# Upload of the PV production (0.2): the chosen energy sensor's hourly
# statistics (kWh per hour, from the recorder's long-term statistics) are sent
# to Solariance once an hour, the last UPLOAD_HOURS hours each time, so an hour
# missed by a restart or a network gap is filled the next time. Solariance
# stores them FILL-ONLY (source "ha_sensor"): an hour the owner uploaded by
# hand is never replaced. The upload needs an API token with the Upload (or
# ReadWrite) scope and the account's measured-data opt-in.
UPLOAD_HOURS = 48
UPLOAD_MINUTE = 7
UPLOAD_SOURCE = "ha_sensor"
