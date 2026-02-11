#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <results_dir>" >&2
  exit 1
fi

RESULTS_DIR="$1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="${SCRIPT_DIR}/analyze_localization_reports.py"

uv run python "${PY_SCRIPT}" "${RESULTS_DIR}"
