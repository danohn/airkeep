#!/usr/bin/env bash
set -euo pipefail

: "${GITHUB_REF_NAME:?}" "${GITHUB_REPOSITORY:?}" "${GITHUB_SHA:?}" "${BUILD_NUMBER:?}" "${RUNNER_TEMP:?}"
: "${RELAY_IMAGE:?}" "${RELAY_DIGEST:?}" "${WEB_IMAGE:?}" "${WEB_DIGEST:?}"

tag=$GITHUB_REF_NAME
version=${tag#v}
mac="Airkeep-$version-build$BUILD_NUMBER"
assets=(airkeep-factory.bin SHA256SUMS.txt FLASHING.md "$mac.dmg" "$mac-SHA256SUMS.txt" "$mac-attestation.json")
draft_marker='<!-- airkeep-coordinated-release -->'

verify_assets() (
  cd "$1"
  test -s FLASHING.md
  test "$(sha256sum airkeep-factory.bin)" = "$(cat SHA256SUMS.txt)"
  test "$(sha256sum "./$mac.dmg")" = "$(cat "$mac-SHA256SUMS.txt")"
  gh attestation verify "$mac.dmg" --bundle "$mac-attestation.json" --repo "$GITHUB_REPOSITORY" \
    --signer-workflow "$GITHUB_REPOSITORY/.github/workflows/apple-build.yml" \
    --source-ref "refs/tags/$tag" --source-digest "$GITHUB_SHA"
)

work=$(mktemp -d "$RUNNER_TEMP/coordinated-release.XXXXXXXX")
if gh release view "$tag" --json isDraft,isPrerelease,body > "$work/state.json" 2> "$work/lookup-error"; then
  if jq -e '(.isDraft or .isPrerelease) | not' "$work/state.json" > /dev/null; then
    # A retry after publication verifies the existing immutable assets.
    patterns=()
    for asset in "${assets[@]}"; do patterns+=(--pattern "$asset"); done
    gh release download "$tag" --dir "$work" "${patterns[@]}"
    verify_assets "$work"
    exit 0
  fi
  if ! jq -e --arg marker "$draft_marker" '.isDraft and (.isPrerelease | not) and (.body | contains($marker))' "$work/state.json" > /dev/null; then
    echo "An existing draft or prerelease for $tag needs review before publication." >&2
    exit 1
  fi
elif ! grep -qi 'release not found' "$work/lookup-error"; then
  cat "$work/lookup-error" >&2
  exit 1
fi

cp "$RUNNER_TEMP/firmware-release/"{airkeep-factory.bin,SHA256SUMS.txt,FLASHING.md} "$work/"
cp "$RUNNER_TEMP/mac-release/$mac.dmg" "$work/"
cp "$RUNNER_TEMP/mac-release/SHA256SUMS.txt" "$work/$mac-SHA256SUMS.txt"
cp "$RUNNER_TEMP/mac-release/attestation.json" "$work/$mac-attestation.json"
verify_assets "$work"

if [[ ! -s "$work/state.json" ]]; then
  # Markdown code spans are literal backticks in the release notes.
  # shellcheck disable=SC2016
  {
    printf '%s\n\n' "$draft_marker"
    printf 'Both container images were built from commit `%s`.\n\n' "$GITHUB_SHA"
    printf '| Service | Image tag | Image index digest |\n| --- | --- | --- |\n'
    printf '| Relay | `%s:%s` | `%s` |\n' "$RELAY_IMAGE" "$version" "$RELAY_DIGEST"
    printf '| Dashboard | `%s:%s` | `%s` |\n\n' "$WEB_IMAGE" "$version" "$WEB_DIGEST"
    printf 'Set `AIRKEEP_IMAGE_TAG=%s` to deploy both images together. See the [upgrade and rollback guide](https://github.com/%s/blob/%s/docs/docker-deployment.md#updates-and-rollback).\n\n' "$version" "$GITHUB_REPOSITORY" "$tag"
    printf 'The Mac DMG is signed with Developer ID and notarized. It includes native code for Apple silicon and Intel Macs. The checksum and build attestation are attached.\n\n'
    printf 'The factory firmware supports the Waveshare ESP32-S3-Touch-AMOLED-1.8 V2. Use the [web installer](https://airkeep.io/install/) or the attached flashing instructions. Factory installation clears saved settings and pairing.\n'
  } > "$work/release-notes.md"
  gh release create "$tag" --verify-tag --draft --target "$GITHUB_SHA" --title "Airkeep $version" \
    --generate-notes --notes-file "$work/release-notes.md"
fi

# Only drafts created by this workflow can be resumed and have assets replaced.
for index in "${!assets[@]}"; do assets[index]="$work/${assets[index]}"; done
gh release upload "$tag" "${assets[@]}" --clobber

revision=$(gh api "repos/$GITHUB_REPOSITORY/commits/$tag" --jq .sha)
if [[ "$revision" != "$GITHUB_SHA" ]]; then
  echo "The source tag $tag changed during the release. The release is still a draft." >&2
  exit 1
fi
gh release edit "$tag" --verify-tag --draft=false --latest
