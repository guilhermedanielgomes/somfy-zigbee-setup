"""Somfy Zigbee Setup.

Pairs Somfy Zigbee motors with Zigbee2MQTT and calibrates them, with the
Bluetooth part done by the browser of whatever phone or laptop is next to the
blind. Home Assistant only talks to Zigbee2MQTT, over MQTT.
"""

from __future__ import annotations

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_BASE_TOPIC, DEFAULT_BASE_TOPIC, DOMAIN
from .panel import async_register_panel, async_remove_panel
from .websocket import async_register
from .z2m import Zigbee2MQTT

_WS_REGISTERED = f"{DOMAIN}_ws_registered"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not await mqtt.async_wait_for_mqtt_client(hass):
        raise ConfigEntryNotReady("MQTT is not connected yet")

    bridge = Zigbee2MQTT(hass, entry.data.get(CONF_BASE_TOPIC, DEFAULT_BASE_TOPIC))
    await bridge.async_start()
    hass.data[DOMAIN] = bridge

    if not hass.data.get(_WS_REGISTERED):
        async_register(hass)
        hass.data[_WS_REGISTERED] = True
    await async_register_panel(hass, bridge.base)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    bridge: Zigbee2MQTT | None = hass.data.pop(DOMAIN, None)
    if bridge is not None:
        await bridge.async_stop()
    async_remove_panel(hass)
    return True
