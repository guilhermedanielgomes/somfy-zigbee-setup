# somfy-zigbee-setup

Somfy Zigbee blinds in Home Assistant, through Zigbee2MQTT, without the TaHoma hub, the TaHoma apps or a Somfy account.

I have Somfy Sonesse2 40 Zigbee motors on venetian blinds. They came with a TaHoma switch, and the only way into Home Assistant was the cloud. The motors themselves are ordinary Zigbee 3.0 devices though, and they also have Bluetooth, which is what the TaHoma Pro installer app uses to set them up. This project uses that Bluetooth link from a laptop instead of the app.

What is here:

- A Home Assistant integration (installable through HACS) that adds a **Somfy setup** page to the sidebar. Open it on the phone or laptop that is next to the blind: the browser does the Bluetooth part, Home Assistant does the Zigbee2MQTT part. Nothing to install on the phone.
- `somfy_setup.py`, the same thing as a command line tool for macOS, Windows and Linux, for people whose Home Assistant is not on https (see below) or who don't use Home Assistant at all.
- `zigbee2mqtt/somfy_sonesse2_40_venetian.mjs`, an external converter for Zigbee2MQTT. Without it the venetian motor shows up as "Not supported" and its buttons do nothing, because Zigbee2MQTT talks to the wrong endpoint.

Nothing here needs the programming button on the motor head. Mine is hidden inside the headrail and I was not going to take the blinds down for it.

## Before you start

Take a sharp photo of the sticker on each motor before you install the blind. You need two things from it:

- **EUI64**, 16 characters, like `4CC206FFFE0A1B2C`. This is the motor's Zigbee address, it is also how it will be named in Zigbee2MQTT (`0x4cc206fffe0a1b2c`).
- **Pin Code**, 6 characters. This is the Bluetooth password. Treat it like one: anyone within Bluetooth range who knows it can drive the blind.

The sticker also has an Install Code and an EUI48. You do not need them.

Do not press **Reset device** in TaHoma Pro. It is a factory reset and the motor forgets its limits. You do not have to delete the motor from the TaHoma app either, the tool takes it off that network itself. (I did delete mine first, and it arrived in Zigbee2MQTT without limits. I do not know yet which of the two caused that, so the tool checks and calibrates if needed.)

## The Home Assistant integration

### What it needs

- Zigbee2MQTT and the MQTT integration, which you have if Zigbee2MQTT devices show up in Home Assistant.
- **Home Assistant reachable over `https://`.** Browsers only allow Bluetooth and the camera on https pages. The Home Assistant Cloud address, a Let's Encrypt certificate (DuckDNS add-on, NGINX proxy add-on) or the Tailscale add-on's HTTPS proxy all work. A self-signed certificate does not. For a quick try on plain http, Chrome has a developer switch: open `chrome://flags/#unsafely-treat-insecure-origin-as-secure`, add your Home Assistant address, enable it and relaunch.
- **Chrome or Edge** on Android, Windows, macOS or Linux, opened on the Home Assistant web address, not inside the Home Assistant companion app (its built-in browser has no Web Bluetooth). iPhones: Safari and every other iOS browser lack Web Bluetooth; the Bluefy app is the exception, or use the command line tool.

### Install

Through HACS: HACS > ⋮ > Custom repositories, add `https://github.com/guilhermedanielgomes/somfy-zigbee-setup` as an Integration, then download **Somfy Zigbee Setup** and restart Home Assistant.

By hand: copy `custom_components/somfy_zigbee_setup` into `/config/custom_components/` and restart Home Assistant.

Then Settings > Devices & services > Add integration > **Somfy Zigbee Setup**. The only setting is the Zigbee2MQTT base topic (`zigbee2mqtt` unless you changed it). A **Somfy setup** entry appears in the sidebar, for administrators.

### Using it

The page lists the Somfy motors Zigbee2MQTT knows, with a **Check calibration** button (reads `configStatus`, see below) and a **Calibrate** button. **Add a blind** is the wizard: type the EUI64 and Pin Code or scan the sticker's QR code, connect over Bluetooth (the browser shows the Somfy motors it can hear, pick any, the page checks it is the right one and jogs it), then it turns on permit join, makes the motor leave its network, waits for Zigbee2MQTT to report the join, reads `configStatus` and, if needed, takes you to the calibration buttons.

The Bluetooth commands are the same ones the command line tool sends, byte for byte. Home Assistant itself never uses Bluetooth; it only publishes and listens on MQTT.

## The command line tool

Either download the executable for your system from the [releases](../../releases) page, or run the Python script:

    python3 -m venv venv
    venv/bin/pip install -r requirements.txt
    venv/bin/python somfy_setup.py

On Windows use `venv\Scripts\python` instead of `venv/bin/python`. Python 3.8 or newer.

On macOS the first run asks whether Terminal may use Bluetooth. Click Allow. With Python 3.8 or 3.9 the first run can then stop with "Bluetooth device is turned off" while that popup is still on screen; just run it again.

### Pairing a blind with the tool

Close the TaHoma Pro app on your phone first. The motor takes one Bluetooth connection at a time.

Run the tool with no arguments near the blind and it walks you through it:

1. It asks for the EUI64 and Pin Code, or for a photo of the QR code on the sticker.
2. It scans for Somfy motors, connects to each one and reads its EUI64, so the commands only ever go to the blind on your sticker. Then it jogs the blind so you can confirm it is the right one.
3. If you give it your MQTT broker, it turns on permit join in Zigbee2MQTT, makes the motor leave its network, and waits for Zigbee2MQTT to report the join. Without the broker it tells you what to do by hand.
4. It reads the motor's `configStatus`. An odd number means the limits are there and the blind will move. An even number means the motor has no limits: it will acknowledge every command and ignore it. That is not a Zigbee2MQTT problem, the motor needs calibrating.
5. If needed, it takes you into the calibration prompt (next section).

