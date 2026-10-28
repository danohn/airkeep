#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"
uv run --directory server pytest -q
npm ci --prefix web
npm run build --prefix web
npm test --prefix web
mkdir -p .local
xcodebuild -project apple/Airkeep.xcodeproj -scheme AirkeepNotificationsTests -destination 'platform=macOS' -derivedDataPath .local/NotificationTests CODE_SIGNING_ALLOWED=NO test
xcodebuild -project apple/Airkeep.xcodeproj -scheme Airkeep-macOS -derivedDataPath .local/DerivedData CODE_SIGNING_ALLOWED=NO build
xcodebuild -project apple/Airkeep.xcodeproj -scheme Airkeep-iOS -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' -derivedDataPath .local/DerivedData-iOS CODE_SIGNING_ALLOWED=NO build

if [[ -f apple/Watch/AirkeepWatchApp.swift ]]; then
  xcodebuild -project apple/Airkeep.xcodeproj -scheme Airkeep-watchOS -destination 'generic/platform=watchOS Simulator' -derivedDataPath .local/DerivedData-Watch CODE_SIGNING_ALLOWED=NO build
fi
