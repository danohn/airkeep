# Screenshot captures

The README images live in `docs/screenshots/`. Capture the running apps from the same revision and use matching humidity and temperature values across the gallery. Run the commands below from the repository root.

| File | Source | Data |
| --- | --- | --- |
| `ios-simulator.png` | iPhone simulator running the iOS app | Live HTTPS relay readings and history |
| `macos-dashboard.png` | Signed Mac app, native window capture | The same relay |
| `macos-menubar.png` | Open Mac menu-bar popover, native window capture | The same relay |
| `watch-dashboard.png` | Watch simulator connected through the paired iPhone app | The same relay |
| `web-dashboard.png` | Browser dashboard, with browser chrome cropped out | The same relay and seven-day history |
| `esp32-lvgl.png` | `firmware/main/ui.c` running in the host LVGL renderer at 368×448 | Matching sample values and synthetic history, labelled "Sample data" |

Crop the Mac dashboard's title bar and the browser's tabs, address bar and bookmarks. Save the gallery images as PNG and inspect each one for credentials, personal hostnames, device identifiers and unrelated notifications. Keep original captures and temporary tooling in ignored local storage.

## Refresh the iPhone capture

Build the app with simulator signing so the shared Keychain group works. Set your development team and bundle prefix in the ignored `apple/Local.xcconfig`.

```sh
xcodebuild -project apple/Airkeep.xcodeproj -scheme Airkeep-iOS \
  -configuration Debug \
  -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath .local/ScreenshotSimulator CODE_SIGN_IDENTITY=- build
xcrun simctl install IPHONE_SIMULATOR_UDID \
  .local/ScreenshotSimulator/Build/Products/Debug-iphonesimulator/Airkeep-iOS.app
xcrun simctl launch --terminate-running-process IPHONE_SIMULATOR_UDID YOUR_CONFIGURED_BUNDLE_ID
```

Connect to the same relay as the Mac in Settings → Relay, or use the private Debug provisioning hook described in [Apple setup](apple-setup.md). Keep credentials out of command arguments, logs and checked-in files. Wait for a fresh reading and history before capturing:

```sh
xcrun simctl io IPHONE_SIMULATOR_UDID screenshot --type=png docs/screenshots/ios-simulator.png
```

## Refresh the Mac captures

Open the signed app connected to the relay and capture its dashboard window. Crop only the title bar. Then open the humidity value in the menu bar and capture the popover without switching to another app first. Save both as PNG and check that the readings match the simulator.

## Refresh the Watch capture

Boot paired iPhone and Watch simulators. The iOS build above also builds the embedded Watch app and widget extension. Install the matching Watch product:

```sh
xcrun simctl install WATCH_SIMULATOR_UDID \
  .local/ScreenshotSimulator/Build/Products/Debug-watchsimulator/Airkeep-watchOS.app
xcrun simctl launch WATCH_SIMULATOR_UDID YOUR_CONFIGURED_BUNDLE_ID.watch
```

In the simulated iPhone app, choose Settings → Relay → Apple Watch → Connect Apple Watch. Wait for the connection acknowledgement and a fresh reading on the Watch before capturing:

```sh
xcrun simctl io WATCH_SIMULATOR_UDID screenshot --type=png docs/screenshots/watch-dashboard.png
```

## Refresh the web capture

Sign in to the relay's browser dashboard and open User → Overview. Select the seven-day history range and wait for fresh readings and the chart to load. Capture the page, then crop out browser controls and any desktop content. Restore any temporary browser zoom after the capture.

Admin captures need an additional privacy check: app topics, registration identifiers and token fingerprints can identify a private installation. Use an isolated relay with example registrations when documenting those screens.

## Refresh the LVGL capture

From the repository root, after the firmware dependencies have resolved:

```sh
cmake -S firmware/preview -B .local/lvgl-preview -G Ninja
cmake --build .local/lvgl-preview --target airkeep-preview
.local/lvgl-preview/airkeep-preview .local/readme-lvgl.ppm
sips -s format png .local/readme-lvgl.ppm --out docs/screenshots/esp32-lvgl.png
```

The sample values live in `firmware/preview/preview.c`. Use a local copy to match the captured humidity and temperature, retaining the "Sample data" label. Keep capture-only value changes outside the source commit. The host renderer checks UI behavior; panel brightness, wiring and physical touch need hardware checks. Run the existing host checks separately:

```sh
ctest --test-dir .local/lvgl-preview --output-on-failure
```
