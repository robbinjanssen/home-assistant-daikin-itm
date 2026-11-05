"""Constants for the Daikin iTM integration."""

import logging
from datetime import timedelta

DOMAIN = "daikin_itm"
LOGGER = logging.getLogger(__package__)

CONF_ZONE_PATH = "zone_path"
# The top level of the iTM's icon view; most iTMs group the units in zones.
DEFAULT_ZONE_PATH = ""

# Each poll logs in, reads and logs out; the web UI is free in between (the iTM allows
# one session).
SCAN_INTERVAL = timedelta(seconds=60)
# The settings (schedules, changeover groups, overtime, the menu bar's indicators, and
# the units' locks, limits and airflow direction) take about twenty requests in two
# sessions, and rarely change: read them far less often.
SETTINGS_SCAN_INTERVAL = timedelta(minutes=15)
# Changes take 3-8 seconds to reach a unit over the Daikin bus. After a change the
# entities show it right away and the next poll, which confirms it, runs this soon.
CONFIRM_INTERVAL = timedelta(seconds=10)
# A change is shown from the moment it is asked for. Polls don't undo it until they
# show it, or until this long after the iTM accepted it.
PENDING_TIMEOUT = timedelta(seconds=30)
# Polls may be refused while someone uses the web UI; keep the last data for this many
# failed polls.
MAX_TOLERATED_LOGIN_REJECTIONS = 5
# The iTM refuses a wrong password and a busy web UI in the same way. Once logins have
# been refused for this many polls in a row, a repair issue asks the user to check.
LOGIN_REJECTED_ISSUE_AFTER = 30
