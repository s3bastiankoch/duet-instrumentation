#!/bin/bash
# =============================================================================
# Run Multiple Experiments in Parallel (Terraform workspaces)
# =============================================================================
# Wrapper for scripts/run_experiment.sh that launches N experiments concurrently
# with unique experiment IDs.
# =============================================================================
set -euo pipefail

# Route all standard output to stderr by default.
exec 1>&2

PARALLEL_COUNT=""
ID_PREFIX=""
RUN_ARGS=()

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER="${SCRIPT_DIR}/run_experiment.sh"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Colors
GREEN='\033[32m'
BLUE='\033[34m'
YELLOW='\033[33m'
RED='\033[31m'
NC='\033[0m'

log() { echo -e "${BLUE}[$(date +%H:%M:%S)]${NC} $1"; }
success() { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[✗]${NC} $1" >&2; }

usage() {
  cat << EOF
Usage: $(basename "$0") --parallel N [--experiment-id-prefix PREFIX] [-- ...]

Launch N experiments concurrently, each in its own Terraform workspace.
All remaining arguments are passed through to scripts/run_experiment.sh.

Required:
  --parallel N                Number of experiments to run at the same time

Optional:
  --experiment-id-prefix STR  Prefix for experiment IDs
                              (default: exp-batch-<timestamp>)
  -h, --help                  Show this help

Examples:
  # Run 4 experiments concurrently (same args for each)
  $(basename "$0") --parallel 4 -- --jwt-secret "secret" --duration 120s

  # Use a custom ID prefix
  $(basename "$0") --parallel 3 --experiment-id-prefix exp-jan22 -- \\
    --jwt-secret "secret" --severity 10 --duration 120s
EOF
  exit 0
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --parallel|-p) PARALLEL_COUNT="$2"; shift 2 ;;
    --experiment-id-prefix) ID_PREFIX="$2"; shift 2 ;;
    -h|--help) usage ;;
    --) shift; RUN_ARGS+=("$@"); break ;;
    *) RUN_ARGS+=("$1"); shift ;;
  esac
done

if [[ -z "$PARALLEL_COUNT" ]]; then
  error "--parallel is required"
  usage
fi

if ! [[ "$PARALLEL_COUNT" =~ ^[0-9]+$ ]] || [[ "$PARALLEL_COUNT" -le 0 ]]; then
  error "--parallel must be a positive integer (got: ${PARALLEL_COUNT})"
  exit 1
fi

if [[ -z "$ID_PREFIX" ]]; then
  ID_PREFIX="exp-batch-$(date +%Y%m%d_%H%M%S)"
fi

# Disallow passing explicit experiment IDs or destroy mode in passthrough args.
for arg in "${RUN_ARGS[@]}"; do
  if [[ "$arg" == "--experiment-id" ]] || [[ "$arg" == "--destroy" ]]; then
    error "Do not pass --experiment-id or --destroy; IDs are managed by this wrapper."
    exit 1
  fi
done

cd "$REPO_ROOT"

RUN_ID="$(date +%Y%m%d_%H%M%S)"
LOG_DIR="${REPO_ROOT}/results/parallel_${RUN_ID}"
mkdir -p "$LOG_DIR"

log "Launching ${PARALLEL_COUNT} experiments in parallel"
log "ID prefix: ${ID_PREFIX}"
log "Logs: ${LOG_DIR}"

PIDS=()
IDS=()

cleanup_on_interrupt() {
  echo ""
  warn "Interrupted! Stopping running experiments..."
  for pid in "${PIDS[@]:-}"; do
    if [[ -n "$pid" ]]; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  exit 130
}
trap cleanup_on_interrupt SIGINT SIGTERM

for i in $(seq 1 "$PARALLEL_COUNT"); do
  EXP_ID="${ID_PREFIX}-${i}"
  LOG_FILE="${LOG_DIR}/${EXP_ID}.log"
  IDS+=("$EXP_ID")

  log "Starting ${EXP_ID} (log: ${LOG_FILE})"
  "${RUNNER}" --experiment-id "${EXP_ID}" "${RUN_ARGS[@]}" >"${LOG_FILE}" 2>&1 &
  PIDS+=("$!")
done

EXIT_CODE=0
for idx in "${!PIDS[@]}"; do
  pid="${PIDS[$idx]}"
  exp="${IDS[$idx]}"
  if wait "$pid"; then
    success "Completed ${exp}"
  else
    error "Failed ${exp} (log: ${LOG_DIR}/${exp}.log)"
    EXIT_CODE=1
  fi
done

if [[ "$EXIT_CODE" -ne 0 ]]; then
  error "One or more experiments failed. Check logs under: ${LOG_DIR}"
  exit "$EXIT_CODE"
fi

success "All experiments completed successfully"
