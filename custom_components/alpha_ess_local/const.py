"""Constants for the AlphaESS Local Control integration."""

from datetime import timedelta
from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

DOMAIN = "alpha_ess_local"
DEFAULT_NAME = "AlphaESS"
DEFAULT_PORT = 502
SCAN_INTERVAL = timedelta(seconds=30)
