#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -z "${IDF_PATH:-}" ]]; then
  for candidate in "${AIRKEEP_IDF_ACTIVATE:-}" "$repo_root/.local/activate-idf.sh" "$HOME/esp/esp-idf/export.sh" "$HOME/Code/esp-idf/export.sh"; do
    if [[ -f "$candidate" ]]; then source "$candidate"; break; fi
  done
fi
if [[ ! -f "${IDF_PATH:-}/tools/idf.py" ]]; then
  echo 'Existing ESP-IDF not found. Source your existing export.sh or set IDF_PATH and IDF_PYTHON_ENV_PATH.' >&2
  exit 1
fi
cd "$repo_root/firmware"
exec uv run --no-project --python "${IDF_PYTHON_ENV_PATH:?Activate the existing ESP-IDF environment}/bin/python" "$IDF_PATH/tools/idf.py" "$@"
