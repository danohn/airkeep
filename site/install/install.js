import { openDisplay, validateSettings } from "./serial.js";

const byId = (id) => document.getElementById(id);
const form = byId("setup-form");
const settings = byId("settings");
const connect = byId("connect");
const status = byId("setup-status");
const flasher = byId("flasher");
const installButton = flasher.querySelector("button");
const serialAvailable = window.isSecureContext && "serial" in navigator;
const serialMessage = window.isSecureContext
  ? "Open this page in desktop Chrome or Edge to use USB."
  : "USB installation and setup require HTTPS.";
let display;
let customRelay = "";
byId("service").addEventListener("change", () => {
  const hosted = byId("service").value === "hosted";
  if (hosted) customRelay = form.elements.relayUrl.value;
  form.elements.relayUrl.value = hosted ? "https://dashboard.airkeep.io" : customRelay;
  byId("relay-url-field").hidden = hosted;
});

function showStatus(message, error = false) {
  status.textContent = message;
  status.toggleAttribute("data-error", error);
}

function setConnected(connected) {
  settings.disabled = !connected;
  installButton.disabled = connected;
  connect.textContent = connected ? "Disconnect USB" : "Connect display";
}

async function disconnect() {
  const connection = display;
  display = undefined;
  await connection?.close();
  setConnected(false);
}

connect.disabled = !serialAvailable;
if (!serialAvailable) showStatus(serialMessage);

connect.addEventListener("click", async () => {
  connect.disabled = true;
  try {
    if (display) {
      await disconnect();
      showStatus("USB disconnected.");
      return;
    }
    const port = await navigator.serial.requestPort();
    showStatus("Checking for Airkeep on the USB port…");
    display = await openDisplay(port, () => {
      if (!display) return;
      showStatus("USB disconnected. Reconnect the display to continue.", true);
      void disconnect();
    });
    setConnected(true);
    showStatus("Connected to Airkeep " + display.version + ".");
  } catch (error) {
    showStatus(error.name === "NotFoundError"
      ? "No USB device selected. Connect the display and try again."
      : error.name === "NetworkError"
        ? "Could not open the USB port. Close the flasher and other serial tools, then try again."
        : error.message, true);
  } finally {
    connect.disabled = false;
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!display || settings.disabled) return;
  let config;
  try {
    config = validateSettings(Object.fromEntries(new FormData(form)));
  } catch (error) {
    showStatus(error.message, true);
    return;
  }
  connect.disabled = true;
  settings.disabled = true;
  try {
    showStatus("Saving settings to the display…");
    await display.save(config);
    form.elements.password.value = "";
    await disconnect();
    showStatus("Settings saved. Wait for the display to restart, then tap Pair display when it connects.");
  } catch (error) {
    await disconnect();
    showStatus(error.message, true);
  } finally {
    connect.disabled = false;
  }
});

async function showRelease() {
  const releaseStatus = byId("release-status");
  try {
    const response = await fetch("../firmware/release.json", { cache: "no-cache" });
    if (!response.ok) throw new Error("Release information unavailable");
    const { version } = await response.json();
    if (version === null) {
      releaseStatus.textContent = "A public firmware image is not available yet. Build from source to get started.";
      return;
    }
    if (!/^[0-9]+\.[0-9]+\.[0-9]+$/.test(version)) throw new Error("Invalid release version");
    const path = "../firmware/v" + version + "/";
    byId("download").href = path + "airkeep-factory.bin";
    byId("checksum").href = path + "SHA256SUMS.txt";
    byId("flashing").href = path + "FLASHING.md";
    byId("release-links").hidden = false;
    releaseStatus.textContent = "Airkeep v" + version + (serialAvailable ? "" : ". " + serialMessage);
    if (serialAvailable) {
      flasher.setAttribute("manifest", path + "manifest.json");
      await import("../vendor/esp-web-tools/install-button.js");
      flasher.hidden = false;
    }
  } catch {
    releaseStatus.textContent = "The USB installer could not load. Reload this page or use the flashing instructions.";
  }
}

void showRelease();
