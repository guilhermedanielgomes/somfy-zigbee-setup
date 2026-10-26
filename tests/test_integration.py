"""Tests for the Home Assistant side: config flow, panel, websocket commands, Zigbee2MQTT bridge."""

import asyncio
import json

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_mqtt_message

from custom_components.somfy_zigbee_setup.const import DOMAIN

IEEE = "0x4cc206fffe7012b2"
DEVICES = [
    {"ieee_address": "0x00124b0012345678", "friendly_name": "Coordinator", "type": "Coordinator"},
    {"ieee_address": "0x0017880104abcdef", "friendly_name": "Hue lamp", "type": "Router", "manufacturer": "Philips",
     "supported": True, "definition": {"vendor": "Philips", "description": "Bulb", "source": "native"}},
    {"ieee_address": IEEE, "friendly_name": "Office blind", "type": "Router", "manufacturer": "SOMFY",
     "model_id": "Sonesse2 40 Zigbee Venetian", "supported": False, "definition": None},
]


async def setup_integration(hass, mqtt_mock):
    entry = MockConfigEntry(domain=DOMAIN, data={"base_topic": "zigbee2mqtt"}, title="Somfy Zigbee Setup")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_config_flow_creates_one_entry(hass, mqtt_mock):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "form"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"base_topic": "zigbee2mqtt"})
    assert result["type"] == "create_entry"
    assert result["data"] == {"base_topic": "zigbee2mqtt"}
    await hass.async_block_till_done()

    again = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert again["type"] == "abort"
    assert again["reason"] == "single_instance_allowed"


async def test_setup_registers_panel_and_lists_somfy_devices(hass, mqtt_mock, hass_ws_client):
    await setup_integration(hass, mqtt_mock)
    panels = hass.data["frontend_panels"]
    assert "somfy-zigbee-setup" in panels
    assert panels["somfy-zigbee-setup"].config["_panel_custom"]["module_url"].startswith("/somfy_zigbee_setup_static/panel.js")

    # subscribed at setup, not when a read is pending: Home Assistant passes
    # subscriptions to the broker a moment after async_subscribe returns, and
    # a log line answering a read published in between would be lost
    subscribed = {call.args[0] for call in mqtt_mock.async_subscribe.call_args_list}
    assert {"zigbee2mqtt/bridge/devices", "zigbee2mqtt/bridge/event", "zigbee2mqtt/bridge/logging",
            "zigbee2mqtt/bridge/response/device/rename"} <= subscribed

    async_fire_mqtt_message(hass, "zigbee2mqtt/bridge/devices", json.dumps(DEVICES))
    await hass.async_block_till_done()

    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": f"{DOMAIN}/devices"})
    reply = await client.receive_json()
    assert reply["success"]
    assert reply["result"]["base_topic"] == "zigbee2mqtt"
    assert reply["result"]["devices"] == [
        {"ieee": IEEE, "friendly_name": "Office blind", "model_id": "Sonesse2 40 Zigbee Venetian",
         "supported": False, "description": None, "source": None}
    ]


async def test_permit_join_publishes(hass, mqtt_mock, hass_ws_client):
    await setup_integration(hass, mqtt_mock)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": f"{DOMAIN}/permit_join"})
    reply = await client.receive_json()
    assert reply["success"] and reply["result"] == {"time": 254}
    mqtt_mock.async_publish.assert_called_with("zigbee2mqtt/bridge/request/permit_join", '{"time": 254}', 0, False)


async def test_events_are_forwarded(hass, mqtt_mock, hass_ws_client):
    await setup_integration(hass, mqtt_mock)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": f"{DOMAIN}/subscribe_events"})
    assert (await client.receive_json())["success"]

    async_fire_mqtt_message(hass, "zigbee2mqtt/bridge/event", json.dumps(
        {"type": "device_interview", "data": {"friendly_name": IEEE, "ieee_address": IEEE, "status": "successful",
                                              "supported": True, "definition": {"description": "Venetian"}}}))
    await hass.async_block_till_done()
    event = await client.receive_json()
    assert event["type"] == "event"
    assert event["event"] == {"type": "device_interview", "ieee": IEEE, "friendly_name": IEEE, "status": "successful",
                              "supported": True, "description": "Venetian"}


@pytest.mark.parametrize("via", ["state", "log"])
async def test_config_status_read(hass, mqtt_mock, hass_ws_client, via):
    await setup_integration(hass, mqtt_mock)
    async_fire_mqtt_message(hass, "zigbee2mqtt/bridge/devices", json.dumps(DEVICES))
    await hass.async_block_till_done()
    client = await hass_ws_client(hass)

    async def answer():
        # give the read a moment to be published, then answer like Zigbee2MQTT would
        await asyncio.sleep(0.2)
        topic, payload = mqtt_mock.async_publish.call_args[0][:2]
        assert topic == "zigbee2mqtt/Office blind/1/set"
        assert json.loads(payload)["read"]["attributes"][0] == 7
        if via == "state":
            async_fire_mqtt_message(hass, "zigbee2mqtt/Office blind",
                                    json.dumps({"linkquality": 40, "somfy_config_status": {"configStatus": 127}}))
        else:
            async_fire_mqtt_message(hass, "zigbee2mqtt/bridge/logging",
                                    json.dumps({"level": "info", "message": "Read result of '258': {\"configStatus\":127}",
                                                "namespace": "zhc:tz"}))

    await client.send_json({"id": 1, "type": f"{DOMAIN}/config_status", "ieee": IEEE})
    task = hass.async_create_task(answer())
    reply = await client.receive_json()
    await task
    assert reply["success"] and reply["result"] == {"config_status": 127}


@pytest.mark.parametrize("answer", ["ok", "error"])
async def test_rename(hass, mqtt_mock, hass_ws_client, answer):
    await setup_integration(hass, mqtt_mock)
    async_fire_mqtt_message(hass, "zigbee2mqtt/bridge/devices", json.dumps(DEVICES))
    await hass.async_block_till_done()
    client = await hass_ws_client(hass)

    async def z2m_answers():
        await asyncio.sleep(0.2)
        topic, payload = mqtt_mock.async_publish.call_args[0][:2]
        assert topic == "zigbee2mqtt/bridge/request/device/rename"
        request = json.loads(payload)
        assert request["from"] == "Office blind" and request["to"] == "Kitchen" and request["homeassistant_rename"] is True
        response = {"data": {"from": "Office blind", "to": "Kitchen"}, "status": "ok", "transaction": request["transaction"]}
        if answer == "error":
            response = {"data": {}, "status": "error", "error": "Device 'Kitchen' already exists", "transaction": request["transaction"]}
        async_fire_mqtt_message(hass, "zigbee2mqtt/bridge/response/device/rename", json.dumps(response))

    await client.send_json({"id": 1, "type": f"{DOMAIN}/rename", "ieee": IEEE, "name": " Kitchen "})
    task = hass.async_create_task(z2m_answers())
    reply = await client.receive_json()
    await task
    assert reply["success"]
    if answer == "ok":
        assert reply["result"] == {"status": "ok"}
    else:
        assert reply["result"] == {"status": "error", "error": "Device 'Kitchen' already exists"}


async def test_unload(hass, mqtt_mock):
    entry = await setup_integration(hass, mqtt_mock)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert "somfy-zigbee-setup" not in hass.data["frontend_panels"]
    assert DOMAIN not in hass.data
