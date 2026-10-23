# Apple signing and development

Open `apple/Airkeep.xcodeproj`, or regenerate it with XcodeGen from `apple/project.yml`. The project has native iOS and Mac apps, each with a widget extension. Shared code lives in `Shared/` and `App/`.

The checked-in project has no development team. Copy `apple/Local.xcconfig.example` to `apple/Local.xcconfig` and set your team ID and a bundle prefix registered to that team. The local file is ignored by Git. `Config.xcconfig` supplies public defaults and optionally includes those local settings.

The default identifiers are:

| Target | Bundle ID |
| --- | --- |
| iOS/iPadOS app | `io.airkeep` |
| iOS widget and Live Activity extension | `io.airkeep.widgets` |
| Mac app | `io.airkeep.mac` |
| Mac widget extension | `io.airkeep.mac.widgets` |

`AIRKEEP_APP_GROUP` and `AIRKEEP_KEYCHAIN_GROUP` derive from `AIRKEEP_BUNDLE_ID_PREFIX`. Keep the prefix, App Group and Keychain group used by an existing installation when updating it so the app can still read its saved connection. All app and extension targets read their App Group from the expanded `AirkeepAppGroup` Info.plist value.

## Sign and connect

1. Register the App Group matching your configured prefix, such as `group.io.airkeep`, in your developer account and enable it on all four targets.
2. Enable the matching shared Keychain access group, such as `$(AppIdentifierPrefix)io.airkeep.shared`, on all targets.
3. Enable Push Notifications on both app IDs, including the main apps.
4. Build and run with automatic signing on a device. An unsigned simulator build checks compilation and UI, but cannot verify device Keychain or APNs entitlements.
5. In Connection, save the relay URL and token. Add a humidity widget through the system gallery and edit it to choose the AMS. The first AMS has ID `0`.
6. In Humidity alerts, enable notifications and save an enabled rule when ready to receive alerts. Debug builds register for sandbox APNs, and Release builds use production. The relay follows each registration's environment.
7. Choose Start Live Activity while a fresh reading is available. Check the eight-hour session's end time and End Live Activity action. A supporting iPhone can show the Dynamic Island views. Other supported iPhones use the Lock Screen.

The apps require iOS/macOS 26 and were built here with Xcode 27.1. They use Swift 6, Observation, async/await, Swift Charts, App Intents and ActivityKit, with no third-party Swift dependencies. Widgets run as system extensions.

## Keychain behavior on Mac

Airkeep uses the data-protection Keychain through `kSecUseDataProtectionKeychain`. The signed access group controls token sharing between the app and widget. Background reads don't ask for interactive authentication.

Earlier builds used the login Keychain, which could repeatedly prompt after a development binary changed. If upgrading from one of those builds, save the relay token again in Connection. It writes to the shared data-protection store and leaves the old item untouched. Simulator and unsigned builds cannot establish that device entitlement behavior works.

## Provision a development device

Debug iOS builds accept `AIRKEEP_DEVELOPMENT_RELAY_URL` and `AIRKEEP_DEVELOPMENT_RELAY_TOKEN` through their process environment. The hook requires HTTPS, fetches an authenticated snapshot, then saves the connection through the normal Keychain path.

Supply these through an untracked local Xcode scheme or `DEVICECTL_CHILD_` environment variables for `devicectl`. For `simctl`, use the `SIMCTL_CHILD_` prefix. Keep real values out of checked-in schemes, command arguments and source. Relaunch without the variables after provisioning. Release builds omit this hook.

The Debug argument `--notification-diagnostics` prints notification authorization and presentation settings, plus the number of delivered Airkeep notifications. It doesn't send a push or log tokens or notification contents.

To update the README images, follow the [screenshot workflow](screenshots.md) for simulator, Mac and LVGL captures.

## Menu bar and shortcuts

Closing the Mac dashboard leaves the app and menu bar running. Launch at login is an opt-in Connection setting backed by `SMAppService`. The app doesn't enable a login item or notification permission on its own.

The App Intent shortcut is "Check humidity in Airkeep". Medium widgets have a refresh button. Small and Lock Screen widgets open the dashboard. Widgets fetch HTTPS snapshots rather than holding an MQTT connection.
