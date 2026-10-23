# Screenshot captures

The root README uses captures of the running apps and LVGL code from 23 October 2026. The image files are in `docs/screenshots/`. Run the commands below from the repository root.

| File | Source | Data |
| --- | --- | --- |
| `ios-simulator.png` | iPhone 18 Pro simulator, iOS 27.0, app 0.1.1 build 2 | Live HTTPS relay readings and history, 23% RH and 25.6°C |
| `macos-dashboard.png` | Signed Mac app, native window capture | The same relay, 23% RH and 25.6°C |
| `macos-menubar.png` | Open Mac menu-bar popover, native window capture | The same relay, 23% RH and 25.6°C |
| `esp32-lvgl.png` | `firmware/main/ui.c` running in the host LVGL renderer at 368×448 | Synthetic history with matching 23% RH and 25.6°C, labelled "Sample data" |

The iPhone image comes from the simulator. The LVGL image is a framebuffer render, not a photograph of the physical display. The Mac dashboard's title bar is cropped out, and Mac window captures are converted to PNG. UI content is otherwise unchanged. No credentials, connection settings or unrelated phone notifications appear in these files.

## Refresh the iPhone capture

Build the app with simulator signing so the shared Keychain group works. Set your development team and bundle prefix in the ignored `apple/Local.xcconfig`.

```sh
xcodebuild -project apple/Airkeep.xcodeproj -scheme Airkeep-iOS \
  -configuration Debug -sdk iphonesimulator \
  -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath .local/ScreenshotSimulator CODE_SIGN_IDENTITY=- build
xcrun simctl install booted \
  .local/ScreenshotSimulator/Build/Products/Debug-iphonesimulator/Airkeep-iOS.app
xcrun simctl launch --terminate-running-process booted YOUR_CONFIGURED_BUNDLE_ID
```

Connect to the same relay as the Mac in Connection settings, or use the private Debug provisioning hook described in [Apple setup](apple-setup.md). Keep credentials out of command arguments, logs and checked-in files. Wait for a fresh reading and history before capturing:

```sh
xcrun simctl io booted screenshot --type=png docs/screenshots/ios-simulator.png
```

## Refresh the Mac captures

Open the signed app connected to the relay and capture its dashboard window. Crop only the title bar. Then open the humidity value in the menu bar and capture the popover without switching to another app first. Save both as PNG and check that the readings match the simulator.

## Refresh the LVGL capture

From the repository root, after the firmware dependencies have resolved:

```sh
cmake -S firmware/preview -B .local/lvgl-preview -G Ninja
cmake --build .local/lvgl-preview --target airkeep-preview
.local/lvgl-preview/airkeep-preview .local/readme-lvgl.ppm
sips -s format png .local/readme-lvgl.ppm --out docs/screenshots/esp32-lvgl.png
```

The sample values live in `firmware/preview/preview.c`. When updating them to match a new capture, keep the "Sample data" label. The host renderer doesn't check panel brightness, wiring or physical touch. Run its existing checks separately:

```sh
ctest --test-dir .local/lvgl-preview --output-on-failure
```
