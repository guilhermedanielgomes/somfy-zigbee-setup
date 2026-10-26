// Somfy setup panel for Home Assistant.
//
// The browser does the Bluetooth part (Web Bluetooth), Home Assistant does the
// Zigbee2MQTT part over MQTT. Nothing to install on the phone or laptop, but the
// page must be opened over https:// for the browser to allow Bluetooth and the camera.

const SOMFY_COMPANY_ID = 1480;

function somfyUuid(n) {
  return `${n.toString(16).padStart(8, "0")}-cad9-46c6-a2ea-2ca16d57b4a5`;
}

const SERVICE = {
  operational: somfyUuid(0x00000000),
  configuration: somfyUuid(0x00010000),
  network: somfyUuid(0x00020000),
};

// name: [service, characteristic]
const CHAR = {
  identify: [SERVICE.operational, somfyUuid(0x00000001)],
  stop: [SERVICE.operational, somfyUuid(0x00000006)],
  auth: [SERVICE.operational, somfyUuid(0x0000000b)],
  firmware: [SERVICE.configuration, somfyUuid(0x00010002)],
  limit: [SERVICE.configuration, somfyUuid(0x00010007)],
  down: [SERVICE.configuration, somfyUuid(0x00010008)],
  up: [SERVICE.configuration, somfyUuid(0x00010009)],
  range: [SERVICE.configuration, somfyUuid(0x0001000b)],
  leave: [SERVICE.network, somfyUuid(0x00020001)],
  channel: [SERVICE.network, somfyUuid(0x00020002)],
  pan: [SERVICE.network, somfyUuid(0x00020003)],
  eui: [SERVICE.network, somfyUuid(0x00020005)],
};

const HEX = "0-9A-Fa-f";

// ------------------------------------------------------------------ sticker

export function normaliseEui64(text) {
  const digits = (text || "").replace(new RegExp(`[^${HEX}]`, "g"), "").toUpperCase();
  return digits.length === 16 ? digits : null;
}

export function eui64FromEui48(text) {
  const digits = (text || "").replace(new RegExp(`[^${HEX}]`, "g"), "").toUpperCase();
  return digits.length === 12 ? digits.slice(0, 6) + "FFFE" + digits.slice(6) : null;
}

export function parseSticker(text) {
  const found = {};
  const clean = (text || "").trim();
  const labels = [
    ["eui64", ["EUI64", "EUI-64", "Z"]],
    ["eui48", ["EUI48", "EUI-48", "MAC", "BLE"]],
    ["pin", ["PINCODE", "PIN CODE", "PIN", "P"]],
    ["install_code", ["INSTALLCODE", "INSTALL CODE", "IC", "I"]],
  ];
  for (const [key, names] of labels) {
    for (const name of names) {
      const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const m = clean.match(new RegExp(`(?<![A-Za-z])${escaped}\\s*[:=]\\s*([${HEX}]+)`, "i"));
      if (m && !(key in found)) {
        found[key] = m[1].toUpperCase();
        break;
      }
    }
  }
  for (const token of clean.match(new RegExp(`(?<![${HEX}])[${HEX}]+(?![${HEX}])`, "g")) || []) {
    const value = token.toUpperCase();
    if (value.length === 16 && !found.eui64) found.eui64 = value;
    else if ((value.length === 32 || value.length === 36) && !found.install_code) found.install_code = value;
    else if (value.length === 12 && !found.eui48) found.eui48 = value;
  }
  if (!found.eui64 && found.eui48) found.eui64 = eui64FromEui48(found.eui48);
  return found;
}

function pinCandidates(pin) {
  const out = [Uint8Array.from(pin.match(/../g).map((h) => parseInt(h, 16)))];
  if (/^\d{6}$/.test(pin)) {
    // the sticker prints the PIN in hex; older tools sent it as a decimal number
    const n = parseInt(pin, 10);
    out.push(Uint8Array.from([n & 0xff, (n >> 8) & 0xff, (n >> 16) & 0xff]));
  }
  return out;
}

function hex(bytes) {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("").toUpperCase();
}

function le16(n) {
  return Uint8Array.from([n & 0xff, (n >> 8) & 0xff]);
}

// ------------------------------------------------------------------ QR codes

async function decodeQr(file) {
  const bitmap = await createImageBitmap(file);
  if ("BarcodeDetector" in window) {
    try {
      const detector = new BarcodeDetector({ formats: ["qr_code"] });
      const codes = await detector.detect(bitmap);
      if (codes.length) return codes[0].rawValue;
    } catch (err) {
      // fall through to jsQR
    }
  }
  if (!window.jsQR) {
    await new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = new URL("jsQR.js", import.meta.url).href;
      script.onload = resolve;
      script.onerror = () => reject(new Error("could not load the QR decoder"));
      document.head.appendChild(script);
    });
  }
  for (const scale of [1, 2, 0.5]) {
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    const ctx = canvas.getContext("2d");
    ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const image = ctx.getImageData(0, 0, canvas.width, canvas.height);
    const code = window.jsQR(image.data, image.width, image.height);
    if (code && code.data) return code.data;
  }
  return "";
}

