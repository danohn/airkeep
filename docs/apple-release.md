# Signed Apple releases

The manual **Apple signed release** workflow builds a stable repository tag into an iOS Store package with its Watch app and widgets, a macOS Store package, and a universal Developer ID macOS DMG. Store packages can be uploaded to App Store Connect for TestFlight processing. The DMG contains an app with a stapled notarization ticket, and the DMG itself is signed, notarized and stapled.

The workflow uses GitHub-hosted `macos-26` runners and the checked-in Xcode project. It runs shared Apple tests before signing. It hashes the completed distributables and attests the final stapled DMG. Choose the channels and upload options when dispatching the workflow. The default options retain signed artifacts in Actions for 30 days. [GitHub macOS runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners), [Apple notarization](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)

## Apple setup

Use an active Apple Developer team with explicit identifiers for the six targets. The iOS and macOS apps share `io.airkeep`. The other identifiers are `io.airkeep.widgets`, `io.airkeep.watch`, `io.airkeep.watch.widgets` and `io.airkeep.macwidgets`. Enable the shared `group.io.airkeep` App Group and shared Keychain access for the profiles. The iOS, Watch and macOS apps also need production Push Notifications. Add macOS as a platform on the iOS App Store Connect record and complete the required configuration, including privacy information, before submitting for review. Verify the bundled privacy manifest against the data collected by the deployed service and the release configuration. [Add platforms](https://developer.apple.com/help/app-store-connect/create-an-app-record/add-platforms/)

Export the Store signing identities and their private keys together as a password-protected PKCS#12 file. Include **Apple Distribution** and **Mac Installer Distribution** identities. Export a **Developer ID Application** identity and its private key as a separate password-protected PKCS#12 file. The direct macOS app and widget targets enable the hardened runtime in the project. The Store and Developer ID profiles must carry the actual registered App Group. [Apple distribution certificates](https://developer.apple.com/help/account/certificates/certificates-overview), [Developer ID distribution](https://help.apple.com/xcode/mac/current/en.lproj/dev033e997ca.html)

Download distribution provisioning profiles for each Store target and ZIP them together. Include both the iOS and macOS profiles for `io.airkeep`. Download Developer ID profiles for the macOS app and widget and ZIP them separately. Use explicit bundle identifiers. The preparation script selects profiles by platform and validates the team, bundle identifier, App Group, expiration and distribution channel. It rejects development and registered-device profiles. It maps the selected profile UUIDs to their individual targets and generates manual-signing export options in private runner storage.

Create a **team App Store Connect API key** with permission to upload the Store apps and use the notarization service. Record its key ID and issuer ID and retain its `.p8` private key. Team keys support `notarytool`. Individual keys have different capabilities. [Creating API keys](https://developer.apple.com/documentation/AppStoreConnectAPI/creating-api-keys-for-app-store-connect-api), [Uploading builds](https://developer.apple.com/help/app-store-connect/manage-builds/upload-builds)

## GitHub configuration

Create an environment named `apple-release` and configure the following environment variables.

| Variable | Value |
| --- | --- |
| `APPLE_TEAM_ID` | Ten-character Apple Developer team identifier |
| `APPLE_BUNDLE_ID_PREFIX` | Registered identifier prefix, normally `io.airkeep` |
| `APPLE_APP_GROUP` | Optional registered App Group override, defaults to `group.` followed by the identifier prefix |

Configure these environment secrets.

| Secret | Contents |
| --- | --- |
| `APPLE_STORE_CERTIFICATES_BASE64` | Base64-encoded PKCS#12 containing Apple Distribution and Mac Installer Distribution identities and private keys |
| `APPLE_STORE_CERTIFICATES_PASSWORD` | Password for the Store PKCS#12 |
| `APPLE_STORE_PROFILES_BASE64` | Base64-encoded ZIP containing all six Store distribution provisioning profiles |
| `APPLE_DEVELOPER_ID_CERTIFICATE_BASE64` | Base64-encoded Developer ID Application PKCS#12 including its private key |
| `APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD` | Password for the Developer ID PKCS#12 |
| `APPLE_DEVELOPER_ID_PROFILES_BASE64` | Base64-encoded ZIP containing the macOS app and widget Developer ID profiles |
| `APP_STORE_CONNECT_KEY_ID` | API key identifier |
| `APP_STORE_CONNECT_ISSUER_ID` | API issuer UUID |
| `APP_STORE_CONNECT_KEY_BASE64` | Base64-encoded `.p8` API private key |

For a single channel, configure the certificate and profiles for that channel along with the shared team, bundle and API-key settings. Encode files without line breaks. On macOS, `base64 -i path/to/file | tr -d '\n' | pbcopy` places the encoded value on the clipboard for entering a secret. Keep the original files and their passwords in private storage.

Signing uses a temporary runner Keychain. The preparation script writes private files with restrictive permissions, and the release script removes its Keychain, installed profiles and private files when it exits. The signed output directory contains distributables and checksums. The direct macOS artifact also contains its attestation bundle. Artifact upload selects that output directory.

The workflow uses OpenID Connect for GitHub attestations. Repository settings and the GitHub plan must support artifact attestations. The build job grants `id-token` and `attestations` write access. Publishing the DMG uses a separate job with `contents` write access. [GitHub artifact attestations](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations)

## Publish a coordinated release

1. Merge the version bump to `main`, tag that commit `vX.Y.Z`, and push the tag. Wait for **Container images** on that tag to publish both versioned images.
2. Run **Publish Airkeep release** on the version tag and supply an unused macOS build number from 1 through 9999. For example, run `gh workflow run release.yml --ref v0.5.2 -f build_number=8`. The workflow checks that the tag is on `main` and that the registry images were built from its commit.
3. The workflow builds the ESP32 firmware and the Developer ID DMG from the tag. It checks the firmware and DMG checksums and verifies the DMG attestation before uploading the files to a draft release. It publishes the draft after all uploads succeed, then dispatches the web installer deployment. If publication fails, rerun the failed jobs to resume the workflow's draft.

## Build for TestFlight or publish a Mac rebuild

1. Merge the intended release source to `main`, update the coordinated repository version and create the corresponding stable `vX.Y.Z` tag. The workflow verifies that the tag resolves to the checked-out commit, is an ancestor of `main`, and matches all versioned source files through `scripts/check-version.py`.
2. Dispatch **Apple signed release** from `main`. Enter the existing tag and an unused build number from 1 through 9999. Select `all`, `store` or `mac-direct`. Every target in the selected archive receives the supplied marketing version and build number.
3. Enable `upload_testflight` to upload the Store archives to App Store Connect. Apple must process an uploaded build before it appears for TestFlight testing. Manage tester groups and release review in App Store Connect. [Apple build uploads](https://developer.apple.com/help/app-store-connect/manage-builds/upload-builds)
4. For a Mac rebuild, select `mac-direct` and enable `publish_dmg`. The workflow publishes the signed DMG, checksum and attestation under `apple-vX.Y.Z-buildN` and keeps the coordinated release marked as latest.
5. Download the Actions artifacts and verify their checksums. For the DMG, verify its provenance with `gh attestation verify Airkeep-X.Y.Z-buildN.dmg --repo danohn/airkeep`. The attested digest covers the distributed DMG after stapling. [Verifying attestations](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations)

The Store export uses Xcode's `app-store-connect` method and manual profile mappings. The optional upload uses `xcodebuild -exportArchive` with `destination=upload` and the API key. Build-number management is disabled so the requested build number remains in the uploaded archive. Inspect the installed Xcode version's supported keys with `xcodebuild -help`. [Xcode archive export files](https://help.apple.com/xcode/mac/current/en.lproj/deva1f2ab5a2.html)

The direct macOS job verifies the app's signature and hardened-runtime flag, submits the app ZIP to Apple's notary service, waits for acceptance, staples and validates the app, then creates the DMG. It signs the DMG, submits it for notarization, waits for acceptance, staples and validates it, and runs Gatekeeper assessment. It computes checksums after those operations. A notarization timeout or rejection fails the job. [Custom notarization workflows](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow)

## Release verification

Publish the privacy notice and verify the App Store privacy answers against the [hosted data inventory](hosted-privacy.md). In the hosted deployment, `docker compose --env-file server/.env.hosted -f server/compose.hosted.yml exec api python hosted_admin.py seed-review` creates an isolated review login with sample AMS history. The command displays its generated credentials once. Enter them in App Review Information, tell reviewers which views use sample telemetry, and delete the review account after review. [App Review guidelines](https://developer.apple.com/app-store/review/guidelines/)

Verify Store builds on an iPhone and Apple Watch through TestFlight, including pairing, account switching, sign-out, widgets, notifications and Live Activities. Verify the direct macOS DMG downloaded through a browser on a Gatekeeper-enabled Mac, including an offline launch after copying the app to Applications. Confirm the hosted service is reachable and that a new installation defaults to `https://dashboard.airkeep.io`. Existing installations keep their saved relay origin.

The repository's automated checks cover shared Apple logic, simulator compilation and release-profile validation. Signed archives, App Store Connect processing, notarization and physical-device behavior require the configured Apple identities and services. Record the actual workflow run and resulting Store build identifiers with the release.
