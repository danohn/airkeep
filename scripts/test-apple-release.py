"""Exercise release profile validation with synthetic metadata and no signing secrets."""
import base64
from copy import deepcopy
import io
import os
import plistlib
from tempfile import TemporaryDirectory
from unittest.mock import patch
import zipfile
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("apple_release", Path(__file__).with_name("prepare-apple-release.py"))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseProfileTests(unittest.TestCase):
    def setUp(self):
        self.profile = {
            "UUID": "12345678-1234-1234-1234-123456789ABC",
            "TeamIdentifier": ["ABCD123456"],
            "ExpirationDate": datetime.now() + timedelta(days=30),
            "Platform": ["OSX"],
            "Entitlements": {"application-identifier": "ABCD123456.io.airkeep",
                             "com.apple.security.application-groups": ["group.io.airkeep"]},
        }

    def check(self, profile, direct=False):
        return release.validate_profile(profile, "ABCD123456", "io.airkeep", "group.io.airkeep", direct)

    def test_store_and_developer_id_profiles(self):
        self.assertEqual(self.check(self.profile), self.profile["UUID"])
        self.profile["ProvisionsAllDevices"] = True
        self.assertEqual(self.check(self.profile, direct=True), self.profile["UUID"])
        with self.assertRaises(ValueError):
            self.check(self.profile)

    def test_wrong_team_bundle_group_expired_or_debuggable_profiles(self):
        cases = [
            {"TeamIdentifier": ["OTHER12345"]},
            {"ExpirationDate": datetime.now() - timedelta(days=1)},
            {"ProvisionedDevices": ["device"]},
            {"UUID": "../unsafe"},
            {"Entitlements": {"application-identifier": "ABCD123456.*"}},
            {"Entitlements": {**self.profile["Entitlements"], "get-task-allow": True}},
            {"Entitlements": {**self.profile["Entitlements"], "com.apple.security.application-groups": []}},
        ]
        for change in cases:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.check({**deepcopy(self.profile), **change})

    def test_preparation_maps_each_target_and_writes_private_export_options(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = io.BytesIO()
            metadata = {}
            with zipfile.ZipFile(archive, "w") as profiles:
                for index, suffix in enumerate(["", ".macwidgets"]):
                    profile = deepcopy(self.profile)
                    profile["UUID"] = f"12345678-1234-1234-1234-{index:012d}"
                    profile["Entitlements"]["application-identifier"] = "ABCD123456.io.airkeep" + suffix
                    name = str(index) + ".provisionprofile"
                    profiles.writestr("nested/" + name, name)
                    metadata[name.encode()] = plistlib.dumps(profile)
                ios_profile = deepcopy(self.profile)
                ios_profile["Platform"] = ["iOS"]
                profiles.writestr("nested/ios.mobileprovision", "ios")
                metadata[b"ios"] = plistlib.dumps(ios_profile)
            environment = {
                "APPLE_TEAM_ID": "ABCD123456", "APPLE_BUNDLE_ID_PREFIX": "io.airkeep",
                "CERTIFICATE_BASE64": base64.b64encode(b"synthetic-p12").decode(),
                "APP_STORE_CONNECT_KEY_BASE64": base64.b64encode(b"synthetic-p8").decode(),
                "PROFILES_BASE64": base64.b64encode(archive.getvalue()).decode(),
            }
            def decode(command, **kwargs):
                data = metadata[Path(command[-1]).read_bytes()]
                return type("Result", (), {"stdout": data})()
            private = directory / "private"
            with patch.dict(os.environ, environment, clear=True), patch.object(Path, "home", return_value=directory), \
                 patch.object(release.subprocess, "run", side_effect=decode), \
                 patch("sys.argv", ["prepare", "mac-store", str(private)]):
                release.main()
            options = plistlib.loads((private / "ExportOptions.plist").read_bytes())
            self.assertEqual(options["method"], "app-store-connect")
            self.assertEqual(options["destination"], "export")
            self.assertFalse(options["manageAppVersionAndBuildNumber"])
            self.assertEqual(set(options["provisioningProfiles"]), {"io.airkeep", "io.airkeep.macwidgets"})
            self.assertIn("AIRKEEP_PROFILE_MAC_WIDGETS=", (private / "build-settings.txt").read_text())
            self.assertEqual((private / "AuthKey.p8").stat().st_mode & 0o777, 0o600)
            self.assertEqual(len((private / "installed-profiles.txt").read_text().splitlines()), 2)
            self.assertEqual(plistlib.loads((private / "UploadOptions.plist").read_bytes())["destination"], "upload")

    def test_mac_entitlement_name_and_distribution_channel(self):
        entitlements = self.profile["Entitlements"]
        entitlements["com.apple.application-identifier"] = entitlements.pop("application-identifier")
        self.assertEqual(self.check(self.profile), self.profile["UUID"])
        with self.assertRaises(ValueError):
            self.check(self.profile, direct=True)


if __name__ == "__main__":
    unittest.main()
