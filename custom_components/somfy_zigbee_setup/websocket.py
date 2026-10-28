"""Websocket commands the panel uses."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .z2m import Zigbee2MQTT


def _bridge(hass: HomeAssistant) -> Zigbee2MQTT:
    return hass.data[DOMAIN]


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/devices"})
@callback
def ws_devices(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    bridge = _bridge(hass)
    connection.send_result(msg["id"], {"base_topic": bridge.base, "devices": bridge.somfy_devices()})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/permit_join", vol.Optional("time", default=254): vol.All(int, vol.Range(0, 254))}
)
@websocket_api.async_response
async def ws_permit_join(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    await _bridge(hass).async_permit_join(msg["time"])
    connection.send_result(msg["id"], {"time": msg["time"]})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/config_status", vol.Required("ieee"): str})
@websocket_api.async_response
async def ws_config_status(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    value = await _bridge(hass).async_config_status(msg["ieee"])
    connection.send_result(msg["id"], {"config_status": value})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/rename",
        vol.Required("ieee"): str,
        vol.Required("name"): vol.All(str, vol.Strip, vol.Length(min=1, max=80)),
    }
)
@websocket_api.async_response
async def ws_rename(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    connection.send_result(msg["id"], await _bridge(hass).async_rename(msg["ieee"], msg["name"]))


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/interview", vol.Required("ieee"): str})
@websocket_api.async_response
async def ws_interview(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    await _bridge(hass).async_interview(msg["ieee"])
    connection.send_result(msg["id"])


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/subscribe_events"})
@callback
def ws_subscribe_events(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    @callback
    def forward(event: dict) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], event))

    connection.subscriptions[msg["id"]] = _bridge(hass).add_listener(forward)
    connection.send_result(msg["id"])


@callback
def async_register(hass: HomeAssistant) -> None:
    for command in (ws_devices, ws_permit_join, ws_config_status, ws_rename, ws_interview, ws_subscribe_events):
        websocket_api.async_register_command(hass, command)
