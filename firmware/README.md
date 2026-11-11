# Desk display

The firmware targets **Waveshare ESP32-S3-Touch-AMOLED-1.8 V2**: CO5300/CST820, 368×448, 16 MB flash and 8 MB octal PSRAM. The BSP is pinned to `2.0.3`. Keep the V2 BSP, even though its compatible touch package may use a CST816-family name. The board's touch controller is CST820.

## Install from your browser

Open the [Airkeep installer](https://airkeep.io/install/) in desktop Chrome or Edge. Connect the board using a USB data cable. The factory install replaces the current firmware and clears saved settings and pairing. Choose **Airkeep hosted** to use `https://dashboard.airkeep.io`, or **Self-hosted relay** to enter your own HTTPS origin.

Close the flasher when it finishes. Connect again in **Configure over USB** and enter your 2.4 GHz Wi-Fi settings, relay HTTPS URL, and AMS ID. The board saves them and restarts. When it connects, tap **Pair display** and approve it on your relay. To change Wi-Fi or relay settings, return to **Configure over USB**.

The [flashing instructions](FLASHING.md) cover downloading and flashing a factory image with esptool.

## Build and configure

From the repository root, run `scripts/idf.sh menuconfig`. Set Wi-Fi credentials, `AIRKEEP_RELAY_URL` (for example `https://airkeep.example.com:8443`) and the AMS ID. Then run `scripts/idf.sh build`.

The wrapper needs an existing ESP-IDF v5.5.x installation. It resolves managed components using the checked-in `dependencies.lock`, but doesn't download a full SDK or flash the board automatically. Saved USB settings take precedence over `menuconfig` defaults. Keep any binaries built with Wi-Fi credentials private.

## Readings and display

The ESP32 fetches `/v1/snapshot` over verified HTTPS every five seconds. Tap **Pair display**, scan the QR code or enter the displayed code at the relay’s `/device` page, then sign in and approve. The display keeps its refresh credential across restarts and needs pairing again after 90 days or 30 days without renewal. Requests time out after ten seconds and reconnect on a later poll. Redirects are rejected, and responses are limited to 16 KiB. Revocation or expiry prompts you to pair again. Network failures display "Relay unavailable".

The chart keeps 120 received samples at 30-second intervals in RAM, then starts over after reboot. It doesn't fill the chart with startup data. The percentage bar spans 0–100%, and zero or invalid humidity values are ignored.

The original `observedAt` timestamp maps to the display's monotonic clock. Polling an old snapshot leaves its age intact. After the relay's `staleAfter` limit, normally 90 seconds, the reading turns grey and is marked stale. An HTTP 200 doesn't make an old sample fresh.

`main/ui.c` builds the dashboard, Details page and brightness slider with LVGL widgets. The large digits use JetBrains Mono Bold under the SIL Open Font License in `main/FONT-LICENSE.txt`, generated with `lv_font_conv@1.5.3`.

## Panel, touch and networking

`main/display.c` starts the vendor CO5300 panel and touch drivers, then attaches them to the SPI LVGL port. A 20-line internal DMA buffer and transfer-completion callbacks avoid the DMA allocation failures seen with BSP 2.0.3's convenience initializer.

Periodic LVGL input reads detect release even when the controller interrupts only on contact. Physical checks have confirmed Details, Back and changes to both the brightness percentage and panel brightness. Host tests cover the hit areas and callback separately.

TLS uses PSRAM for buffers to leave internal RAM for Wi-Fi, task stacks and display DMA. The driver checks the relay hostname against the ESP-IDF public CA bundle. The clock must synchronize before HTTPS polling starts. If `pool.ntp.org` is blocked, set a reachable NTP server rather than bypassing certificate checks.

At boot, the board scans for the configured SSID. If it can't find it, the serial log lists nearby SSIDs so spelling and spacing problems are visible. Passwords and tokens aren't logged. Humidity, temperature and sample age are logged at most once every 30 seconds. HTTPS status changes and Details/Back events are also logged. Details shows the relay URL.

## Host rendering and tests

The host renderer runs the **same `ui.c`** as the board. After resolving the firmware dependencies, run:

```sh
cmake -S firmware/preview -B .local/lvgl-preview -G Ninja
cmake --build .local/lvgl-preview -j 4
ctest --test-dir .local/lvgl-preview --output-on-failure
.local/lvgl-preview/airkeep-preview .local/firmware-preview.ppm
```

The renderer labels its synthetic readings as sample data. Nothing is sent to the printer. Host queue and PMU shims let the UI run without hardware, so this cannot check wiring, panel timing, radio reception, USB flashing or physical brightness.

The touch regression sends a bottom-edge tap observed during board testing through LVGL's input system. It checks Details/Back and the slider callback.

The `relay-snapshot` test reuses cJSON from the installed SDK, discovered through firmware build metadata or `AIRKEEP_CJSON_DIR`. It covers cached sample age, samples older than device uptime, printer-offline state, nullable fields, malformed payloads, wrong AMS IDs, invalid humidity and future timestamps.

Upgrade the relay and dashboard before pairing new firmware. The updated relay rejects the old shared token, so plan to flash and pair any existing displays during the upgrade.
