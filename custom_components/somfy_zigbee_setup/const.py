"""Constants for Somfy Zigbee Setup."""

DOMAIN = "somfy_zigbee_setup"
VERSION = "0.1.0"

CONF_BASE_TOPIC = "base_topic"
DEFAULT_BASE_TOPIC = "zigbee2mqtt"

PANEL_URL_PATH = "somfy-zigbee-setup"
PANEL_TITLE = "Somfy setup"
PANEL_ICON = "mdi:blinds-horizontal"
STATIC_URL = "/somfy_zigbee_setup_static"

# how long to wait for the motor to answer a configStatus read, in seconds
READ_TIMEOUT = 15
