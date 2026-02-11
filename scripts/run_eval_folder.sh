#!/bin/bash
# =============================================================================
# Run Eval (Folder): Recompute eval report for multiple experiments in a folder
# =============================================================================
# Usage:
#   ./scripts/run_eval_folder.sh /path/to/results/1
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

usage() {
  cat << EOF
Usage: $(basename "$0") <experiments-folder>

Re-run evaluation using all span databases from every experiment contained in
the provided folder. The folder is expected to contain one or more subfolders
with:
  <experiment>/base-spans/
  <experiment>/head-spans/

Example:
  $(basename "$0") ./results/1
EOF
  exit 1
}

if [[ $# -ne 1 ]]; then
  usage
fi

INPUT_DIR="$1"

if [[ ! -d "${INPUT_DIR}" ]]; then
  echo "Error: folder not found: ${INPUT_DIR}" >&2
  exit 1
fi

INPUT_DIR="$(cd "${INPUT_DIR}" && pwd)"
OUTPUT_DIR="${INPUT_DIR}"
OUT_REPORT="${OUTPUT_DIR}/eval_report_combined.json"

# Collect experiment directories that contain base/head spans.
exp_dirs=()
for candidate in "${INPUT_DIR}"/*; do
  if [[ -d "${candidate}/base-spans" ]] && [[ -d "${candidate}/head-spans" ]]; then
    exp_dirs+=("${candidate}")
  fi
done

# Also allow the input directory itself to be an experiment folder.
if [[ -d "${INPUT_DIR}/base-spans" ]] && [[ -d "${INPUT_DIR}/head-spans" ]]; then
  exp_dirs+=("${INPUT_DIR}")
fi

if [[ ${#exp_dirs[@]} -eq 0 ]]; then
  echo "Error: no experiment folders with base/head spans found under ${INPUT_DIR}" >&2
  exit 1
fi

TMP_DIR="$(mktemp -d)"
cleanup() { rm -rf "${TMP_DIR}"; }
trap cleanup EXIT

mkdir -p "${TMP_DIR}/base-spans" "${TMP_DIR}/head-spans"

base_count=0
head_count=0
exp_count=0

for exp_dir in "${exp_dirs[@]}"; do
  exp_name="$(basename "${exp_dir}")"
  base_dir="${exp_dir}/base-spans"
  head_dir="${exp_dir}/head-spans"

  for f in "${base_dir}"/*.db*; do
    if [[ -f "${f}" ]]; then
      cp "${f}" "${TMP_DIR}/base-spans/${exp_name}__$(basename "${f}")"
      ((base_count++))
    fi
  done

  for f in "${head_dir}"/*.db*; do
    if [[ -f "${f}" ]]; then
      cp "${f}" "${TMP_DIR}/head-spans/${exp_name}__$(basename "${f}")"
      ((head_count++))
    fi
  done

  ((exp_count++))
done

if [[ ${base_count} -eq 0 ]] || [[ ${head_count} -eq 0 ]]; then
  echo "Error: no span databases found (base=${base_count}, head=${head_count})" >&2
  exit 1
fi

echo "Using experiments: ${exp_count}"
echo "Base span dbs: ${base_count}"
echo "Head span dbs: ${head_count}"
echo "Output report: ${OUT_REPORT}"

cd "${REPO_ROOT}"
uv run otel-agent eval \
  -o "${TMP_DIR}/base-spans" \
  -n "${TMP_DIR}/head-spans" \
  --out "${OUT_REPORT}" \
  --plot \
  --plot-dir "${OUTPUT_DIR}"

echo "Combined eval report written: ${OUT_REPORT}"
