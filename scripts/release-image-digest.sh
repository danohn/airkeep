#!/usr/bin/env bash
set -euo pipefail

image=$1
version=$2
revision=$3
index=$(docker buildx imagetools inspect "$image:$version" --format '{{json .Manifest}}')
digest=$(jq -er '.digest | select(test("^sha256:[a-f0-9]{64}$"))' <<< "$index")

for architecture in amd64 arm64; do
  manifest=$(jq -er --arg arch "$architecture" '
    [.manifests[] | select(.platform.os == "linux" and .platform.architecture == $arch)]
    | select(length == 1) | .[0].digest
  ' <<< "$index")
  if ! docker buildx imagetools inspect "$image@$manifest" --format '{{json .Image}}' |
    jq -e --arg revision "$revision" --arg version "$version" '
      .config.Labels["org.opencontainers.image.revision"] == $revision and
      .config.Labels["org.opencontainers.image.version"] == $version
    ' > /dev/null; then
    printf '%s:%s (linux/%s) does not match release revision %s and version %s.\n' \
      "$image" "$version" "$architecture" "$revision" "$version" >&2
    exit 1
  fi
done
printf '%s\n' "$digest"