// ------------------------------------------------------------------ the motor over Bluetooth

export class Motor {
  constructor(eui64, pin, log) {
    this.eui64 = eui64;
    this.pin = pin;
    this.log = log || (() => {});
    this.device = null;
    this.server = null;
    this.expectDrop = false;
  }

  static available() {
    return !!(navigator.bluetooth && window.isSecureContext);
  }

  async choose() {
    this.device = await navigator.bluetooth.requestDevice({
      filters: [{ manufacturerData: [{ companyIdentifier: SOMFY_COMPANY_ID }] }],
      optionalServices: Object.values(SERVICE),
    });
    this.device.addEventListener("gattserverdisconnected", () => {
      if (!this.expectDrop) this.log("Bluetooth connection lost - it reconnects on the next command");
      this.server = null;
    });
  }

  async connect() {
    if (this.server && this.server.connected) return;
    this.server = await this.device.gatt.connect();
    this.loggedIn = false;
    this.verified = false;
  }

  async characteristic(name) {
    const [serviceUuid, charUuid] = CHAR[name];
    const service = await this.server.getPrimaryService(serviceUuid);
    return service.getCharacteristic(charUuid);
  }

  // raw access, used while connecting; everything else goes through write() and read()
  async _write(name, bytes) {
    const ch = await this.characteristic(name);
    if (ch.writeValueWithResponse) await ch.writeValueWithResponse(bytes);
    else await ch.writeValue(bytes);
  }

  async _read(name) {
    const ch = await this.characteristic(name);
    const value = await ch.readValue();
    return new Uint8Array(value.buffer);
  }

  // one command at a time, even if buttons are pressed quickly
  async _oneAtATime(fn) {
    const previous = this._queue || Promise.resolve();
    let release;
    this._queue = new Promise((resolve) => (release = resolve));
    try {
      await previous.catch(() => {});
      return await fn();
    } finally {
      release();
    }
  }

  async write(name, bytes) {
    return this._oneAtATime(async () => {
      await this.ensure();
      await this._write(name, bytes);
    });
  }

  async read(name) {
    return this._oneAtATime(async () => {
      await this.ensure();
      return this._read(name);
    });
  }

  async readEui() {
    try {
      const bytes = await this._read("eui");
      return hex(Array.from(bytes.slice(0, 8)).reverse());
    } catch (err) {
      return null;
    }
  }

  async login() {
    for (const candidate of pinCandidates(this.pin)) {
      try {
        await this._write("auth", candidate);
        this.loggedIn = true;
        return true;
      } catch (err) {
        // try the next form
      }
    }
    return false;
  }

  // Connects (again if needed) and logs in. Returns "mine", "other" or "refused".
  async ensure() {
    if (this.server && this.server.connected && this.loggedIn) return "mine";
    if (this.server && this.server.connected && this.verified) {
      // reconnected earlier and already checked: only the PIN is missing
      if (!(await this.login())) throw new Error("the motor refused the PIN");
      return "mine";
    }
    await this.connect();
    let eui = await this.readEui();
    let loggedIn = false;
    if (eui === null) {
      // in case the motor only answers after the PIN
      loggedIn = await this.login();
      eui = await this.readEui();
    }
    if (eui !== null && eui !== this.eui64) {
      this.foundEui = eui;
      this.server.disconnect();
      return "other";
    }
    if (!loggedIn && !(await this.login())) {
      this.server.disconnect();
      return "refused";
    }
    this.verified = true;
    await new Promise((r) => setTimeout(r, 1500));
    return "mine";
  }

  async identify() {
    await this.write("identify", Uint8Array.from([1]));
  }

  async stop() {
    await this.write("stop", Uint8Array.from([1]));
  }

  async move(direction, steps) {
    await this.write(direction === "up" ? "up" : "down", le16(steps));
  }

  async tilt(direction, steps, release = 0) {
    // the app's "dimensioned move": duration, release, dimension 1 = slats, speed 2 = nominal
    const payload = Uint8Array.from([steps & 0xff, (steps >> 8) & 0xff, release, 1, 2]);
    await this.write(direction === "up" ? "up" : "down", payload);
  }

  async saveLimit(which) {
    await this.write("limit", Uint8Array.from([which === "top" ? 0 : 1]));
  }

  async rangeStart() {
    await this.write("range", Uint8Array.from([0, 0]));
  }

  async rangeFull() {
    await this.write("range", Uint8Array.from([0, 1]));
  }

  async leaveNetwork() {
    this.expectDrop = true;
    try {
      await this.write("leave", Uint8Array.from([1]));
    } catch (err) {
      // the motor often drops the link while answering; the command still went through
    } finally {
      setTimeout(() => (this.expectDrop = false), 3000);
    }
  }

