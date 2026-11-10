"""Check the Airkeep release version across source and generated files."""
import argparse
import json
from pathlib import Path
import re
import tomllib


ROOT = Path(__file__).resolve().parents[1]
VERSION = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="Require this release tag to match the source version.")
    args = parser.parse_args()
    version = tomllib.loads(read(".bumpversion.toml"))["tool"]["bumpversion"]["current_version"]
    if not re.fullmatch(VERSION, version):
        parser.error(f"Expected a stable X.Y.Z version in .bumpversion.toml, found {version!r}")

    project = tomllib.loads(read("server/pyproject.toml"))["project"]
    relay_lock = tomllib.loads(read("server/uv.lock"))["package"]
    web_lock = json.loads(read("web/package-lock.json"))
    versions = {
        "server/pyproject.toml": [project["version"]],
        "server/uv.lock": [package["version"] for package in relay_lock
                           if package["name"] == project["name"] and package["source"] == {"virtual": "."}],
        "server/app.py": re.findall(r'FastAPI\([^\n]*\bversion="([^"]+)"', read("server/app.py")),
        "web/package.json": [json.loads(read("web/package.json"))["version"]],
        "web/package-lock.json": [web_lock["version"], web_lock["packages"][""]["version"]],
        "apple/project.yml": re.findall(r"^\s*MARKETING_VERSION: '([^']+)'", read("apple/project.yml"), re.M),
        "apple/Airkeep.xcodeproj/project.pbxproj": re.findall(
            r"\bMARKETING_VERSION = ([^;]+);", read("apple/Airkeep.xcodeproj/project.pbxproj")),
        "firmware/version.txt": [read("firmware/version.txt").strip()],
    }
    errors = [f"{path} has {values or 'no version'}" for path, values in versions.items()
              if not values or any(value != version for value in values)]
    if args.tag is not None and args.tag != f"v{version}":
        errors.append(f"Git tag is {args.tag!r}, expected 'v{version}'")
    if errors:
        parser.exit(1, f"Expected Airkeep {version}\n" + "\n".join(errors) + "\n")
    print(version)


if __name__ == "__main__":
    main()
