#!/usr/bin/env python3
"""Set up Somfy Zigbee blinds without the TaHoma apps.

The tool talks to the motor over Bluetooth with the EUI64 and Pin Code from
the sticker on the motor. It can make the motor leave its current Zigbee
network so Zigbee2MQTT can pair it, check whether the motor is calibrated, and
set or fine-tune its limits.

Run it with no arguments for the guided setup, or run one step directly:

  somfy_setup.py pair      --eui64 4CC206FFFE0A1B2C --pin 1A2B3C
  somfy_setup.py calibrate --eui64 4CC206FFFE0A1B2C --pin 1A2B3C
  somfy_setup.py info      --eui64 4CC206FFFE0A1B2C --pin 1A2B3C
  somfy_setup.py qr sticker.jpg

Add --mqtt-host (and --mqtt-user/--mqtt-password if your broker needs them)
to let the tool turn on permit join in Zigbee2MQTT and watch the motor join.
"""
import argparse
import asyncio
import json
import queue
import re
import sys
import threading
import time

from bleak import BleakClient, BleakScanner

SOMFY = 1480  # Somfy's Bluetooth company ID


def uuid(n):
    return f"{n:08x}-cad9-46c6-a2ea-2ca16d57b4a5"


IDENTIFY = uuid(0x00000001)
STOP = uuid(0x00000006)
AUTH = uuid(0x0000000B)
FIRMWARE = uuid(0x00010002)
DIRECTION = uuid(0x00010005)
LIMIT = uuid(0x00010007)
MOVE_DOWN = uuid(0x00010008)
MOVE_UP = uuid(0x00010009)
RANGE = uuid(0x0001000B)
LEAVE = uuid(0x00020001)
CHANNEL = uuid(0x00020002)
# the motor's settings files: commands on one characteristic, result on another
FILE_CMD = uuid(0x00040001)
FILE_STATUS = uuid(0x00040003)
MOTOR_FILE = b"\xc4\x00"      # file 0x00C4, low byte first
FILE_PAUSE = 0.5               # the motor wants a pause between file commands
PAN = uuid(0x00020003)
EUI = uuid(0x00020005)

HEX = "0-9A-Fa-f"

CALIBRATE_HELP = """
  identify                     jog the motor once
  status                       ask Zigbee2MQTT whether the motor is calibrated
  up [n] / down [n]            move the blind, n from 1 to 500 (default 100)
  tilt-up [n] / tilt-down [n]  turn the slats (default 50), add r2 if they don't move
  stop                         stop the motor
  limit up                     save the current position as the top limit
  limit down                   save the current position as the bottom limit
  range start                  start a new range setup (the motor forgets its range)
  range full                   finish the range setup
  direction cw / direction ccw set the turning direction (swap if up moves down)
  type                         read the motor type (Roller, Venetian...) and ranges
  type venetian                set the motor type (a venetian set to Roller has no tilt)
  guide                        the order to do things in
  quit
"""

CALIBRATE_GUIDE = """
  Fine-tuning a limit (the blind keeps working meanwhile):
    move the blind with up/down, then 'limit up' or 'limit down'.

  Setting a motor up from scratch (configStatus even, blind does not move):
    1. up 100 - did the blind go up? If it went down: direction cw
       (or direction ccw if that did not change it), then up 100 again
    2. range start
    3. down 100 until the blind is where the bottom should be
       (use down 20 or down 5 near the end), then: limit down
    4. up 100 until it is where the top should be, then: limit up
    5. status - odd means done

  A venetian whose motor type is Roller has no tilt at all and shows up in
  Zigbee2MQTT as a roller blind: 'type' tells, 'type venetian' changes it
  (power-cycle the motor, re-interview it in Zigbee2MQTT, set the limits
  again). Then the slats range, which is what tilt 0-100% means:
    range start, tilt-down 50 until the slats stop turning,
    tilt-up 50 until they stop the other way, range full

  If the motor disappears from Zigbee2MQTT after step 4, turn on permit join
  and run 'somfy_setup.py pair' again. The limits stay in the motor.
"""


# ---------------------------------------------------------------- sticker

def normalise_eui64(text):
    digits = re.sub(rf"[^{HEX}]", "", text or "").upper()
    return digits if len(digits) == 16 else None


def eui64_from_eui48(eui48):
    digits = re.sub(rf"[^{HEX}]", "", eui48 or "").upper()
    if len(digits) != 12:
        return None
    return digits[:6] + "FFFE" + digits[6:]


