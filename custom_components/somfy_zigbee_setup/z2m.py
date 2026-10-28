"""The little we need from Zigbee2MQTT, over its MQTT topics."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant, callback

from .const import READ_TIMEOUT, RENAME_TIMEOUT

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
        self._read_lock = asyncio.Lock()
        self._pending: asyncio.Future[int] | None = None
        self._renames: dict[str, asyncio.Future[dict]] = {}
        self._transactions = 0

    async def async_start(self) -> None:
        # Home Assistant sends subscriptions to the broker a moment after
        # async_subscribe returns, so anything we must not miss is subscribed
        # here, once, and not right before it is needed. The log is where
        # Zigbee2MQTT reports attribute reads.
        for topic, handler in (
            (f"{self.base}/bridge/devices", self._on_devices),
            (f"{self.base}/bridge/event", self._on_event),
            (f"{self.base}/bridge/logging", self._on_log),
            (f"{self.base}/bridge/response/device/rename", self._on_rename),
        ):
            self._unsubs.append(await mqtt.async_subscribe(self.hass, topic, handler))

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

    @callback
    def _on_log(self, msg: mqtt.ReceiveMessage) -> None:
        # Only looked at while a read is waiting for its answer
        if self._pending is None:
            return
        payload = _json(msg.payload)
        if isinstance(payload, dict):
            found = READ_RESULT.search(str(payload.get("message") or ""))
            if found:
                self._deliver(_json(found.group(1)))

    @callback
    def _on_rename(self, msg: mqtt.ReceiveMessage) -> None:
        payload = _json(msg.payload)
        if not isinstance(payload, dict):
            return
        future = self._renames.get(str(payload.get("transaction")))
        if future is not None and not future.done():
            future.set_result(payload)

    def _deliver(self, result) -> None:
        future = self._pending
        if future is None or future.done():
            return
        if isinstance(result, dict) and "configStatus" in result:
            try:
                future.set_result(int(result["configStatus"]))
            except (TypeError, ValueError):
                pass

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
            # Somfy also makes remotes, plugs and sensors. Motors are mains powered,
            # so they are routers; a known device is a motor when it exposes a cover,
            # an unknown router is kept, it is probably a motor waiting for its converter.
            if dev.get("type") != "Router":
                continue
            exposes = definition.get("exposes")
            if exposes and not any(e.get("type") == "cover" for e in exposes if isinstance(e, dict)):
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

    async def async_interview(self, ieee: str) -> None:
        """Ask Zigbee2MQTT to interview the device again (its model may have changed)."""
        await mqtt.async_publish(
            self.hass,
            f"{self.base}/bridge/request/device/interview",
            json.dumps({"id": self.friendly_name(ieee)}),
        )

    async def async_rename(self, ieee: str, name: str) -> dict:
        """Give the device a friendly name in Zigbee2MQTT (and so in Home Assistant).

        Returns {"status": "ok"} or {"status": "error", "error": "..."} as
        Zigbee2MQTT answers; it checks the name itself (no '#', '+', '/', unique).
        """
        self._transactions += 1
        transaction = f"somfy-{self._transactions}"
        future: asyncio.Future[dict] = self.hass.loop.create_future()
        self._renames[transaction] = future
        try:
            await mqtt.async_publish(
                self.hass,
                f"{self.base}/bridge/request/device/rename",
                json.dumps(
                    {
                        "from": self.friendly_name(ieee),
                        "to": name,
                        "homeassistant_rename": True,
                        "transaction": transaction,
                    }
                ),
            )
            answer = await asyncio.wait_for(future, RENAME_TIMEOUT)
        except asyncio.TimeoutError:
            return {"status": "error", "error": "Zigbee2MQTT did not answer"}
        finally:
            self._renames.pop(transaction, None)
        if answer.get("status") == "ok":
            return {"status": "ok"}
        return {"status": "error", "error": str(answer.get("error") or "Zigbee2MQTT refused the name")}

    async def async_config_status(self, ieee: str) -> int | None:
        """Read configStatus (cluster 258, attribute 7) from endpoint 1.

        Zigbee2MQTT logs the result ("Read result of '258': {...}"), and
        newer versions also publish it as state when asked to
        (state_property). Both are watched. Zigbee2MQTT drops a log line
        identical to the previous one, so the request alternates between
        one and two attributes. One read at a time, because the log line
        does not say which device answered.
        """
        async with self._read_lock:
            name = self.friendly_name(ieee)
            self._pending = self.hass.loop.create_future()

            @callback
            def on_state(msg: mqtt.ReceiveMessage) -> None:
                payload = _json(msg.payload)
                if isinstance(payload, dict):
                    self._deliver(payload.get("somfy_config_status"))

            unsub_state = await mqtt.async_subscribe(self.hass, f"{self.base}/{name}", on_state)
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
                return await asyncio.wait_for(self._pending, READ_TIMEOUT)
            except asyncio.TimeoutError:
                return None
            finally:
                unsub_state()
                self._pending = None


def _json(text: str):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None
