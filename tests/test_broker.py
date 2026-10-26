"""The same read, but through a real MQTT broker started inside the test.

The mocked MQTT in test_integration.py delivers messages the moment a topic is
subscribed. Home Assistant's real client sends subscriptions to the broker a
little later, which is how the first version lost Zigbee2MQTT's answer: it
subscribed to the log, published the read, and the answer came back before the
broker knew about the subscription. Zigbee2MQTT is faked here and answers
immediately, like the real one does.
"""

import asyncio
import json
import socket

import pytest
from amqtt.broker import Broker
from amqtt.client import MQTTClient
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.somfy_zigbee_setup.const import DOMAIN

IEEE = "0x4cc206fffe7012b2"
DEVICES = [
    {"ieee_address": "0x00124b0012345678", "friendly_name": "Coordinator", "type": "Coordinator"},
    {"ieee_address": IEEE, "friendly_name": IEEE, "type": "Router", "manufacturer": "SOMFY",
     "model_id": "Sonesse2 40 Zigbee Venetian", "supported": True,
     "definition": {"vendor": "Somfy", "description": "Venetian", "source": "external"}},
]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def fake_zigbee2mqtt(client, seen):
    """Answers attribute reads in the log, the way Zigbee2MQTT 2.7 does."""
    await client.subscribe([("zigbee2mqtt/+/1/set", 0)])
    await client.publish("zigbee2mqtt/bridge/devices", json.dumps(DEVICES).encode(), retain=True)
    while True:
        message = await client.deliver_message()
        topic = message.publish_packet.variable_header.topic_name
        payload = json.loads(message.publish_packet.payload.data.decode())
        seen.append((topic, payload))
        if "read" in payload:
            result = {"configStatus": 127}
            if payload["read"]["attributes"] != [7]:
                result = {"windowCoveringType": 8, "configStatus": 127}
            line = {"level": "info", "namespace": "zhc:tz",
                    "message": f"Read result of '258': {json.dumps(result, separators=(',', ':'))}"}
            await client.publish("zigbee2mqtt/bridge/logging", json.dumps(line).encode())


@pytest.mark.usefixtures("mock_hass_config")
async def test_config_status_through_a_real_broker(hass, hass_ws_client):
    port = free_port()
    broker = Broker({
        "listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
        "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}},
    })
    await broker.start()
    z2m = MQTTClient(config={"auto_reconnect": False})
    await z2m.connect(f"mqtt://127.0.0.1:{port}/")
    seen = []
    z2m_task = asyncio.ensure_future(fake_zigbee2mqtt(z2m, seen))
    try:
        mqtt_entry = MockConfigEntry(domain="mqtt", data={"broker": "127.0.0.1", "port": port})
        mqtt_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(mqtt_entry.entry_id)
        await hass.async_block_till_done()

        entry = MockConfigEntry(domain=DOMAIN, data={"base_topic": "zigbee2mqtt"}, title="Somfy Zigbee Setup")
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await asyncio.sleep(1)  # the retained device list takes a moment

        client = await hass_ws_client(hass)
        await client.send_json({"id": 1, "type": f"{DOMAIN}/devices"})
        reply = await client.receive_json()
        assert [d["ieee"] for d in reply["result"]["devices"]] == [IEEE]

        # twice: the second read asks for two attributes, so the log line differs
        for n in (2, 3):
            await client.send_json({"id": n, "type": f"{DOMAIN}/config_status", "ieee": IEEE})
            reply = await client.receive_json()
            assert reply["success"], reply
            assert reply["result"] == {"config_status": 127}, (reply, seen)
        assert seen[0][0] == f"zigbee2mqtt/{IEEE}/1/set"
        assert seen[0][1]["read"] == {"cluster": 258, "attributes": [7], "state_property": "somfy_config_status"}
        assert seen[1][1]["read"]["attributes"] == [7, 0]
    finally:
        z2m_task.cancel()
        await z2m.disconnect()
        await broker.shutdown()
