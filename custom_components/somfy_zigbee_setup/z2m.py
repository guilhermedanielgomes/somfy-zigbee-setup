"""The little we need from Zigbee2MQTT, over its MQTT topics."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant, callback

from .const import READ_TIMEOUT

_LOGGER = logging.getLogger(__name__)

EVENT_TYPES = ("device_joined", "device_interview", "device_announce", "device_leave")
READ_RESULT = re.compile(r"Read result of '258': (\{.*\})")


class Zigbee2MQTT:
    """Keeps the device list, forwards join events and reads configStatus."""

    def __init__(self, hass: HomeAssistant, base_topic: str) -> None:
        self.hass = hass
        self.base = base_topic.strip("/")
        self.devices: list[dict] = []
        self._unsubs: list[Callable[[], None]] = []
        self._listeners: list[Callable[[dict], None]] = []
        self._reads = 0

    async def async_start(self) -> None:
        self._unsubs.append(
            await mqtt.async_subscribe(self.hass, f"{self.base}/bridge/devices", self._on_devices)
        )
        self._unsubs.append(
            await mqtt.async_subscribe(self.hass, f"{self.base}/bridge/event", self._on_event)
        )

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    # ----------------------------------------------------------- incoming

    @callback
    def _on_devices(self, msg: mqtt.ReceiveMessage) -> None:
        payload = _json(msg.payload)
        if isinstance(payload, list):
            self.devices = payload
            self._notify({"type": "devices"})

    @callback
    def _on_event(self, msg: mqtt.ReceiveMessage) -> None:
        payload = _json(msg.payload)
        if isinstance(payload, dict) and payload.get("type") in EVENT_TYPES:
            data = payload.get("data") or {}
            self._notify(
                {
                    "type": payload["type"],
                    "ieee": data.get("ieee_address"),
                    "friendly_name": data.get("friendly_name"),
                    "status": data.get("status"),
                    "supported": data.get("supported"),
                    "description": (data.get("definition") or {}).get("description"),
                }
            )

    def _notify(self, event: dict) -> None:
        for listener in list(self._listeners):
            listener(event)

    @callback
    def add_listener(self, listener: Callable[[dict], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        @callback
        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    # ----------------------------------------------------------- devices

    def somfy_devices(self) -> list[dict]:
        """The Somfy motors Zigbee2MQTT knows, in a shape the panel can show."""
        result = []
        for dev in self.devices:
            if dev.get("type") == "Coordinator":
                continue
            manufacturer = str(dev.get("manufacturer") or "")
            definition = dev.get("definition") or {}
            vendor = str(definition.get("vendor") or "")
            if "somfy" not in manufacturer.lower() and "somfy" not in vendor.lower():
                continue
            result.append(
                {
                    "ieee": dev.get("ieee_address"),
                    "friendly_name": dev.get("friendly_name"),
                    "model_id": dev.get("model_id"),
                    "supported": bool(dev.get("supported")),
                    "description": definition.get("description"),
                    "source": definition.get("source"),
                }
            )
        return result

    def friendly_name(self, ieee: str) -> str:
        for dev in self.devices:
            if dev.get("ieee_address") == ieee:
                return str(dev.get("friendly_name") or ieee)
        return ieee

    # ----------------------------------------------------------- outgoing

    async def async_permit_join(self, seconds: int = 254) -> None:
        await mqtt.async_publish(
            self.hass, f"{self.base}/bridge/request/permit_join", json.dumps({"time": seconds})
        )

    async def async_config_status(self, ieee: str) -> int | None:
        """Read configStatus (cluster 258, attribute 7) from endpoint 1.

        Newer Zigbee2MQTT versions publish the result as state when asked to
        (state_property); older ones only log it, so the log is watched too.
        Zigbee2MQTT drops a log line identical to the previous one, so the
        request alternates between one and two attributes.
        """
        name = self.friendly_name(ieee)
        future: asyncio.Future[int] = self.hass.loop.create_future()

        def done(result) -> None:
            if isinstance(result, dict) and "configStatus" in result and not future.done():
                try:
                    future.set_result(int(result["configStatus"]))
                except (TypeError, ValueError):
                    pass

        @callback
        def on_state(msg: mqtt.ReceiveMessage) -> None:
            payload = _json(msg.payload)
            if isinstance(payload, dict):
                done(payload.get("somfy_config_status"))

        @callback
        def on_log(msg: mqtt.ReceiveMessage) -> None:
            payload = _json(msg.payload)
            if isinstance(payload, dict):
                found = READ_RESULT.search(str(payload.get("message") or ""))
                if found:
                    done(_json(found.group(1)))

        unsub_state = await mqtt.async_subscribe(self.hass, f"{self.base}/{name}", on_state)
        unsub_log = await mqtt.async_subscribe(self.hass, f"{self.base}/bridge/logging", on_log)
        try:
            self._reads += 1
            attributes = [7] if self._reads % 2 else [7, 0]
            await mqtt.async_publish(
                self.hass,
                f"{self.base}/{name}/1/set",
                json.dumps(
                    {"read": {"cluster": 258, "attributes": attributes, "state_property": "somfy_config_status"}}
                ),
            )
            return await asyncio.wait_for(future, READ_TIMEOUT)
        except asyncio.TimeoutError:
            return None
        finally:
            unsub_state()
            unsub_log()


def _json(text: str):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None
