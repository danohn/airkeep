#!/usr/bin/env bash
set -euo pipefail

output="$1"
mkdir -p -- "$output"
tags=$(gh api --paginate --slurp "repos/$GITHUB_REPOSITORY/releases?per_page=100" |
  jq -r '
    flatten
    | map(select(.draft == false and .prerelease == false)
          | .tag_name
          | select(test("^v(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$"))
          | {tag: ., version: (ltrimstr("v") | split(".") | map(tonumber))}
          | select(.version >= [0,5,0]))
    | sort_by(.version) | reverse | .[:2][] | .tag
  ')

while IFS= read -r tag; do
  [[ -n "$tag" ]] || continue
  gh release download "$tag" --repo "$GITHUB_REPOSITORY" --dir "$output/$tag" \
    --pattern airkeep-factory.bin --pattern SHA256SUMS.txt --pattern FLASHING.md
done <<< "$tags"
