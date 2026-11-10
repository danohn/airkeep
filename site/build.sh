#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: bash site/build.sh OUTPUT_DIRECTORY" >&2
  exit 2
fi

repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
output="$1"
mkdir -p -- "$output"
if [[ -n "$(ls -A -- "$output")" ]]; then
  echo "The output directory must be empty: $output" >&2
  exit 1
fi

mkdir -- "$output/assets" "$output/fonts"
cp -- "$repo/site/index.html" "$repo/site/style.css" "$output/"
cp -- "$repo/site/fonts/dm-serif-display-latin.woff2" "$repo/design/brand/fonts/OFL.txt" "$output/fonts/"
cp -- "$repo/design/brand/logo/airkeep-mark-amber.svg" "$output/assets/mark.svg"
cp -- "$repo/design/brand/github/social-preview.png" "$output/assets/"
cp -- "$repo/docs/screenshots/"{web-dashboard,ios-simulator,macos-menubar,watch-dashboard,esp32-lvgl}.png "$output/assets/"
