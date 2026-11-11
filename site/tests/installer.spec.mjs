import { test, expect } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { extname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const site = fileURLToPath(new URL("..", import.meta.url));
let directory, server, origin;

test.beforeAll(async () => {
  directory = await mkdtemp(join(tmpdir(), "airkeep-installer-"));
  const bundles = join(directory, "bundles");
  for (const tag of ["v0.5.0", "v0.10.0"]) {
    const release = join(bundles, tag);
    await mkdir(release, { recursive: true });
    await writeFile(join(release, "airkeep-factory.bin"), tag);
    await writeFile(join(release, "SHA256SUMS.txt"),
      createHash("sha256").update(tag).digest("hex") + "  airkeep-factory.bin\n");
    await writeFile(join(release, "FLASHING.md"), "Factory flash instructions\n");
  }
  const output = join(directory, "pages");
  execFileSync("bash", [join(site, "build.sh"), output, bundles]);
  execFileSync("bash", [join(site, "build.sh"), join(directory, "before-release")]);
  server = createServer(async (request, response) => {
    const url = new URL(request.url, origin);
    const path = resolve(directory, "." + url.pathname + (url.pathname.endsWith("/") ? "index.html" : ""));
    const types = { ".html": "text/html", ".js": "application/javascript", ".css": "text/css", ".json": "application/json" };
    try {
      const body = await readFile(path);
      response.writeHead(200, { "Content-Type": types[extname(path)] ?? "application/octet-stream" });
      response.end(body);
    } catch {
      response.writeHead(404).end();
    }
  });
  await new Promise((ready) => server.listen(0, "127.0.0.1", ready));
  origin = "http://127.0.0.1:" + server.address().port;
});

test.afterAll(async () => {
  server?.close();
  if (directory) await rm(directory, { recursive: true, force: true });
});

async function fakeSerial(page, { cancel = false, disconnectOnce = false, ignoreFirstHello = false } = {}) {
  await page.addInitScript(({ cancel, disconnectOnce, ignoreFirstHello }) => {
    window.commands = [];
    Object.defineProperty(navigator, "serial", {
      value: {
        async requestPort() {
          if (cancel) throw new DOMException("Cancelled", "NotFoundError");
          return {
            async open() {
              let reader;
              this.readable = new ReadableStream({ start(controller) { reader = controller; } });
              this.writable = new WritableStream({
                write(chunk) {
                  const command = new TextDecoder().decode(chunk).trim();
                  window.commands.push(command);
                  if (command === "AIRKEEP_HELLO") {
                    if (ignoreFirstHello) {
                      ignoreFirstHello = false;
                      return;
                    }
                    reader.enqueue(new TextEncoder().encode("boot log\r\nAIRKEEP_RE"));
                    reader.enqueue(new TextEncoder().encode("ADY 1 0.10.0\r\n"));
                  } else if (disconnectOnce) {
                    disconnectOnce = false;
                    reader.close();
                  } else {
                    reader.enqueue(new TextEncoder().encode("AIRKEEP_OK saved\r\n"));
                  }
                },
              });
            },
            async close() {},
          };
        },
      },
    });
  }, { cancel, disconnectOnce, ignoreFirstHello });
}

async function fillSettings(page, password) {
  await page.getByLabel("Wi-Fi name").fill("Test network");
  await page.getByLabel("Wi-Fi password").fill(password);
  await page.getByLabel("Relay HTTPS URL").fill("https://relay.example.org:8443/");
}

test("stages the current and previous stable image and matches the advertised manifest", async ({ page, request }) => {
  await fakeSerial(page);
  await page.goto(origin + "/pages/install/");
  await expect(page.locator("#release-status")).toHaveText("Airkeep v0.10.0");
  const flasher = page.locator("#flasher");
  await expect(flasher).toBeVisible();
  await expect.poll(() => page.evaluate(() => Boolean(customElements.get("esp-web-install-button")))).toBe(true);
  const manifestUrl = new URL(await flasher.getAttribute("manifest"), page.url());
  const manifest = await (await request.get(manifestUrl.href)).json();
  expect(manifest.version).toBe("0.10.0");
  expect(manifest.new_install_prompt_erase).toBe(false);
  expect(manifest.builds[0]).toEqual({
    chipFamily: "ESP32-S3", parts: [{ path: "./airkeep-factory.bin", offset: 0 }],
  });
  const binaryUrl = new URL(manifest.builds[0].parts[0].path, manifestUrl);
  const download = page.getByRole("link", { name: "Download firmware" });
  expect(new URL(await download.getAttribute("href"), page.url()).href).toBe(binaryUrl.href);
  const binary = await (await request.get(binaryUrl.href)).body();
  const checksum = await (await request.get(new URL("SHA256SUMS.txt", manifestUrl).href)).text();
  expect(checksum).toBe(createHash("sha256").update(binary).digest("hex") + "  airkeep-factory.bin\n");
  expect(await (await request.get(origin + "/pages/firmware/v0.5.0/airkeep-factory.bin")).text()).toBe("v0.5.0");
});

test("offers source builds before the first release and downloads in an unsupported browser", async ({ page }) => {
  await page.addInitScript(() => delete Navigator.prototype.serial);
  await page.goto(origin + "/before-release/install/");
  await expect(page.locator("#release-status")).toContainText("A public firmware image is not available yet");
  await expect(page.getByRole("link", { name: "Build from source" })).toBeVisible();
  await page.goto(origin + "/pages/install/");
  await expect(page.locator("#release-status")).toContainText("desktop Chrome or Edge");
  await expect(page.getByRole("link", { name: "Download firmware" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Connect display" })).toBeDisabled();
});

test("handles a cancelled port selection", async ({ page }) => {
  await fakeSerial(page, { cancel: true });
  await page.goto(origin + "/pages/install/");
  await page.getByRole("button", { name: "Connect display" }).click();
  await expect(page.locator("#setup-status")).toContainText("No USB device selected");
  await expect(page.getByRole("button", { name: "Save and restart" })).toBeDisabled();
});

test("retries identification while the display is restarting", async ({ page }) => {
  await fakeSerial(page, { ignoreFirstHello: true });
  await page.goto(origin + "/pages/install/");
  await page.getByRole("button", { name: "Connect display" }).click();
  await expect(page.locator("#setup-status")).toHaveText("Connected to Airkeep 0.10.0.");
  expect(await page.evaluate(() => window.commands)).toEqual(["AIRKEEP_HELLO", "AIRKEEP_HELLO"]);
});

test("identifies Airkeep, rejects invalid settings, and retries after a USB disconnect", async ({ page }) => {
  await fakeSerial(page, { disconnectOnce: true });
  await page.goto(origin + "/pages/install/");
  await page.getByRole("button", { name: "Connect display" }).click();
  await expect(page.locator("#setup-status")).toHaveText("Connected to Airkeep 0.10.0.");
  await fillSettings(page, "short");
  await page.getByRole("button", { name: "Save and restart" }).click();
  await expect(page.locator("#setup-status")).toContainText("Wi-Fi password of 8–63 bytes");
  expect(await page.evaluate(() => window.commands)).toEqual(["AIRKEEP_HELLO"]);
  await fillSettings(page, "test password");
  await page.getByRole("button", { name: "Save and restart" }).click();
  await expect(page.locator("#setup-status")).toContainText(/USB connection closed|USB disconnected/);
  await page.getByRole("button", { name: "Connect display" }).click();
  await page.getByRole("button", { name: "Save and restart" }).click();
  await expect(page.locator("#setup-status")).toContainText("Settings saved");
  await expect(page.getByLabel("Wi-Fi password")).toHaveValue("");
  const commands = await page.evaluate(() => window.commands);
  expect(commands[2]).toBe("AIRKEEP_HELLO");
  expect(JSON.parse(commands[3].slice("AIRKEEP_CONFIG ".length))).toEqual({
    ssid: "Test network", password: "test password", relayUrl: "https://relay.example.org:8443", amsId: "0",
  });
});