def parse_sticker(text):
    """Pull whatever a QR code holds out of its text: EUI64, EUI48, PIN, install code."""
    found = {}
    clean = (text or "").strip()
    labels = (
        ("eui64", ("EUI64", "EUI-64", "Z")),
        ("eui48", ("EUI48", "EUI-48", "MAC", "BLE")),
        ("pin", ("PINCODE", "PIN CODE", "PIN", "P")),
        ("install_code", ("INSTALLCODE", "INSTALL CODE", "IC", "I")),
    )
    for key, names in labels:
        for name in names:
            pattern = rf"(?<![A-Za-z]){re.escape(name)}\s*[:=]\s*([{HEX}]+)"
            m = re.search(pattern, clean, re.I)
            if m and key not in found:
                found[key] = m.group(1).upper()
                break
    # bare numbers, for codes without labels
    for token in re.findall(rf"(?<![{HEX}])[{HEX}]+(?![{HEX}])", clean):
        value = token.upper()
        if len(value) == 16 and "eui64" not in found:
            found["eui64"] = value
        elif len(value) in (32, 36) and "install_code" not in found:
            found["install_code"] = value
        elif len(value) == 12 and "eui48" not in found:
            found["eui48"] = value
    if "eui64" not in found and "eui48" in found:
        found["eui64"] = eui64_from_eui48(found["eui48"])
    return found


def decode_qr(path):
    try:
        import cv2
    except ImportError:
        sys.exit("Reading QR codes needs OpenCV: pip install opencv-python-headless")
    image = cv2.imread(path)
    if image is None:
        sys.exit(f"Could not open {path}")
    detector = cv2.QRCodeDetector()
    text, _, _ = detector.detectAndDecode(image)
    if text:
        return text
    # phone photos are often small or blurry, so try enlarged versions too
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    for scale in (2, 3):
        big = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        for candidate in (big, cv2.threshold(big, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]):
            text, _, _ = detector.detectAndDecode(candidate)
            if text:
                return text
    return ""


# ---------------------------------------------------------------- bluetooth

def pin_candidates(pin):
    # the sticker prints the PIN in hex; older tools sent it as a decimal number
    yield bytes.fromhex(pin)
    if pin.isdigit():
        yield int(pin).to_bytes(3, "little")


def is_my_motor(value, eui64):
    eui = bytes(value[:8])
    return eui64 in (eui.hex().upper(), eui[::-1].hex().upper())


def advertised_eui48(adv):
    """The motors put their Bluetooth address in the advertisement: bytes 1 to 6
    of the Somfy manufacturer data, last byte first."""
    data = bytes(adv.manufacturer_data.get(SOMFY, b""))
    return data[6:0:-1].hex().upper() if len(data) >= 7 else None


def eui48_of(eui64):
    """A Sonesse2 motor's Bluetooth address is its EUI64 without the FFFE in the middle."""
    return eui64[:6] + eui64[10:] if eui64[6:10] == "FFFE" else None


# ---------------------------------------------------------------- the settings files
# CBOR, the subset the motor's settings files use. Each setting is stored as
# [value, description]; a write sends {key: [value]}.

def cbor_decode(data):
    pos = 0

    def length(info):
        nonlocal pos
        if info < 24:
            return info
        size = {24: 1, 25: 2, 26: 4, 27: 8}[info]
        value = int.from_bytes(data[pos:pos + size], "big")
        pos += size
        return value

    def item():
        nonlocal pos
        first = data[pos]
        pos += 1
        major, info = first >> 5, first & 0x1F
        if major == 0:
            return length(info)
        if major == 1:
            return -1 - length(info)
        if major in (2, 3):
            n = length(info)
            raw = data[pos:pos + n]
            pos += n
            return raw.decode("utf-8", "replace") if major == 3 else raw
        if major == 4:
            return [item() for _ in range(length(info))]
        if major == 5:
            out = {}
            for _ in range(length(info)):
                key = item()
                out[key if isinstance(key, str) else repr(key)] = item()
            return out
        if major == 7:
            if info == 20:
                return False
            if info == 21:
                return True
            if info in (22, 23):
                return None
            pos += {25: 2, 26: 4, 27: 8}.get(info, 0)
            return None
        raise ValueError("unsupported CBOR")

    return item()


