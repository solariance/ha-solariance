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
