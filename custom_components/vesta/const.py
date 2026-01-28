"""Constants for Vesta integration."""

from datetime import time

DOMAIN = "vesta"

CONF_BOILER_ENTITY = "boiler_entity"
CONF_BOOST_TEMP = "boost_temp"
CONF_COMFORT_TEMP = "comfort_temp"
CONF_OFF_TEMP = "off_temp"
CONF_WEATHER_ENTITY = "weather_entity"
CONF_MIN_CYCLE = "min_cycle"
CONF_WINDOW_THRESHOLD = "window_threshold"
CONF_VALVE_MAINTENANCE = "valve_maintenance"
CONF_MAINTENANCE_TIME = "maintenance_time"
CONF_MAINTENANCE_DAY = "maintenance_day"
CONF_BERMUDA_THRESHOLD = "bermuda_threshold"

DEFAULT_BOOST_TEMP = 25
DEFAULT_OFF_TEMP = 5
DEFAULT_MIN_CYCLE = 5
DEFAULT_WINDOW_THRESHOLD = 0.1
DEFAULT_ECO_TEMP = 16
DEFAULT_COMFORT_TEMP = 21
DEFAULT_VALVE_MAINTENANCE = True
DEFAULT_BERMUDA_THRESHOLD = 2.5
DEFAULT_MAINTENANCE_TIME = time(hour=11, minute=0)
DEFAULT_MAINTENANCE_DAY = 3

MAINTENANCE_DAY_BY_INDEX = {
    0: "Monday",
    1: "Tuesday",
    2: "Wednesday",
    3: "Thursday",
    4: "Friday",
    5: "Saturday",
    6: "Sunday",
}
MAINTENANCE_DAY_INDEX_BY_NAME = {
    name.casefold(): index for index, name in MAINTENANCE_DAY_BY_INDEX.items()
}

STORAGE_KEY = "vesta_learning"
STORAGE_VERSION = 1

EVENT_SCHEDULE_UPDATE = "vesta_schedule_update"
SERVICE_SET_SCHEDULE = "set_schedule"

PLATFORMS = ["climate", "number", "switch"]
