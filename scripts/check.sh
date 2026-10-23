#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"
uv run --directory server pytest -q
mkdir -p .local
xcodebuild -project apple/Airkeep.xcodeproj -scheme Airkeep-macOS -derivedDataPath .local/DerivedData CODE_SIGNING_ALLOWED=NO build
xcodebuild -project apple/Airkeep.xcodeproj -scheme Airkeep-iOS -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' -derivedDataPath .local/DerivedData-iOS CODE_SIGNING_ALLOWED=NO build
