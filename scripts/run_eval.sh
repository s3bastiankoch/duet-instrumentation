#!/bin/bash
# =============================================================================
# Run Eval: Recompute eval report for an experiment ID
# =============================================================================
# Usage:
#   ./scripts/run_eval.sh exp-97uodhs5
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
RESULTS_ROOT="${REPO_ROOT}/results"

usage() {
  cat << EOF
Usage: $(basename "$0") <experiment-id>

Re-run evaluation for a specific experiment ID. The script will pick the
most recent results folder matching:
  results/<severity>/experiment_<experiment-id>_YYYYMMDD_HHMMSS

Example:
  $(basename "$0") exp-97uodhs5
EOF
  exit 1
}

if [[ $# -ne 1 ]]; then
  usage
fi

EXPERIMENT_ID="$1"

# Find matching experiment folders (newest first).
shopt -s nullglob
matches=("${RESULTS_ROOT}"/*/experiment_"${EXPERIMENT_ID}_"*)
shopt -u nullglob

if [[ ${#matches[@]} -eq 0 ]]; then
  echo "Error: no results found for experiment id '${EXPERIMENT_ID}' under ${RESULTS_ROOT}" >&2
  exit 1
fi

# Pick most recent by mtime.
RESULTS_DIR="$(ls -1dt "${matches[@]}" | head -n 1)"

BASE_SPANS="${RESULTS_DIR}/base-spans"
HEAD_SPANS="${RESULTS_DIR}/head-spans"

if [[ ! -d "${BASE_SPANS}" ]] || [[ ! -d "${HEAD_SPANS}" ]]; then
  echo "Error: expected spans directories not found in ${RESULTS_DIR}" >&2
  echo "  Missing: ${BASE_SPANS} or ${HEAD_SPANS}" >&2
  exit 1
fi

echo "Using results dir: ${RESULTS_DIR}"

cd "${REPO_ROOT}"
uv run otel-agent eval \
  -o "${BASE_SPANS}" \
  -n "${HEAD_SPANS}" \
  --out "${RESULTS_DIR}/eval_report.json" \
  --plot

echo "Eval report written: ${RESULTS_DIR}/eval_report.json"
