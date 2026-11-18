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
TOKEN_URL = "https://www.solariance.de/user/entwickler/"

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

# Upload of an irradiance sensor (0.3): the 5-minute means of the chosen
# sensor (device class irradiance, W/m²; never an illuminance sensor in lux)
# from the recorder's short-term statistics go to POST
# measured/irradiance/upload with source "ha_sensor", which Solariance stores
# FILL-ONLY like the production. One request per run and system: the last
# IRRADIANCE_RECENT_HOURS hours, and on the first run of each UTC hour (and
# after a gap) the last IRRADIANCE_FILL_HOURS hours instead.
CONF_IRRADIANCE_SENSOR = "irradiance_sensor"
CONF_IRRADIANCE_PLANE = "irradiance_plane"
CONF_IRRADIANCE_TILT = "irradiance_tilt"
CONF_IRRADIANCE_AZIMUTH = "irradiance_azimuth"
CONF_IRRADIANCE_TYPE = "irradiance_type"
CONF_IRRADIANCE_SENSOR_ID = "irradiance_sensor_id"
CONF_IRRADIANCE_GENERATION = "irradiance_generation"

PLANE_HORIZONTAL = "horizontal"
PLANE_MODULE = "module"
IRRADIANCE_PLANES = (PLANE_HORIZONTAL, PLANE_MODULE)
IRRADIANCE_TYPES = ("weather_station", "pyranometer", "reference_cell", "other")
DEFAULT_IRRADIANCE_TYPE = "weather_station"
# Offered for a module-plane sensor when neither the options nor the
# system's roof say otherwise.
DEFAULT_IRRADIANCE_TILT = 30
DEFAULT_IRRADIANCE_AZIMUTH = 180

IRRADIANCE_INTERVAL_MINUTES = 5
IRRADIANCE_RECENT_HOURS = 2
IRRADIANCE_FILL_HOURS = 48
# The route allows 120 requests an hour per account, shared by all its
# systems. One request per run: every 10 minutes (6 an hour) while an account
# has up to 10 systems uploading irradiance, then every 20, 30 or 60 minutes,
# so an account's Home Assistant never spends more than half of it (60 an
# hour) for up to 60 systems.
IRRADIANCE_CADENCES = ((10, 10), (20, 20), (30, 30))   # (systems up to, minutes)
IRRADIANCE_MAX_CADENCE = 60
