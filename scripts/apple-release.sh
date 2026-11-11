#!/usr/bin/env bash
set -euo pipefail
mode=${1:?Choose ios-store, mac-store or mac-direct}
case "$mode" in
  ios-store) scheme=Airkeep-iOS; destination='generic/platform=iOS' ;;
  mac-store|mac-direct) scheme=Airkeep-macOS; destination='generic/platform=macOS' ;;
  *) echo 'Unknown Apple release channel' >&2; exit 1 ;;
esac
: "${RUNNER_TEMP:?}" "${APPLE_TEAM_ID:?}" "${APPLE_BUNDLE_ID_PREFIX:?}"
: "${APP_STORE_CONNECT_KEY_ID:?}" "${APP_STORE_CONNECT_ISSUER_ID:?}" "${CERTIFICATE_PASSWORD:?}"
: "${RELEASE_TAG:?}" "${BUILD_NUMBER:?}"
[[ "$RELEASE_TAG" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]
[[ "$BUILD_NUMBER" =~ ^[1-9][0-9]{0,3}$ ]]
version=${RELEASE_TAG#v}
private="$RUNNER_TEMP/airkeep-signing-$mode"
release="$RUNNER_TEMP/airkeep-release-$mode"
keychain="$private/signing.keychain-db"
mkdir -p "$release/output"
cleanup() {
  security delete-keychain "$keychain" >/dev/null 2>&1 || true
  if [[ -f "$private/installed-profiles.txt" ]]; then
    while IFS= read -r profile; do rm -f "$profile"; done < "$private/installed-profiles.txt"
  fi
  rm -rf "$private"
}
trap cleanup EXIT
umask 077
uv run --no-project --python 3.14 python scripts/prepare-apple-release.py "$mode" "$private"
keychain_password=$(openssl rand -hex 32)
security create-keychain -p "$keychain_password" "$keychain"
security set-keychain-settings -lut 21600 "$keychain"
security unlock-keychain -p "$keychain_password" "$keychain"
security import "$private/certificates.p12" -k "$keychain" -P "$CERTIFICATE_PASSWORD" -T /usr/bin/codesign -T /usr/bin/productbuild >/dev/null
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$keychain_password" "$keychain" >/dev/null
security list-keychains -d user -s "$keychain" "$HOME/Library/Keychains/login.keychain-db"
unset CERTIFICATE_BASE64 CERTIFICATE_PASSWORD PROFILES_BASE64 APP_STORE_CONNECT_KEY_BASE64
settings=()
while IFS= read -r setting; do settings+=("$setting"); done < "$private/build-settings.txt"
if [[ "$mode" != ios-store ]]; then
  settings+=("ARCHS=arm64 x86_64" "ONLY_ACTIVE_ARCH=NO")
fi
identity='Apple Distribution'
if [[ "$mode" == mac-direct ]]; then identity='Developer ID Application'; fi
xcodebuild -project apple/Airkeep.xcodeproj -scheme "$scheme" -configuration Release \
  -destination "$destination" -archivePath "$release/Airkeep.xcarchive" \
  archive CODE_SIGN_STYLE=Manual DEVELOPMENT_TEAM="$APPLE_TEAM_ID" CODE_SIGN_IDENTITY="$identity" \
  AIRKEEP_BUNDLE_ID_PREFIX="$APPLE_BUNDLE_ID_PREFIX" \
  AIRKEEP_APP_GROUP="${APPLE_APP_GROUP:-group.$APPLE_BUNDLE_ID_PREFIX}" \
  MARKETING_VERSION="$version" CURRENT_PROJECT_VERSION="$BUILD_NUMBER" \
  OTHER_CODE_SIGN_FLAGS=--timestamp "${settings[@]}"
auth=(-authenticationKeyPath "$private/AuthKey.p8" -authenticationKeyID "$APP_STORE_CONNECT_KEY_ID" -authenticationKeyIssuerID "$APP_STORE_CONNECT_ISSUER_ID")
xcodebuild -exportArchive -archivePath "$release/Airkeep.xcarchive" \
  -exportOptionsPlist "$private/ExportOptions.plist" -exportPath "$release/export" "${auth[@]}"
if [[ "$mode" == mac-direct ]]; then
  app="$release/export/Airkeep-macOS.app"
  codesign --verify --deep --strict --verbose=2 "$app"
  codesign -d --verbose=4 "$app" 2>&1 | grep -q 'flags=.*runtime'
  # Staple the app before packaging, so drag-installed copies retain their ticket.
  ditto -c -k --keepParent "$app" "$release/Airkeep-notarization.zip"
  notary=(--key "$private/AuthKey.p8" --key-id "$APP_STORE_CONNECT_KEY_ID" --issuer "$APP_STORE_CONNECT_ISSUER_ID")
  xcrun notarytool submit "$release/Airkeep-notarization.zip" "${notary[@]}" --wait --timeout 60m --output-format json > "$release/app-notary.json"
  uv run --no-project python -c 'import json,sys; assert json.load(open(sys.argv[1]))["status"] == "Accepted"' "$release/app-notary.json"
  xcrun stapler staple "$app"
  xcrun stapler validate "$app"
  spctl --assess --type execute --verbose=2 "$app"
  mkdir "$release/dmg"
  ditto "$app" "$release/dmg/Airkeep.app"
  ln -s /Applications "$release/dmg/Applications"
  dmg="$release/output/Airkeep-$version-build$BUILD_NUMBER.dmg"
  hdiutil create -volname Airkeep -srcfolder "$release/dmg" -ov -format UDZO "$dmg"
  codesign --sign "$identity" --timestamp "$dmg"
  xcrun notarytool submit "$dmg" "${notary[@]}" --wait --timeout 60m --output-format json > "$release/dmg-notary.json"
  uv run --no-project python -c 'import json,sys; assert json.load(open(sys.argv[1]))["status"] == "Accepted"' "$release/dmg-notary.json"
  xcrun stapler staple "$dmg"
  xcrun stapler validate "$dmg"
  codesign --verify --strict --verbose=2 "$dmg"
  spctl --assess --type open --context context:primary-signature --verbose=2 "$dmg"
else
  extension=ipa
  if [[ "$mode" == mac-store ]]; then extension=pkg; fi
  packages=("$release/export/"*."$extension")
  [[ ${#packages[@]} == 1 && -f "${packages[0]}" ]]
  cp "${packages[0]}" "$release/output/Airkeep-$mode-$version-build$BUILD_NUMBER.$extension"
  if [[ "${UPLOAD_TESTFLIGHT:-false}" == true ]]; then
    xcodebuild -exportArchive -archivePath "$release/Airkeep.xcarchive" \
      -exportOptionsPlist "$private/UploadOptions.plist" -exportPath "$release/upload" "${auth[@]}"
  fi
fi
# Stapling mutates the DMG. Hash only the final distributable, which the workflow attests next.
(cd "$release/output" && shasum -a 256 ./* > SHA256SUMS.txt)