def cbor_encode(value):
    def head(major, n):
        if n < 24:
            return bytes([(major << 5) | n])
        if n < 256:
            return bytes([(major << 5) | 24, n])
        return bytes([(major << 5) | 25]) + n.to_bytes(2, "big")

    if value is None:
        return b"\xf6"
    if value is True:
        return b"\xf5"
    if value is False:
        return b"\xf4"
    if isinstance(value, int):
        return head(0 if value >= 0 else 1, value if value >= 0 else -1 - value)
    if isinstance(value, str):
        raw = value.encode()
        return head(3, len(raw)) + raw
    if isinstance(value, list):
        return head(4, len(value)) + b"".join(cbor_encode(v) for v in value)
    if isinstance(value, dict):
        return head(5, len(value)) + b"".join(cbor_encode(k) + cbor_encode(v) for k, v in value.items())
    raise ValueError("cannot encode %r" % (value,))


def setting(settings, key):
    entry = settings.get(key) if isinstance(settings, dict) else None
    return entry[0] if isinstance(entry, list) and entry else entry


async def read_eui(client):
    try:
        return await client.read_gatt_char(EUI)
    except Exception:
        return None


async def login(client, pin):
    for pin_bytes in pin_candidates(pin):
        try:
            await client.write_gatt_char(AUTH, pin_bytes, response=True)
            return pin_bytes
        except Exception:
            pass
    return None


class Motor:
    def __init__(self, eui64, pin):
        self.eui64 = eui64
        self.pin = pin
        self.device = None
        self.client = None
        self.closing = False
        self.expect_drop = False

    def lost(self, client):
        if client is self.client and not self.closing and not self.expect_drop:
            print("  (Bluetooth connection lost - it reconnects on the next command)")

    async def find(self, scan_time):
        print(f"Scanning for Somfy motors ({scan_time} s)...")
        try:
            found = await BleakScanner.discover(timeout=scan_time, return_adv=True)
        except Exception as err:
            print("Could not use Bluetooth on this computer:", err)
            print("Check that Bluetooth is turned on and that this program is allowed to use it.")
            return False
        motors = [(adv.rssi, dev) for dev, adv in found.values()
                  if SOMFY in adv.manufacturer_data]
        if not motors:
            print("No Somfy motor found. Close the TaHoma Pro app on your phone and move closer.")
            return False

        motors.sort(key=lambda m: m[0] if m[0] != 127 else -999, reverse=True)
        # the advertisement says which motor is which, so try the sticker's one first
        mine = eui48_of(self.eui64)
        advertised = {dev.address: advertised_eui48(adv) for dev, adv in found.values()}
        motors.sort(key=lambda m: advertised.get(m[1].address) != mine)
        if mine and advertised.get(motors[0][1].address) == mine:
            print(f"The sticker's motor is advertising as {motors[0][1].name or 'unnamed'}")
        for rssi, dev in motors:
            print(f"Checking {dev.name or 'unnamed'} (signal {rssi} dBm)")
            try:
                result = await self.try_motor(dev)
            except Exception as err:
                print("  could not connect:", err)
                continue
            if result == "mine":
                return True
            if result == "refused":
                print("  the motor refused the PIN - check the Pin Code on the sticker")
                return False
        print("No motor matched the EUI64. Check the sticker, or move closer to the blind.")
        return False

    async def try_motor(self, dev):
        client = BleakClient(dev, timeout=20, disconnected_callback=self.lost)
        await client.connect()
        keep = False
        try:
            pin_ok = None
            eui = await read_eui(client)
            if eui is None:
                # in case the motor only answers after the PIN
                pin_ok = await login(client, self.pin)
                eui = await read_eui(client)
            if eui is None or not is_my_motor(eui, self.eui64):
                print("  not this motor")
                return "other"

            if pin_ok is None:
                pin_ok = await login(client, self.pin)
            if pin_ok is None:
                return "refused"
            print("  this is the motor from the sticker, PIN accepted")
            await asyncio.sleep(2)
            self.device, self.client = dev, client
            keep = True
            return "mine"
        finally:
            if not keep:
                await client.disconnect()

    async def ensure(self):
        if self.client is not None and self.client.is_connected:
            return
        print("  reconnecting...")
        client = BleakClient(self.device, timeout=20, disconnected_callback=self.lost)
        await client.connect()
        if await login(client, self.pin) is None:
            await client.disconnect()
            raise RuntimeError("the motor refused the PIN")
        await asyncio.sleep(2)
        self.client = client

    async def write(self, char, data):
        await self.ensure()
        await asyncio.wait_for(self.client.write_gatt_char(char, bytes(data), response=True), 5)

    async def read(self, char):
        await self.ensure()
        return bytes(await asyncio.wait_for(self.client.read_gatt_char(char), 5))

    async def read_motor_file(self):
        """Open the motor settings file for reading, read it, close it."""
        await self.write(FILE_CMD, b"\x00" + MOTOR_FILE + b"\x00")
        await asyncio.sleep(FILE_PAUSE)
        await self.write(FILE_CMD, b"\x03" + MOTOR_FILE)
        await asyncio.sleep(FILE_PAUSE)
        data = await self.read(FILE_CMD)
        await asyncio.sleep(FILE_PAUSE)
        try:
            await self.write(FILE_CMD, b"\x04" + MOTOR_FILE)
        except Exception:
            pass
        if not data:
            raise RuntimeError("the motor sent an empty settings file")
        return cbor_decode(data)

    async def write_motor_setting(self, key, value):
        """Open for writing, send {key: [value]}, close, then ask the motor whether it took it."""
        await self.write(FILE_CMD, b"\x00" + MOTOR_FILE + b"\x01")
        await asyncio.sleep(FILE_PAUSE)
        await self.write(FILE_CMD, b"\x02" + cbor_encode({key: [value]}))
        await asyncio.sleep(FILE_PAUSE)
        await self.write(FILE_CMD, b"\x04")
        for _ in range(5):
            await asyncio.sleep(FILE_PAUSE)
            try:
                status = await self.read(FILE_STATUS)
            except Exception:
                continue
            if len(status) >= 2 and status[0] == 0x05 and status[1] in (0x00, 0x64):
                return True
        return False

    async def leave_network(self):
        # the motor usually drops the Bluetooth link right after this, which is fine
        self.expect_drop = True
        try:
            await self.write(LEAVE, b"\x01")
            print("Leave-network sent.")
        except Exception as err:
            print("Leave-network answered with an error:", err)
            print("It may still have worked.")
        finally:
            await asyncio.sleep(1)
            self.expect_drop = False

    async def close(self):
        self.closing = True
        if self.client is not None and self.client.is_connected:
            await self.client.disconnect()


