#!/bin/bash
# =============================================================================
# Run k6 Eval: Recompute k6 eval report for an experiment ID
# =============================================================================
# Usage:
#   ./scripts/run_k6_eval.sh exp-97uodhs5
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
RESULTS_ROOT="${REPO_ROOT}/results"

usage() {
  cat << EOF
Usage: $(basename "$0") <experiment-id>

Re-run k6 eval for a specific experiment ID. The script picks the most recent
results folder matching:
  results/<severity>/experiment_<experiment-id>_YYYYMMDD_HHMMSS
  results/<severity>/simple_experiment_<experiment-id>_YYYYMMDD_HHMMSS

Example:
  $(basename "$0") exp-97uodhs5
EOF
  exit 1
}

if [[ $# -ne 1 ]]; then
  usage
fi

EXPERIMENT_ID="$1"

shopt -s nullglob
matches=(
  "${RESULTS_ROOT}"/*/experiment_"${EXPERIMENT_ID}_"*
  "${RESULTS_ROOT}"/*/simple_experiment_"${EXPERIMENT_ID}_"*
)
shopt -u nullglob

if [[ ${#matches[@]} -eq 0 ]]; then
  echo "Error: no results found for experiment id '${EXPERIMENT_ID}' under ${RESULTS_ROOT}" >&2
  exit 1
fi

RESULTS_DIR="$(ls -1dt "${matches[@]}" | head -n 1)"
K6_JSONL="${RESULTS_DIR}/duet-k6.jsonl"

if [[ ! -f "${K6_JSONL}" ]]; then
  echo "Error: expected k6 jsonl not found: ${K6_JSONL}" >&2
  exit 1
fi

echo "Using results dir: ${RESULTS_DIR}"

cd "${REPO_ROOT}"
uv run python -m src.k6_jsonl_eval \
  --input "${K6_JSONL}" \
  --out "${RESULTS_DIR}/k6_eval_report.json" \
  --plot \
  --plot-dir "${RESULTS_DIR}" \
  --plot-format png

echo "k6 eval report written: ${RESULTS_DIR}/k6_eval_report.json"