  async info() {
    const get = async (name) => {
      try {
        return await this.read(name);
      } catch (err) {
        return null;
      }
    };
    const channel = await get("channel");
    const pan = await get("pan");
    const firmware = await get("firmware");
    return {
      channel: channel ? channel[0] : null,
      pan: pan && pan.length >= 2 ? "0x" + hex([pan[1], pan[0]]) : null,
      firmware: firmware ? new TextDecoder().decode(firmware).replace(/\0+$/, "") : null,
    };
  }

  disconnect() {
    this.expectDrop = true;
    if (this.device && this.device.gatt.connected) this.device.gatt.disconnect();
  }
}

// ------------------------------------------------------------------ the panel

const STYLE = `
  :host { display: block; padding: 16px; max-width: 760px; margin: 0 auto; color: var(--primary-text-color); }
  h1 { font-size: 24px; font-weight: 400; margin: 8px 0 16px; }
  h2 { font-size: 18px; font-weight: 500; margin: 0 0 12px; }
  .card { background: var(--card-background-color, #fff); border-radius: var(--ha-card-border-radius, 12px);
          box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.15)); padding: 16px; margin-bottom: 16px; }
  .muted { color: var(--secondary-text-color); font-size: 14px; }
  .warn { background: var(--warning-color, #ffa600); color: #000; padding: 12px; border-radius: 8px; margin-bottom: 16px; }
  .bad { color: var(--error-color, #db4437); }
  .good { color: var(--success-color, #43a047); }
  button { font: inherit; padding: 8px 14px; border-radius: 8px; border: 1px solid var(--primary-color);
           background: var(--primary-color); color: var(--text-primary-color, #fff); cursor: pointer; margin: 4px 6px 4px 0; }
  button.secondary { background: transparent; color: var(--primary-color); }
  button:disabled { opacity: .5; cursor: default; }
  button.hold { touch-action: none; user-select: none; -webkit-user-select: none; -webkit-touch-callout: none; padding: 14px 18px; }
  button.hold.on { filter: brightness(1.3); box-shadow: 0 0 0 3px var(--primary-color); }
  details { margin-top: 12px; }
  summary { cursor: pointer; }
  input, select { font: inherit; padding: 8px; border-radius: 6px; border: 1px solid var(--divider-color, #ccc);
                  background: var(--card-background-color, #fff); color: var(--primary-text-color); width: 100%; box-sizing: border-box; }
  label { display: block; margin: 10px 0 4px; font-size: 14px; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .device { display: flex; justify-content: space-between; gap: 12px; padding: 12px 0; border-top: 1px solid var(--divider-color, #eee); flex-wrap: wrap; }
  .device:first-of-type { border-top: none; }
  .log { font-family: monospace; font-size: 13px; white-space: pre-wrap; background: var(--primary-background-color);
         padding: 8px; border-radius: 6px; max-height: 220px; overflow: auto; }
  .steps { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 12px; }
  .step { padding: 4px 10px; border-radius: 12px; background: var(--primary-background-color); font-size: 13px; }
  .step.on { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  code { background: var(--primary-background-color); padding: 1px 4px; border-radius: 4px; }
`;

const JOIN_TIMEOUT = 180000;
// Hold-to-move: the biggest move the motor accepts, repeated while the button is held
const HOLD_STEPS = 500;
const HOLD_REPEAT = 1000;

class SomfyZigbeeSetupPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this.devices = [];
    this.view = "home";
    this.lines = [];
    this.status = {};
    this.wizard = null;
    this.motor = null;
    this.unsubscribe = null;
    this.pendingJoin = null;
    this.holding = null;
    // whatever happens to the page, a held button must not keep the blind moving
    this._release = () => this.endHold();
  }

  set hass(value) {
    const first = this._hass === null;
    this._hass = value;
    if (first) this._start();
  }

  get hass() {
    return this._hass;
  }

  async _start() {
    await this.loadDevices();
    try {
      this.unsubscribe = await this._hass.connection.subscribeMessage((event) => this.onEvent(event), {
        type: "somfy_zigbee_setup/subscribe_events",
      });
    } catch (err) {
      this.say("Could not subscribe to Zigbee2MQTT events: " + err.message);
    }
    this.render();
  }

  connectedCallback() {
    window.addEventListener("blur", this._release);
    document.addEventListener("visibilitychange", this._release);
  }

  disconnectedCallback() {
    window.removeEventListener("blur", this._release);
    document.removeEventListener("visibilitychange", this._release);
    this.endHold();
    if (this.unsubscribe) this.unsubscribe();
    if (this.motor) this.motor.disconnect();
  }

  async loadDevices() {
    try {
      const result = await this._hass.callWS({ type: "somfy_zigbee_setup/devices" });
      this.devices = result.devices;
    } catch (err) {
      this.say("Could not get the device list: " + err.message);
    }
  }

  onEvent(event) {
    if (event.type === "devices") {
      this.loadDevices().then(() => this.render());
      return;
    }
    const who = event.friendly_name || event.ieee;
    if (event.type === "device_joined") this.say(`${who} joined, Zigbee2MQTT is interviewing it...`);
    if (event.type === "device_interview" && event.status === "successful") {
      this.say(`${who}: interview done (${event.description || "no matching definition yet"})`);
    }
    if (event.type === "device_interview" && event.status === "failed") this.say(`${who}: the interview failed`);
    if (event.type === "device_announce") this.say(`${who} is back on the network`);
    if (event.type === "device_leave") this.say(`${who} left the network`);
    if (this.pendingJoin && event.ieee === this.pendingJoin.ieee) {
      if ((event.type === "device_interview" && event.status === "successful") || event.type === "device_announce") {
        this.pendingJoin.resolve(true);
      } else if (event.type === "device_interview" && event.status === "failed") {
        this.pendingJoin.resolve(false);
      }
    }
  }

  say(text) {
    const stamp = new Date().toLocaleTimeString();
    this.lines.push(`${stamp}  ${text}`);
    if (this.lines.length > 200) this.lines.shift();
    const log = this.shadowRoot.querySelector(".log");
    if (log) {
      log.textContent = this.lines.join("\n");
      log.scrollTop = log.scrollHeight;
    }
  }

  ieeeOf(eui64) {
    return "0x" + eui64.toLowerCase();
  }

  deviceFor(eui64) {
    return this.devices.find((d) => d.ieee === this.ieeeOf(eui64)) || null;
  }

  async permitJoin() {
    await this._hass.callWS({ type: "somfy_zigbee_setup/permit_join", time: 254 });
    this.say("Permit join is on in Zigbee2MQTT for 4 minutes");
  }

  async rename(eui64, name) {
    const result = await this._hass.callWS({ type: "somfy_zigbee_setup/rename", ieee: this.ieeeOf(eui64), name });
    if (result.status !== "ok") throw new Error(result.error || "Zigbee2MQTT refused the name");
    this.say(`Named "${name}" in Zigbee2MQTT and Home Assistant`);
    await new Promise((r) => setTimeout(r, 1000));
    await this.loadDevices();
  }

  // A name typed in the wizard goes to Zigbee2MQTT once the motor is there. Not fatal if refused.
  async renameIfAsked(w) {
    const existing = this.deviceFor(w.eui64);
    if (!w.name || (existing && existing.friendly_name === w.name)) return;
    try {
      await this.rename(w.eui64, w.name);
    } catch (err) {
      this.say(`Could not name it "${w.name}": ${err.message}. You can rename it in Zigbee2MQTT.`);
    }
  }

  async configStatus(eui64) {
    this.say("Asking the motor whether it is calibrated...");
    const result = await this._hass.callWS({ type: "somfy_zigbee_setup/config_status", ieee: this.ieeeOf(eui64) });
    const value = result.config_status;
    if (value === null || value === undefined) {
      this.say("No answer from Zigbee2MQTT. Is the motor on the network and powered?");
      return null;
    }
    this.say(
      value % 2
        ? `configStatus ${value} (odd): the motor has its limits and will move`
        : `configStatus ${value} (even): the motor has no limits and will ignore move commands`
    );
    return value;
  }

  waitForJoin(eui64) {
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.pendingJoin = null;
        resolve(false);
      }, JOIN_TIMEOUT);
      this.pendingJoin = {
        ieee: this.ieeeOf(eui64),
        resolve: (ok) => {
          clearTimeout(timer);
          this.pendingJoin = null;
          resolve(ok);
        },
      };
    });
  }

  // --------------------------------------------------------------- rendering

  render() {
    const root = this.shadowRoot;
    root.innerHTML = `<style>${STYLE}</style><div id="view"></div>`;
    const view = root.querySelector("#view");
    if (this.view === "home") this.renderHome(view);
    else if (this.view === "wizard") this.renderWizard(view);
    else if (this.view === "calibrate") this.renderCalibrate(view);
  }

  bluetoothWarning() {
    if (Motor.available()) return "";
    const why = window.isSecureContext
      ? "This browser has no Web Bluetooth. Use Chrome or Edge on Android, Windows, macOS or Linux. iPhones: Safari can't, the Bluefy app can."
      : "This page was opened over http://, and browsers only allow Bluetooth on https:// pages. Open Home Assistant through its https address.";
    return `<div class="warn"><b>Bluetooth is not available here.</b> ${why} You can still use the Zigbee2MQTT buttons below, or the laptop tool from the project.</div>`;
  }

  renderHome(view) {
    const rows = this.devices.length
      ? this.devices
          .map((d) => {
            const badge = d.supported
              ? `<span class="good">supported</span>`
              : `<span class="bad">not supported</span> <span class="muted">(needs the converter, see the README)</span>`;
            const status = this.status[d.ieee] !== undefined ? ` · configStatus ${this.status[d.ieee]}` : "";
            return `<div class="device">
              <div><b>${escapeHtml(d.friendly_name)}</b><br><span class="muted">${escapeHtml(d.model_id || "")} · ${badge}${status}</span></div>
              <div class="row">
                <button class="secondary" data-check="${d.ieee}">Check calibration</button>
                <button class="secondary" data-cal="${d.ieee}" ${Motor.available() ? "" : "disabled"}>Calibrate</button>
              </div></div>`;
          })
          .join("")
      : `<p class="muted">No Somfy motors in Zigbee2MQTT yet.</p>`;

    view.innerHTML = `
      <h1>Somfy blinds</h1>
      ${this.bluetoothWarning()}
      <div class="card">
        <div class="row">
          <button id="add" ${Motor.available() ? "" : "disabled"}>Add a blind</button>
          <button id="permit" class="secondary">Permit join (4 min)</button>
          <button id="reload" class="secondary">Refresh</button>
        </div>
      </div>
      <div class="card"><h2>In Zigbee2MQTT</h2>${rows}</div>
      <div class="card"><h2>Events</h2><div class="log">${escapeHtml(this.lines.join("\n"))}</div></div>`;

    view.querySelector("#add").onclick = () => {
      this.wizard = { step: 1, eui64: "", pin: "", name: "" };
      this.view = "wizard";
      this.render();
    };
    view.querySelector("#permit").onclick = () => this.permitJoin().catch((e) => this.say(e.message));
    view.querySelector("#reload").onclick = () => this.loadDevices().then(() => this.render());
    view.querySelectorAll("[data-check]").forEach((b) => {
      b.onclick = async () => {
        b.disabled = true;
        const eui64 = b.dataset.check.slice(2).toUpperCase();
        const value = await this.configStatus(eui64).catch((e) => (this.say(e.message), null));
        if (value !== null) this.status[b.dataset.check] = value;
        this.render();
      };
    });
    view.querySelectorAll("[data-cal]").forEach((b) => {
      b.onclick = () => {
        const dev = this.devices.find((d) => d.ieee === b.dataset.cal);
        this.wizard = { step: 0, eui64: b.dataset.cal.slice(2).toUpperCase(), pin: "", name: dev && dev.friendly_name };
        this.view = "calibrate";
        this.render();
      };
    });
  }

  stepsBar(current) {
    const names = ["Sticker", "Connect", "Pair", "Status", "Calibrate"];
    return `<div class="steps">${names
      .map((n, i) => `<span class="step ${i + 1 === current ? "on" : ""}">${i + 1}. ${n}</span>`)
      .join("")}</div>`;
  }

  renderWizard(view) {
    const w = this.wizard;
    let body = "";
    if (w.step === 1) {
      body = `
        <p>Both values are printed on the sticker on the motor. Scan the QR code with the camera or type them.</p>
        <label>Photo of the QR code <input type="file" id="qr" accept="image/*" capture="environment"></label>
        <label>EUI64 <input id="eui" placeholder="4CC206FFFE0A1B2C" value="${escapeHtml(w.eui64)}"></label>
        <label>Pin Code <input id="pin" placeholder="6 characters" maxlength="6" value="${escapeHtml(w.pin)}"></label>
        <label>Name in Home Assistant (optional) <input id="name" placeholder="Kitchen blind" maxlength="80" value="${escapeHtml(w.name || "")}"></label>
        <p class="muted" id="qrnote"></p>
        <div class="row"><button id="next">Next</button><button id="cancel" class="secondary">Cancel</button></div>`;
    } else if (w.step === 2) {
      body = `
        <p>Close the TaHoma Pro app on your phone first (the motor takes one Bluetooth connection at a time), stand near the blind and press the button. Your browser shows the Somfy motors it can hear; pick any, the page checks it is the one from the sticker.</p>
        <div class="row"><button id="connect">Connect over Bluetooth</button><button id="cancel" class="secondary">Cancel</button></div>
        <p id="note"></p>
        <div id="jog" hidden><p>The blind should have jogged just now. Did it?</p>
          <div class="row"><button id="yes">Yes</button><button id="no" class="secondary">No</button></div></div>`;
    } else if (w.step === 3) {
      const existing = this.deviceFor(w.eui64);
      body = existing
        ? `<p>This motor is already in Zigbee2MQTT as <b>${escapeHtml(existing.friendly_name)}</b>.</p>
           <div class="row"><button id="skip">Continue</button><button id="pair" class="secondary">Make it leave and join again</button></div>`
        : `<p>Permit join goes on in Zigbee2MQTT, the motor is told to leave its current network, and the page waits for Zigbee2MQTT to report the join (up to 3 minutes).</p>
           <div class="row"><button id="pair">Pair with Zigbee2MQTT</button><button id="cancel" class="secondary">Cancel</button></div>`;
      body += `<p id="note"></p>`;
    } else if (w.step === 4) {
      body = `<p id="note">Reading configStatus from the motor...</p>
        <div class="row" id="actions" hidden>
          <button id="calibrate">Calibrate now</button><button id="finish" class="secondary">Finish</button></div>`;
    } else if (w.step === 5) {
      body = this.calibrationControls() + `<div class="row"><button id="finish" class="secondary">Finish</button></div>`;
    }
    view.innerHTML = `<h1>Add a blind</h1>${this.stepsBar(w.step)}<div class="card">${body}</div>
      <div class="card"><h2>Log</h2><div class="log">${escapeHtml(this.lines.join("\n"))}</div></div>`;
    this.wireWizard(view);
  }

  wireWizard(view) {
    const w = this.wizard;
    const note = (text, cls = "") => {
      const el = view.querySelector("#note");
      if (el) {
        el.textContent = text;
        el.className = cls;
      }
    };
    const cancel = view.querySelector("#cancel");
    if (cancel) cancel.onclick = () => this.goHome();

    if (w.step === 1) {
      view.querySelector("#qr").onchange = async (ev) => {
        const file = ev.target.files[0];
        if (!file) return;
        const qrnote = view.querySelector("#qrnote");
        qrnote.textContent = "Reading the QR code...";
        try {
          const text = await decodeQr(file);
          const found = parseSticker(text);
          if (!text) qrnote.textContent = "No QR code found in that photo. Try closer, with more light.";
          else if (!found.eui64 && !found.pin) qrnote.textContent = `The QR code says "${text}", nothing I recognise. Type the values instead.`;
          else {
            if (found.eui64) view.querySelector("#eui").value = found.eui64;
            if (found.pin) view.querySelector("#pin").value = found.pin;
            qrnote.textContent = "Found: " + Object.entries(found).map(([k, v]) => `${k} ${v}`).join(", ");
          }
        } catch (err) {
          qrnote.textContent = "Could not read the photo: " + err.message;
        }
      };
      view.querySelector("#next").onclick = () => {
        const eui64 = normaliseEui64(view.querySelector("#eui").value);
        const pin = view.querySelector("#pin").value.trim().toUpperCase();
        if (!eui64) return alert("The EUI64 should be 16 characters, like 4CC206FFFE0A1B2C.");
        if (!/^[0-9A-F]{6}$/.test(pin)) return alert("The Pin Code is the 6 characters printed on the sticker.");
        w.eui64 = eui64;
        w.pin = pin;
        w.name = view.querySelector("#name").value.trim();
        w.step = 2;
        this.render();
      };
    }

    if (w.step === 2) {
      view.querySelector("#connect").onclick = async () => {
        const button = view.querySelector("#connect");
        button.disabled = true;
        try {
          this.motor = new Motor(w.eui64, w.pin, (t) => this.say(t));
          await this.motor.choose();
          note("Connecting...");
          const result = await this.motor.ensure();
          if (result === "other") {
            note(`That is a different motor (EUI64 ${this.motor.foundEui}). Press the button again and pick another one.`, "bad");
            button.disabled = false;
            return;
          }
          if (result === "refused") {
            note("The motor refused the PIN. Check the Pin Code on the sticker.", "bad");
            button.disabled = false;
            return;
          }
          this.say(`Connected to ${w.eui64}, PIN accepted`);
          await this.motor.identify();
          note("");
          view.querySelector("#jog").hidden = false;
        } catch (err) {
          note("Bluetooth: " + err.message, "bad");
          button.disabled = false;
        }
      };
      view.querySelector("#yes").onclick = () => {
        w.step = 3;
        this.render();
      };
      view.querySelector("#no").onclick = () => {
        note("Then this is not the blind you think. Check the sticker and start again.", "bad");
        this.motor.disconnect();
        this.motor = null;
        view.querySelector("#jog").hidden = true;
        view.querySelector("#connect").disabled = false;
      };
    }

    if (w.step === 3) {
      const skip = view.querySelector("#skip");
      if (skip)
        skip.onclick = async () => {
          await this.renameIfAsked(w);
          w.step = 4;
          this.render();
          this.runStatus();
        };
      view.querySelector("#pair").onclick = async () => {
        view.querySelector("#pair").disabled = true;
        try {
          await this.permitJoin();
          const waiting = this.waitForJoin(w.eui64);
          note("Telling the motor to leave its network...");
          await this.motor.leaveNetwork();
          this.say("Leave-network sent, waiting for the motor to join (up to 3 minutes)...");
          note("Waiting for Zigbee2MQTT to report the join...");
          const ok = await waiting;
          if (!ok) {
            note("Zigbee2MQTT did not report the motor. Check that the Zigbee adapter is in range and try again.", "bad");
            view.querySelector("#pair").disabled = false;
            return;
          }
          await this.loadDevices();
          await this.renameIfAsked(w);
          w.step = 4;
          this.render();
          this.runStatus();
        } catch (err) {
          note(err.message, "bad");
          view.querySelector("#pair").disabled = false;
        }
      };
    }

    if (w.step === 4) {
      view.querySelector("#calibrate").onclick = () => {
        w.step = 5;
        this.render();
      };
      view.querySelector("#finish").onclick = () => this.goHome();
    }

    if (w.step === 5) {
      this.wireCalibration(view, w.eui64);
      view.querySelector("#finish").onclick = () => this.goHome();
    }
  }

  async runStatus() {
    const w = this.wizard;
    await new Promise((r) => setTimeout(r, 2000));
    let value = null;
    try {
      value = await this.configStatus(w.eui64);
    } catch (err) {
      this.say(err.message);
    }
    const view = this.shadowRoot.querySelector("#view");
    const note = view.querySelector("#note");
    const actions = view.querySelector("#actions");
    if (!note) return;
    if (value === null) note.textContent = "No answer. You can still try the calibration, or check the Zigbee2MQTT log.";
    else if (value % 2) note.textContent = `configStatus ${value}: the motor is calibrated. Fine-tune a limit now, or finish.`;
    else note.textContent = `configStatus ${value}: the motor has no limits and will not move until it is calibrated.`;
    actions.hidden = false;
  }

  calibrationControls() {
    return `
      <p class="muted">Hold ▲ or ▼ to move the blind; it stops when you let go. Put the blind where a limit should be, then save it. From scratch (configStatus even): Range start, hold ▼ to the bottom and save the bottom limit, hold ▲ to the top and save the top limit.</p>
      <div class="row">
        <button class="hold" data-hold="up">▲ Hold to move up</button>
        <button class="hold" data-hold="down">▼ Hold to move down</button>
        <button data-act="stop" class="secondary">Stop</button>
      </div>
      <div class="row">
        <span class="muted">Fine-tune:</span>
        <button data-act="nudge-up" class="secondary">▲ a little</button><button data-act="nudge-down" class="secondary">▼ a little</button>
        <select id="step" style="width:auto"><option value="5">5 steps</option><option value="20" selected>20 steps</option><option value="100">100 steps</option></select>
      </div>
      <div class="row">
        <button data-act="limit-top">Save as top limit</button><button data-act="limit-bottom">Save as bottom limit</button>
      </div>
      <div class="row">
        <button data-act="range-start" class="secondary">Range start</button><button data-act="range-full" class="secondary">Range full</button>
        <button data-act="status" class="secondary">Check status</button><button data-act="identify" class="secondary">Identify</button><button data-act="info" class="secondary">Info</button>
      </div>
      <details>
        <summary class="muted">Slats (only when setting a venetian up from scratch)</summary>
        <p class="muted">On a venetian the slats turn with the same motor, so these feel like small moves up and down. They are what TaHoma Pro's "blade tilting" setup sends: Range start, Slats down until the slats are closed one way, Slats up until closed the other way, Range full.</p>
        <div class="row">
          <select id="tstep" style="width:auto"><option value="50">50 tilt steps</option><option value="10">10 tilt steps</option></select>
          <button data-act="tilt-up" class="secondary">Slats up</button><button data-act="tilt-down" class="secondary">Slats down</button>
        </div>
      </details>
      <p id="note"></p>`;
  }

  // Hold-to-move: a big move command when the button goes down, again every second
  // while it stays down, and stop the moment it is released.
  startHold(direction, button) {
    if (this.holding || !this.motor) return;
    const hold = { direction, button, busy: false, failed: false, timer: null };
    this.holding = hold;
    button.classList.add("on");
    const send = () => {
      if (hold.busy || this.holding !== hold) return;
      hold.busy = true;
      this.motor
        .move(direction, HOLD_STEPS)
        .catch((err) => {
          if (!hold.failed) this.say("error: " + err.message);
          hold.failed = true;
        })
        .finally(() => (hold.busy = false));
    };
    send();
    hold.timer = setInterval(send, HOLD_REPEAT);
    this.say(`moving ${direction} while held`);
  }

  async endHold() {
    const hold = this.holding;
    if (!hold) return;
    this.holding = null;
    clearInterval(hold.timer);
    hold.button.classList.remove("on");
    try {
      await this.motor.stop();
      this.say("stop sent");
    } catch (err) {
      this.say("error: " + err.message);
    }
  }

  wireCalibration(view, eui64) {
    const note = (text, cls = "") => {
      const el = view.querySelector("#note");
      if (el) {
        el.textContent = text;
        el.className = cls;
      }
    };
    view.querySelectorAll("[data-hold]").forEach((b) => {
      b.addEventListener("pointerdown", (ev) => {
        ev.preventDefault();
        b.setPointerCapture(ev.pointerId);
        this.startHold(b.dataset.hold, b);
      });
      for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) b.addEventListener(type, this._release);
      b.addEventListener("contextmenu", (ev) => ev.preventDefault());
    });
    view.querySelectorAll("[data-act]").forEach((b) => {
      b.onclick = async () => {
        const act = b.dataset.act;
        const steps = parseInt(view.querySelector("#step").value, 10);
        const tsteps = parseInt(view.querySelector("#tstep").value, 10);
        try {
          if (act === "status") return void (await this.configStatus(eui64));
          const m = this.motor;
          if (act === "nudge-up" || act === "nudge-down") {
            const direction = act.slice(6);
            await m.move(direction, steps);
            this.say(`moving ${direction} (${steps} steps)`);
          } else if (act === "tilt-up" || act === "tilt-down") {
            await m.tilt(act.slice(5), tsteps);
            this.say(`tilting ${act.slice(5)} (${tsteps})`);
          } else if (act === "stop") {
            await m.stop();
            this.say("stop sent");
          } else if (act === "identify") {
            await m.identify();
            this.say("identify sent, the motor should jog");
          } else if (act === "limit-top" || act === "limit-bottom") {
            const which = act === "limit-top" ? "top" : "bottom";
            if (!confirm(`Save the current position as the ${which} limit?`)) return;
            await m.saveLimit(which);
            this.say(`${which} limit saved`);
          } else if (act === "range-start") {
            if (!confirm("The motor forgets its range until you finish the setup. Start?")) return;
            await m.rangeStart();
            this.say("range setup started");
          } else if (act === "range-full") {
            if (!confirm("Finish the range setup?")) return;
            await m.rangeFull();
            this.say("range setup finished");
          } else if (act === "info") {
            const info = await m.info();
            this.say(`Zigbee channel ${info.channel}, PAN ID ${info.pan}, firmware ${info.firmware}`);
          }
          note("");
        } catch (err) {
          note(err.message, "bad");
          this.say("error: " + err.message);
        }
      };
    });
  }

  renderCalibrate(view) {
    const w = this.wizard;
    const body =
      w.step === 0
        ? `<p>Calibrating <b>${escapeHtml(w.name || w.eui64)}</b> (EUI64 ${w.eui64}). Close the TaHoma Pro app on your phone, stand near the blind, type the Pin Code from its sticker and connect.</p>
           <label>Pin Code <input id="pin" maxlength="6" value="${escapeHtml(w.pin)}"></label>
           <div class="row"><button id="connect">Connect over Bluetooth</button><button id="cancel" class="secondary">Back</button></div>
           <p id="note"></p>`
        : this.calibrationControls() + `<div class="row"><button id="finish" class="secondary">Done</button></div>`;
    view.innerHTML = `<h1>Calibrate</h1><div class="card">${body}</div>
      <div class="card"><h2>Log</h2><div class="log">${escapeHtml(this.lines.join("\n"))}</div></div>`;
    const cancel = view.querySelector("#cancel");
    if (cancel) cancel.onclick = () => this.goHome();
    if (w.step === 0) {
      view.querySelector("#connect").onclick = async () => {
        const pin = view.querySelector("#pin").value.trim().toUpperCase();
        if (!/^[0-9A-F]{6}$/.test(pin)) return alert("The Pin Code is the 6 characters printed on the sticker.");
        const button = view.querySelector("#connect");
        const note = view.querySelector("#note");
        button.disabled = true;
        try {
          this.motor = new Motor(w.eui64, pin, (t) => this.say(t));
          await this.motor.choose();
          note.textContent = "Connecting...";
          const result = await this.motor.ensure();
          if (result !== "mine") {
            note.textContent =
              result === "other"
                ? `That is a different motor (EUI64 ${this.motor.foundEui}). Press the button again and pick another one.`
                : "The motor refused the PIN. Check the Pin Code on the sticker.";
            note.className = "bad";
            button.disabled = false;
            return;
          }
          this.say(`Connected to ${w.eui64}, PIN accepted`);
          w.pin = pin;
          w.step = 1;
          this.render();
        } catch (err) {
          note.textContent = "Bluetooth: " + err.message;
          note.className = "bad";
          button.disabled = false;
        }
      };
    } else {
      this.wireCalibration(view, w.eui64);
      view.querySelector("#finish").onclick = () => this.goHome();
    }
  }

  async goHome() {
    await this.endHold();
    if (this.motor) this.motor.disconnect();
    this.motor = null;
    this.wizard = null;
    this.view = "home";
    this.render();
  }
}

function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

if (!customElements.get("somfy-zigbee-setup-panel")) {
  customElements.define("somfy-zigbee-setup-panel", SomfyZigbeeSetupPanel);
}