async def show_info(motor):
    async def get(char):
        try:
            return await motor.read(char)
        except Exception:
            return None

    channel = await get(CHANNEL)
    pan = await get(PAN)
    eui = await get(EUI)
    firmware = await get(FIRMWARE)
    print("  Zigbee channel:", channel[0] if channel else "n/a")
    print("  PAN ID:", f"0x{int.from_bytes(pan[:2], 'little'):04X}" if pan and len(pan) >= 2 else "n/a")
    print("  EUI64:", eui[:8][::-1].hex().upper() if eui and len(eui) >= 8 else "n/a")
    print("  Firmware:", firmware.decode("ascii", "replace").strip("\x00 ") if firmware else "n/a")


# ---------------------------------------------------------------- zigbee2mqtt

class Zigbee2MQTT:
    """The little we need from Zigbee2MQTT, over its MQTT topics."""

    def __init__(self, host, port, user, password, base):
        import paho.mqtt.client as mqtt

        self.base = base.strip("/")
        self.inbox = queue.Queue()
        self.devices = []
        self.connected = threading.Event()
        self.refused = None
        self.reads = 0
        try:
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except AttributeError:  # paho-mqtt 1.x
            self.client = mqtt.Client()
        if user:
            self.client.username_pw_set(user, password or None)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        try:
            self.client.connect(host, port, 30)
        except Exception as err:
            raise RuntimeError(f"could not reach the MQTT broker at {host}:{port} ({err})")
        self.client.loop_start()
        if not self.connected.wait(10):
            self.close()
            raise RuntimeError(f"nothing answered like an MQTT broker at {host}:{port} - "
                               "MQTT is usually on port 1883 (8123 is Home Assistant's web page)")
        if self.refused:
            self.close()
            raise RuntimeError(f"the MQTT broker refused the login ({self.refused}) - check the username and password")
        time.sleep(1)  # let the retained device list arrive

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        failed = getattr(rc, "is_failure", None)  # paho 2.x gives a reason code object
        if failed is None:
            failed = rc != 0  # paho 1.x gives a number, 0 means accepted
        if failed:
            self.refused = str(rc)
        else:
            client.subscribe(f"{self.base}/bridge/event")
            client.subscribe(f"{self.base}/bridge/devices")
            client.subscribe(f"{self.base}/bridge/logging")
            client.subscribe(f"{self.base}/+")
        self.connected.set()

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except Exception:
            return
        if msg.topic == f"{self.base}/bridge/devices":
            if isinstance(payload, list):
                self.devices = payload
            return
        self.inbox.put((msg.topic, payload))

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()

    def ieee(self, eui64):
        return "0x" + eui64.lower()

    def device(self, eui64):
        for dev in self.devices:
            if dev.get("ieee_address") == self.ieee(eui64):
                return dev
        return None

    def permit_join(self, seconds=254):
        self.client.publish(f"{self.base}/bridge/request/permit_join", json.dumps({"time": seconds}))

    def drain(self):
        while True:
            try:
                self.inbox.get_nowait()
            except queue.Empty:
                return

    def next_message(self, timeout):
        try:
            return self.inbox.get(timeout=timeout)
        except queue.Empty:
            return None

    def wait_for_join(self, eui64, timeout=180):
        """Wait until Zigbee2MQTT has interviewed the motor. Returns True on success."""
        deadline = time.time() + timeout
        ieee = self.ieee(eui64)
        while time.time() < deadline:
            msg = self.next_message(min(5, max(0.1, deadline - time.time())))
            if msg is None:
                continue
            topic, payload = msg
            if topic != f"{self.base}/bridge/event" or not isinstance(payload, dict):
                continue
            data = payload.get("data") or {}
            if data.get("ieee_address") != ieee:
                continue
            kind = payload.get("type")
            if kind == "device_joined":
                print("  joined, Zigbee2MQTT is interviewing it...")
            elif kind == "device_announce":
                print("  the motor is back on the network")
                return True
            elif kind == "device_interview":
                status = data.get("status")
                if status == "successful":
                    definition = data.get("definition") or {}
                    name = definition.get("description") or "no matching definition yet"
                    print(f"  interview done ({name})")
                    return True
                if status == "failed":
                    print("  the interview failed - try again, or move the laptop and the Zigbee adapter closer")
                    return False
        return False

    def config_status(self, eui64, timeout=15):
        """Read configStatus from endpoint 1. Returns the number, or None.

        Newer Zigbee2MQTT versions publish the result as state when asked to
        (state_property); older ones only log it, so the log is watched too.
        """
        dev = self.device(eui64)
        name = dev["friendly_name"] if dev else self.ieee(eui64)
        # Zigbee2MQTT drops a log line identical to the previous one, so alternate the request
        self.reads += 1
        attributes = [7] if self.reads % 2 else [7, 0]
        self.drain()
        self.client.publish(
            f"{self.base}/{name}/1/set",
            json.dumps({"read": {"cluster": 258, "attributes": attributes, "state_property": "config_status"}}),
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self.next_message(min(2, max(0.1, deadline - time.time())))
            if msg is None:
                continue
            topic, payload = msg
            if not isinstance(payload, dict):
                continue
            result = None
            if topic == f"{self.base}/{name}":
                result = payload.get("config_status")
            elif topic == f"{self.base}/bridge/logging":
                found = re.search(r"Read result of '258': (\{.*\})", payload.get("message", ""))
                if found:
                    try:
                        result = json.loads(found.group(1))
                    except ValueError:
                        result = None
            if isinstance(result, dict) and "configStatus" in result:
                return int(result["configStatus"])
        return None


def describe_status(value):
    if value is None:
        return "unknown"
    if value % 2 == 1:
        return f"{value} (odd) - the motor has its limits and will move"
    return f"{value} (even) - the motor has no limits and will ignore move commands"


# ---------------------------------------------------------------- talking to the user

async def ask(prompt):
    # input() runs in its own thread so the Bluetooth link stays alive while you type
    loop = asyncio.get_running_loop()
    answer = loop.create_future()

    def worker():
        try:
            line = input(prompt)
        except EOFError:
            line = None
        loop.call_soon_threadsafe(lambda: answer.done() or answer.set_result(line))

    threading.Thread(target=worker, daemon=True).start()
    result = await answer
    if result is None:
        # the input was closed (Ctrl+D or a finished script), so stop cleanly
        raise EOFError
    return result.strip()


async def confirm(question):
    answer = await ask(f"  {question} Type yes to continue: ")
    return answer.lower() == "yes"


async def yes_no(question):
    while True:
        answer = (await ask(f"{question} (yes/no): ")).lower()
        if answer in ("yes", "y"):
            return True
        if answer in ("no", "n"):
            return False


async def sticker_from_user(args):
    """Get the EUI64 and PIN from the command line, a QR photo, or by asking."""
    eui64 = normalise_eui64(args.eui64) if getattr(args, "eui64", None) else None
    pin = (args.pin or "").strip() if getattr(args, "pin", None) else None

    if not eui64 and not pin and sys.stdin.isatty():
        print("I need the EUI64 and the Pin Code printed on the sticker on the motor.")
        path = await ask("Path to a photo of the QR code (or press Enter to type the values): ")
        if path:
            found = parse_sticker(decode_qr(path.strip().strip('"\'')))
            if found:
                print("  found in the QR code:", ", ".join(f"{k} {v}" for k, v in found.items()))
            else:
                print("  no usable QR code in that photo")
            eui64 = found.get("eui64") or eui64
            pin = found.get("pin") or pin

    while not eui64:
        eui64 = normalise_eui64(await ask("EUI64 (16 characters, e.g. 4CC206FFFE0A1B2C): "))
    while not pin or not re.fullmatch(rf"[{HEX}]{{6}}", pin):
        pin = await ask("Pin Code (6 characters): ")
    return eui64, pin


def mqtt_from_args(args):
    """Connect to Zigbee2MQTT if the user gave us the broker. Returns None otherwise."""
    if not getattr(args, "mqtt_host", None):
        return None
    print(f"Connecting to the MQTT broker at {args.mqtt_host}...")
    z2m = Zigbee2MQTT(args.mqtt_host, args.mqtt_port, args.mqtt_user, args.mqtt_password, args.base_topic)
    print(f"  connected, Zigbee2MQTT knows {len(z2m.devices)} devices")
    return z2m


# ---------------------------------------------------------------- the steps

async def step_pair(motor, z2m, eui64):
    """Make the motor leave its network and join Zigbee2MQTT."""
    if z2m is not None:
        existing = z2m.device(eui64)
        if existing is not None:
            print(f"This motor is already in Zigbee2MQTT as '{existing['friendly_name']}'.")
            if not await yes_no("Make it leave and join again anyway?"):
                return True
        z2m.drain()
        z2m.permit_join(254)
        print("Permit join is on in Zigbee2MQTT for 4 minutes.")
    else:
        print("Turn on 'Permit join (All)' in Zigbee2MQTT now.")
        await ask("Press Enter when it is on: ")

    await motor.leave_network()

    if z2m is None:
        print("Watch the Zigbee2MQTT log: within a minute it should say the motor joined")
        print(f"and was interviewed. Its address there is {z2m_ieee(eui64)}.")
        return await yes_no("Did the motor join?")

    print("Waiting for the motor to join (up to 3 minutes)...")
    if z2m.wait_for_join(eui64):
        return True
    print("Zigbee2MQTT did not report the motor. Check that permit join was on and the")
    print("Zigbee adapter is in range, then run 'somfy_setup.py pair' again.")
    return False


def z2m_ieee(eui64):
    return "0x" + eui64.lower()


async def step_status(z2m, eui64):
    """Find out whether the motor is calibrated. Returns the configStatus number or None."""
    if z2m is not None:
        print("Asking the motor whether it is calibrated...")
        value = z2m.config_status(eui64)
        if value is not None:
            print("  configStatus:", describe_status(value))
            return value
        print("  no answer through Zigbee2MQTT")
    print("In Home Assistant, publish this MQTT message (Settings > Devices & services > MQTT > Configure):")
    print(f"  topic:   zigbee2mqtt/{z2m_ieee(eui64)}/1/set")
    print('  payload: {"read":{"cluster":258,"attributes":[7]}}')
    print("The Zigbee2MQTT log then shows: Read result of '258': {\"configStatus\":N}")
    while True:
        try:
            answer = await ask("Type the number N (or press Enter to skip): ")
        except EOFError:
            return None
        if not answer:
            return None
        if answer.isdigit():
            value = int(answer)
            print("  configStatus:", describe_status(value))
            return value


def step_arg(args, default):
    numbers = [a for a in args if a.isdigit()]
    n = int(numbers[0]) if numbers else default
    if not 1 <= n <= 500:
        raise ValueError("n must be between 1 and 500")
    return n


async def calibrate_command(motor, z2m, line):
    words = line.lower().split()
    if not words:
        return True
    cmd, args = words[0], words[1:]

    if cmd in ("quit", "exit"):
        return False
    if cmd == "help":
        print(CALIBRATE_HELP)
    elif cmd == "guide":
        print(CALIBRATE_GUIDE)
    elif cmd == "identify":
        await motor.write(IDENTIFY, b"\x01")
        print("  sent - the motor should jog")
    elif cmd == "info":
        await show_info(motor)
    elif cmd == "status":
        await step_status(z2m, motor.eui64)
    elif cmd in ("up", "down"):
        n = step_arg(args, 100)
        await motor.write(MOVE_UP if cmd == "up" else MOVE_DOWN, n.to_bytes(2, "little"))
        print(f"  moving {cmd} ({n})")
    elif cmd in ("tilt-up", "tilt-down"):
        n = step_arg(args, 50)
        release = 2 if "r2" in args else 0
        payload = n.to_bytes(2, "little") + bytes([release, 1, 2])
        await motor.write(MOVE_UP if cmd == "tilt-up" else MOVE_DOWN, payload)
        print(f"  tilting {cmd[5:]} ({n})")
    elif cmd == "stop":
        await motor.write(STOP, b"\x01")
        print("  stop sent")
    elif cmd == "limit" and args[:1] in (["up"], ["down"]):
        name = "top" if args[0] == "up" else "bottom"
        if await confirm(f"Save the current position as the {name} limit?"):
            await motor.write(LIMIT, b"\x00" if args[0] == "up" else b"\x01")
            print(f"  {name} limit saved")
        else:
            print("  cancelled")
    elif cmd == "range" and args[:1] == ["start"]:
        if await confirm("The motor forgets its range until you finish the setup. Start?"):
            await motor.write(RANGE, b"\x00\x00")
            print("  range setup started")
        else:
            print("  cancelled")
    elif cmd == "range" and args[:1] == ["full"]:
        if await confirm("Finish the range setup?"):
            await motor.write(RANGE, b"\x00\x01")
            print("  range setup finished")
        else:
            print("  cancelled")
    elif cmd == "direction" and args[:1] in (["cw"], ["ccw"]):
        await motor.write(DIRECTION, b"\x01" if args[0] == "cw" else b"\x00")
        print(f"  direction set to {'clockwise' if args[0] == 'cw' else 'counter-clockwise'}")
    elif cmd == "type" and not args:
        settings = await motor.read_motor_file()
        for key in ("Application", "LiftRange", "TiltRange", "ReversedDirection"):
            if key in settings:
                print(f"  {key}: {setting(settings, key)}")
        others = [k for k in settings if k not in ("Application", "LiftRange", "TiltRange", "ReversedDirection")]
        if others:
            print("  also in the file:", ", ".join(others))
    elif cmd == "type" and args[:1] in (["venetian"], ["roller"]):
        name = args[0].capitalize()
        if await confirm(f"Set the motor type to {name}? The motor forgets its limits, needs a power cycle "
                         "and a new interview in Zigbee2MQTT. Continue?"):
            ok = await motor.write_motor_setting("Application", name)
            print(f"  motor type {name} written" + (", the motor confirmed" if ok else ", the motor did not confirm (it may still have taken it)"))
            print("  Now cut the motor's power for 10 seconds, press Interview on its Zigbee2MQTT page,")
            print("  then set the limits again (and the slats range for a venetian).")
        else:
            print("  cancelled")
    else:
        print("  unknown command - type help")
    return True


async def step_calibrate(motor, z2m):
    print("Calibration. Type help for the commands, guide for the order to do things in.")
    while True:
        try:
            line = await ask("somfy> ")
        except EOFError:
            return
        try:
            if not await calibrate_command(motor, z2m, line):
                return
        except Exception as err:
            print("  error:", err)


# ---------------------------------------------------------------- commands

async def run_guided(args):
    print("Somfy Zigbee setup\n")
    eui64, pin = await sticker_from_user(args)
    z2m = None
    motor = Motor(eui64, pin)
    try:
        if not await motor.find(args.scan_time):
            return
        await motor.write(IDENTIFY, b"\x01")
        if not await yes_no("The blind should have jogged just now. Did it?"):
            print("Then this is not the blind you think. Check the sticker and try again.")
            return

        if getattr(args, "mqtt_host", None):
            z2m = mqtt_from_args(args)
        elif await yes_no("Can I talk to your MQTT broker to turn on permit join and watch the motor join?"):
            args.mqtt_host = await ask("Broker host or IP (e.g. 192.168.1.10): ")
            port = await ask("Port [1883]: ")
            args.mqtt_port = int(port) if port.isdigit() else 1883
            args.mqtt_user = await ask("Username (Enter if none): ") or None
            args.mqtt_password = await ask("Password (Enter if none): ") or None
            try:
                z2m = mqtt_from_args(args)
            except Exception as err:
                print("  ", err)
                print("  continuing without it")

        if z2m is not None and z2m.device(eui64) is not None:
            print(f"\nThis motor is already in Zigbee2MQTT as '{z2m.device(eui64)['friendly_name']}'.")
            paired = True
        elif z2m is None and await yes_no("\nIs this motor already in Zigbee2MQTT?"):
            paired = True
        else:
            print("\nStep 1: pairing with Zigbee2MQTT")
            paired = await step_pair(motor, z2m, eui64)
        if not paired:
            return

        print("\nStep 2: is the motor calibrated?")
        if z2m is not None:
            await asyncio.sleep(3)
        status = await step_status(z2m, eui64)

        print("\nStep 3: calibration")
        if status is not None and status % 2 == 1:
            if await yes_no("The motor is calibrated. Do you want to fine-tune a limit now?"):
                await step_calibrate(motor, z2m)
        else:
            print("The motor needs its limits. Type guide for the steps.")
            await step_calibrate(motor, z2m)

        print("\nDone. If Zigbee2MQTT shows the motor as 'Not supported', copy the converter")
        print("from the zigbee2mqtt folder of this project into external_converters and")
        print("restart Zigbee2MQTT. The README has the details.")
    finally:
        await motor.close()
        if z2m is not None:
            z2m.close()


async def run_pair(args):
    eui64, pin = await sticker_from_user(args)
    z2m = mqtt_from_args(args)
    motor = Motor(eui64, pin)
    try:
        if not await motor.find(args.scan_time):
            return
        if await step_pair(motor, z2m, eui64) and z2m is not None:
            await asyncio.sleep(3)
            await step_status(z2m, eui64)
    finally:
        await motor.close()
        if z2m is not None:
            z2m.close()


async def run_calibrate(args):
    eui64, pin = await sticker_from_user(args)
    z2m = mqtt_from_args(args)
    motor = Motor(eui64, pin)
    try:
        if not await motor.find(args.scan_time):
            return
        await step_calibrate(motor, z2m)
    finally:
        await motor.close()
        if z2m is not None:
            z2m.close()


async def run_info(args):
    eui64, pin = await sticker_from_user(args)
    motor = Motor(eui64, pin)
    try:
        if await motor.find(args.scan_time):
            await show_info(motor)
    finally:
        await motor.close()


def run_qr(args):
    text = decode_qr(args.photo)
    if not text:
        sys.exit("No QR code found in that photo. Try a sharper, closer photo.")
    print("QR code text:", text)
    found = parse_sticker(text)
    if not found:
        print("Nothing recognisable in it. Please open an issue with the text above (remove the PIN first).")
        return
    for key, value in found.items():
        print(f"  {key}: {value}")


def add_motor_args(parser):
    parser.add_argument("--eui64", help="EUI64 from the motor sticker")
    parser.add_argument("--pin", help="Pin Code from the motor sticker")
    parser.add_argument("--scan-time", type=int, default=15, help="how many seconds to scan (default 15)")


def add_mqtt_args(parser):
    parser.add_argument("--mqtt-host", help="MQTT broker host or IP (lets the tool talk to Zigbee2MQTT)")
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--mqtt-user")
    parser.add_argument("--mqtt-password")
    parser.add_argument("--base-topic", default="zigbee2mqtt", help="Zigbee2MQTT base topic (default zigbee2mqtt)")


def main():
    parser = argparse.ArgumentParser(description="Set up Somfy Zigbee blinds without the TaHoma apps.")
    add_motor_args(parser)
    add_mqtt_args(parser)
    sub = parser.add_subparsers(dest="command")

    for name, text in (("pair", "make the motor leave its network and join Zigbee2MQTT"),
                       ("calibrate", "set or fine-tune the limits"),
                       ("info", "show the motor's Zigbee channel, PAN ID, EUI64 and firmware")):
        p = sub.add_parser(name, help=text)
        add_motor_args(p)
        if name != "info":
            add_mqtt_args(p)
    p = sub.add_parser("qr", help="read the sticker's QR code from a photo")
    p.add_argument("photo")

    args = parser.parse_args()
    if args.command == "qr":
        run_qr(args)
        return
    if args.command in ("pair", "calibrate", "info") and bool(args.eui64) != bool(args.pin):
        parser.error("give both --eui64 and --pin, or neither")
    if args.eui64 and not normalise_eui64(args.eui64):
        parser.error("the EUI64 should be 16 hex characters, e.g. 4CC206FFFE0A1B2C")
    if args.pin and not re.fullmatch(rf"[{HEX}]{{6}}", args.pin.strip()):
        parser.error("the PIN should be the 6 characters printed on the sticker")

    runner = {"pair": run_pair, "calibrate": run_calibrate, "info": run_info}.get(args.command, run_guided)
    try:
        asyncio.run(runner(args))
    except (KeyboardInterrupt, EOFError):
        print()
    except Exception as err:
        print("Error:", err)


if __name__ == "__main__":
    main()