You can also run the steps on their own:

    venv/bin/python somfy_setup.py pair --eui64 4CC206FFFE0A1B2C --pin 1A2B3C --mqtt-host 192.168.1.10 --mqtt-user mqtt --mqtt-password secret
    venv/bin/python somfy_setup.py calibrate --eui64 4CC206FFFE0A1B2C --pin 1A2B3C
    venv/bin/python somfy_setup.py info --eui64 4CC206FFFE0A1B2C --pin 1A2B3C
    venv/bin/python somfy_setup.py qr sticker.jpg

`--mqtt-host` is optional everywhere. It is the broker Zigbee2MQTT uses (with the Mosquitto add-on, that is your Home Assistant's address).

## Calibration

Same buttons on the Home Assistant page, same commands at the tool's `somfy>` prompt. The limits live in the motor. You can change them while the blind stays paired with Zigbee2MQTT, and Zigbee2MQTT keeps controlling it meanwhile. The tool's commands:

    identify                     jog the motor once
    status                       ask Zigbee2MQTT whether the motor is calibrated
    up [n] / down [n]            move the blind, n from 1 to 500 (default 100)
    tilt-up [n] / tilt-down [n]  turn the slats (default 50)
    stop                         stop the motor
    limit up                     save the current position as the top limit
    limit down                   save the current position as the bottom limit
    range start                  start a new range setup (the motor forgets its range)
    range full                   finish the range setup

To fine-tune a limit, move the blind with `up` and `down` until it is where you want it, then `limit up` or `limit down`. I tested this on my blind, in both directions: the new limit is used immediately and the blind stays in Zigbee2MQTT.

To set a motor up from scratch (configStatus even), `range start`, move down to the bottom position and `limit down`, move up to the top and `limit up`. For the slats on a venetian: `range start`, turn the slats to one end with `tilt-down`, to the other end with `tilt-up`, then `range full`. This is the order the TaHoma Pro app uses. I have not run the from-scratch sequence on my own motor yet, I calibrated that one with TaHoma Pro before the tool existed. Use small steps near the ends, the motor has no limits at that point and will happily drive the blind past them.

A motor can leave the Zigbee network when it saves a new range. If that happens, turn on permit join and run `pair` again. The limits stay in the motor.

## The Zigbee2MQTT converter

The venetian motor reports itself as `Sonesse2 40 Zigbee Venetian`, which Zigbee2MQTT does not know yet. It generates a definition on the fly, but the motor has three endpoints and lists endpoint 232 first, which only carries the firmware update cluster. The generated definition sends everything there, so the buttons do nothing. Endpoint 1 is the actual blind.

Copy `zigbee2mqtt/somfy_sonesse2_40_venetian.mjs` into Zigbee2MQTT's `external_converters` folder (next to `configuration.yaml`) and restart Zigbee2MQTT. The log should say `Loaded external converter 'somfy_sonesse2_40_venetian.mjs'`. The motor then shows up as supported, with open/close/stop, position, tilt and an Identify button, and Home Assistant gets a normal cover entity.

I will submit the definition to Zigbee2MQTT so this step disappears.

## Home Assistant

Nothing special. Once the converter is in place, the blind is a cover entity from the MQTT integration like any other Zigbee2MQTT device. Position is 0 to 100, tilt is 0 to 100.

On my blinds the slats turn during the first part of every movement, and the motor then backs up a little to put them at the tilt you asked for, so tilt steps are coarse (about 5%). That is how the motor works, not the converter.

## Tested with

- Somfy Sonesse2 40 Zigbee on a venetian blind, firmware `5170183A07.6C14BC12` (TaHoma Pro showed it as A009)
- Zigbee2MQTT 2.7 (Home Assistant add-on), Sonoff Zigbee USB dongle, Mosquitto add-on
- The tool: macOS 15 on a MacBook, Python 3.8 with bleak 0.22.3; also Python 3.13 with bleak 3.0
- The integration: Chrome on macOS and Android

Other Sonesse2 Zigbee motors use the same Bluetooth commands, so they should work, but I have only tried mine. If you try another model, open an issue and say what happened.

## How it works

The Bluetooth protocol is Somfy's own, found by [Melair](https://mrmelair.com/2024/07/10/hacking-the-somfy-sonesse2-zigbee-motors/) by taking the TaHoma Pro Android app apart. The PIN from the sticker is written to one characteristic, then each command is a write to another one: leave network, move up or down by n steps, save the limit at the current position, start or finish the range setup. This tool only sends those. It has no factory reset and does not touch the motor's firmware or configuration files.

Over Zigbee the motor is a standard window covering (cluster 258) on endpoint 1. `configStatus` is attribute 7 of that cluster and its lowest bit means "operational".

## Development

The Home Assistant side has tests (`pip install -r requirements_test.txt`, then `python -m pytest`). The panel is plain JavaScript in `custom_components/somfy_zigbee_setup/frontend/panel.js`, no build step. QR codes are decoded in the browser with [jsQR](https://github.com/cozmo/jsQR) (Apache 2.0, included).

## Credits

- [Melair](https://mrmelair.com) for the protocol.
- [magik6k/somfy-sonesse2-ble-calib-tool-esp](https://github.com/magik6k/somfy-sonesse2-ble-calib-tool-esp), an ESP32 tool for the same job, where the limit and range commands and the venetian tilt payload are documented.
- [6farer/ha-somfy-ble](https://github.com/6farer/ha-somfy-ble), a Home Assistant integration that sets limits over Bluetooth proxies, where I learned that the PIN on the sticker is hex.

Not affiliated with Somfy. The motors are yours, so is the risk.

## License

MIT
