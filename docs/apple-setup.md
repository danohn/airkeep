# Apple signing and development

Open `apple/Airkeep.xcodeproj`, or regenerate it with XcodeGen from `apple/project.yml`. The project has native iOS, Mac and Watch apps, each with a widget extension. The iOS target embeds the Watch companion. Shared Apple app code lives in `Shared/` and `App/`; Watch app and widget code also use `WatchShared/`.

The checked-in project has no development team. Copy `apple/Local.xcconfig.example` to `apple/Local.xcconfig` and set your team ID and a bundle prefix registered to that team. The local file is ignored by Git. `Config.xcconfig` supplies public defaults and optionally includes those local settings.

The default identifiers are:

| Target | Bundle ID |
| --- | --- |
| iOS/iPadOS app | `io.airkeep` |
| iOS widget and Live Activity extension | `io.airkeep.widgets` |
| Mac app | `io.airkeep.mac` |
| Mac widget extension | `io.airkeep.mac.widgets` |
| Watch companion | `io.airkeep.watch` |
| Watch widget extension | `io.airkeep.watch.widgets` |

`AIRKEEP_APP_GROUP` and `AIRKEEP_KEYCHAIN_GROUP` derive from `AIRKEEP_BUNDLE_ID_PREFIX`. Keep the prefix, App Group and Keychain group used by an existing installation when updating it so the app can still read its saved connection. All app and extension targets read their App Group from the expanded `AirkeepAppGroup` Info.plist value.

## Sign and connect

1. Register the App Group matching your configured prefix, such as `group.io.airkeep`, in your developer account and enable it on all six app and widget targets.
2. Enable the matching shared Keychain access group, such as `$(AppIdentifierPrefix)io.airkeep.shared`, on all targets.
3. Provision the push entitlements used by the iOS, Mac and Watch apps and the Watch widget extension. Set the relay's iOS, Mac and Watch topics to the corresponding app bundle IDs. [Watch widget push setup](web-dashboard.md#watch-widget-updates) covers its separate key configuration.
4. Build and run with automatic signing on a device. An unsigned simulator build checks compilation and UI, but cannot verify device Keychain or APNs entitlements.
5. In Settings → Relay, save the relay URL and token. Add a humidity widget through the system gallery and edit it to choose the AMS. The first AMS has ID `0`.
6. In Settings → Notifications, enable Receive alerts on this device. In Settings → Humidity alerts, save and enable the shared rules you want. Debug builds register for sandbox APNs, and Release builds use production. The relay follows each registration's environment.
7. In Settings → Live Activities on iPhone, choose Start Live Activity while a fresh reading is available. Check the eight-hour session's end time and End Live Activity action. A supporting iPhone can show the Dynamic Island views. Other supported iPhones use the Lock Screen.

The apps target iOS, iPadOS, macOS and watchOS 26 or later. Build with Xcode 26 or later and the matching platform support. They use Swift 6, Observation, async/await, Swift Charts, App Intents and ActivityKit, with no third-party Swift dependencies. Widgets run as system extensions.

## Connect Apple Watch

Build and run `Airkeep-iOS` on the paired iPhone, then install Airkeep through the iPhone's Watch app. For a development build, register the Watch with your developer team, include it in the Watch app and widget provisioning profiles, and enable Developer Mode on the Watch when prompted. Use the `Airkeep-watchOS` scheme for Watch simulator or device development.

After saving a working relay connection on iPhone, open Settings → Relay → Apple Watch and choose Connect Apple Watch. The Watch acknowledges the transferred connection and stores its credentials in Keychain. Use Update Watch connection after changing relay credentials, or Disconnect Apple Watch to remove the connection.

Add Airkeep through the Watch's complication picker or Smart Stack, then choose an AMS. Complications support circular, corner, inline and rectangular slots; the rectangular widget also appears in Smart Stack. Tapping opens the selected AMS in the Watch app.

The app refreshes readings every 15 seconds while active and pauses polling while inactive or ambient. History offers 1h, 6h and 24h ranges. Shared alerts are viewable on the Watch and editable on the other Apple apps or web dashboard.

The Watch app tries direct relay HTTPS first, then asks a reachable iPhone for readings if needed. Widgets use direct HTTPS and cached fallback. Original reading timestamps survive both routes. Watch widget push registration is separate from humidity notification forwarding through iPhone; Apple controls timeline scheduling.

## Keychain behavior on Mac

Airkeep uses the data-protection Keychain through `kSecUseDataProtectionKeychain`. The signed access group controls token sharing between the app and widget. Background reads don't ask for interactive authentication.

Earlier builds used the login Keychain, which could repeatedly prompt after a development binary changed. If upgrading from one of those builds, save the relay token again in Settings → Relay. It writes to the shared data-protection store and leaves the old item untouched. Simulator and unsigned builds cannot establish that device entitlement behavior works.

## Provision a development device

Debug iOS builds accept `AIRKEEP_DEVELOPMENT_RELAY_URL` and `AIRKEEP_DEVELOPMENT_RELAY_TOKEN` through their process environment. The hook requires HTTPS, fetches an authenticated snapshot, then saves the connection through the normal Keychain path.

Supply these through an untracked local Xcode scheme or `DEVICECTL_CHILD_` environment variables for `devicectl`. For `simctl`, use the `SIMCTL_CHILD_` prefix. Keep real values out of checked-in schemes, command arguments and source. Relaunch without the variables after provisioning. Release builds omit this hook.

The Debug argument `--notification-diagnostics` prints notification authorization and presentation settings, plus the number of delivered Airkeep notifications. It doesn't send a push or log tokens or notification contents.

To update the README images, follow the [screenshot workflow](screenshots.md) for simulator, Mac and LVGL captures.

## Menu bar and shortcuts

Closing the Mac dashboard leaves the app and menu bar running. Launch at login is an opt-in setting in the settings hub, backed by `SMAppService`. Airkeep shows when system approval is required.

The App Intent shortcut is "Check humidity in Airkeep". Medium widgets have a refresh button. Small and Lock Screen widgets open the dashboard. Widgets fetch HTTPS snapshots rather than holding an MQTT connection.
