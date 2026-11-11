"""Package a clean Airkeep build for GitHub Releases and ESP Web Tools."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FACTORY = "airkeep-factory.bin"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build = args.build.resolve()
    output = args.output.resolve()
    version = (ROOT / "firmware/version.txt").read_text().strip()
    config = (build / "config/sdkconfig.h").read_text()
    for name in ("WIFI_SSID", "WIFI_PASSWORD", "RELAY_URL"):
        if not re.search(rf'^#define CONFIG_AIRKEEP_{name} ""$', config, re.MULTILINE):
            parser.error(f"Public firmware requires a blank CONFIG_AIRKEEP_{name}.")

    flash = json.loads((build / "flasher_args.json").read_text())
    if flash["extra_esptool_args"]["chip"] != "esp32s3":
        parser.error("Expected an ESP32-S3 build.")
    parts = {int(offset, 0): (build / name).resolve() for offset, name in flash["flash_files"].items()}
    if set(parts) != {0, 0x8000, 0x10000} or not all(path.is_relative_to(build) for path in parts.values()):
        parser.error("Unexpected factory flash layout.")

    # ESP-IDF places esp_app_desc_t after the image header and first segment header.
    with parts[0x10000].open("rb") as app:
        app.seek(32)
        magic, embedded_version = struct.unpack("<I12x32s", app.read(48))
    if magic != 0xABCD5432 or embedded_version.rstrip(b"\0") != version.encode():
        parser.error(f"Expected embedded firmware version {version}.")

    output.mkdir(parents=True, exist_ok=True)
    image = output / FACTORY
    settings = flash["flash_settings"]
    flash_options = ["--flash-mode", settings["flash_mode"], "--flash-size", settings["flash_size"],
                     "--flash-freq", settings["flash_freq"]]
    flash_parts = [item for offset, path in sorted(parts.items()) for item in (hex(offset), str(path))]
    subprocess.run([sys.executable, "-m", "esptool", "--chip", "esp32s3", "merge-bin",
                    *flash_options, "-o", str(image), *flash_parts], check=True)
    with image.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    (output / "SHA256SUMS.txt").write_text(f"{digest}  {FACTORY}\n")
    shutil.copyfile(ROOT / "firmware/FLASHING.md", output / "FLASHING.md")


if __name__ == "__main__":
    main()
