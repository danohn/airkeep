"""Prepare private signing files and target-specific export options on a CI runner."""
import argparse
import base64
from datetime import datetime, timezone
import io
import os
from pathlib import Path
import plistlib
import re
import subprocess
import zipfile


TARGETS = {
    "ios-store": {"": "IOS", ".widgets": "IOS_WIDGETS", ".watch": "WATCH", ".watch.widgets": "WATCH_WIDGETS"},
    "mac-store": {"": "MAC", ".macwidgets": "MAC_WIDGETS"},
    "mac-direct": {"": "MAC", ".macwidgets": "MAC_WIDGETS"},
}


def validate_profile(profile, team, bundle_id, app_group, direct):
    entitlements = profile.get("Entitlements", {})
    identifier = entitlements.get("application-identifier", entitlements.get("com.apple.application-identifier", ""))
    expires = profile.get("ExpirationDate", datetime.min).replace(tzinfo=timezone.utc)
    if profile.get("TeamIdentifier") != [team] or identifier.partition(".")[2] != bundle_id:
        raise ValueError("Provisioning profile team or explicit bundle identifier does not match")
    if expires <= datetime.now(timezone.utc) or entitlements.get("get-task-allow", entitlements.get("com.apple.security.get-task-allow", False)):
        raise ValueError("Provisioning profile expired or permits debugging")
    if profile.get("ProvisionedDevices") or bool(profile.get("ProvisionsAllDevices")) != direct:
        raise ValueError("Provisioning profile does not match the distribution channel")
    if app_group not in entitlements.get("com.apple.security.application-groups", []):
        raise ValueError("Provisioning profile lacks the shared Airkeep App Group")
    if not re.fullmatch(r"[A-Fa-f0-9-]{36}", profile.get("UUID", "")):
        raise ValueError("Provisioning profile UUID is invalid")
    return profile["UUID"]


def decode_private(name, path):
    value = base64.b64decode(os.environ[name], validate=True)
    path.write_bytes(value)
    path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=TARGETS)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    team = os.environ["APPLE_TEAM_ID"]
    prefix = os.environ["APPLE_BUNDLE_ID_PREFIX"]
    if not re.fullmatch(r"[A-Z0-9]{10}", team) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)+", prefix):
        parser.error("Configure a valid APPLE_TEAM_ID and APPLE_BUNDLE_ID_PREFIX")
    directory = args.directory
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    decode_private("CERTIFICATE_BASE64", directory / "certificates.p12")
    decode_private("APP_STORE_CONNECT_KEY_BASE64", directory / "AuthKey.p8")
    required = {prefix + suffix: setting for suffix, setting in TARGETS[args.mode].items()}
    installed = Path.home() / "Library/Developer/Xcode/UserData/Provisioning Profiles"
    installed.mkdir(parents=True, exist_ok=True)
    profiles = {}
    settings = []
    installed_paths = directory / "installed-profiles.txt"
    installed_paths.write_text("")
    app_group = os.environ.get("APPLE_APP_GROUP") or "group." + prefix
    platform = "iOS" if args.mode == "ios-store" else "OSX"
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(os.environ["PROFILES_BASE64"], validate=True))) as archive:
        for item in archive.infolist():
            if Path(item.filename).suffix not in {".mobileprovision", ".provisionprofile"}:
                continue
            # Use a generated filename and never extract paths from the ZIP.
            source = directory / "profile.cms"
            source.write_bytes(archive.read(item))
            decoded = subprocess.run(["security", "cms", "-D", "-i", str(source)], check=True, capture_output=True).stdout
            profile = plistlib.loads(decoded)
            entitlements = profile.get("Entitlements", {})
            identifier = entitlements.get("application-identifier", entitlements.get("com.apple.application-identifier", ""))
            bundle_id = identifier.partition(".")[2]
            if bundle_id not in required or platform not in profile.get("Platform", []):
                continue
            uuid = validate_profile(profile, team, bundle_id, app_group, args.mode == "mac-direct")
            if bundle_id in profiles:
                raise ValueError("Provide exactly one distribution profile per required bundle identifier")
            target = installed / (uuid + Path(item.filename).suffix)
            target.write_bytes(archive.read(item))
            target.chmod(0o600)
            with installed_paths.open("a") as log:
                log.write(str(target) + "\n")
            profiles[bundle_id] = uuid
            settings.append("AIRKEEP_PROFILE_" + required[bundle_id] + "=" + uuid)
    if profiles.keys() != required.keys():
        raise ValueError("Provisioning profiles are missing for " + ", ".join(sorted(required.keys() - profiles.keys())))
    (directory / "build-settings.txt").write_text("\n".join(settings) + "\n")
    options = {
        "method": "developer-id" if args.mode == "mac-direct" else "app-store-connect",
        "destination": "export", "teamID": team, "signingStyle": "manual",
        "signingCertificate": "Developer ID Application" if args.mode == "mac-direct" else "Apple Distribution",
        "provisioningProfiles": profiles, "manageAppVersionAndBuildNumber": False,
        "uploadSymbols": True,
    }
    if args.mode == "mac-store":
        options["installerSigningCertificate"] = "Mac Installer Distribution"
    (directory / "ExportOptions.plist").write_bytes(plistlib.dumps(options))
    options["destination"] = "upload"
    (directory / "UploadOptions.plist").write_bytes(plistlib.dumps(options))


if __name__ == "__main__":
    main()
