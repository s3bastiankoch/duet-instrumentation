#!/usr/bin/env bash
# =============================================================================
# Run Instrumentation + Localization Eval (no cloud benchmark)
# =============================================================================
set -euo pipefail

# Route all standard output to stderr by default.
exec 1>&2

# Defaults
OUTPUT_DIR="./results"
FORCE=""
SEVERITY_PCT=""
EXPERIMENT_ID=""
CHANGES_FILE_ABS=""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Colors
GREEN='\033[32m'
BLUE='\033[34m'
YELLOW='\033[33m'
RED='\033[31m'
NC='\033[0m'

log() { echo -e "${BLUE}[$(date +%H:%M:%S)]${NC} [$EXPERIMENT_ID] $1"; }
success() { echo -e "${GREEN}[✓]${NC} [$EXPERIMENT_ID] $1"; }
warn() { echo -e "${YELLOW}[!]${NC} [$EXPERIMENT_ID] $1"; }
error() { echo -e "${RED}[✗]${NC} [$EXPERIMENT_ID] $1" >&2; }

generate_experiment_id() {
    # With pipefail, head exiting early can SIGPIPE tr. Mask that.
    LC_ALL=C tr -dc 'a-z0-9' < /dev/urandom | head -c 8 || true
}

usage() {
    cat << EOF
Usage: $(basename "$0") [OPTIONS]

Run instrumentation and localization-quality evaluation without cloud benchmarking.

Options:
    --experiment-id ID     Unique experiment ID (auto-generated if not provided)
    --severity PCT         Experiment severity percentage (selects mock changes file:
                           examples/mock_git/movie_perf_<pct>pct.json). Also recorded in metadata.
    -o, --output DIR       Results directory (default: ./results)
    -f, --force            Force re-instrumentation
    -h, --help             Show this help
EOF
    exit 0
}

# Parse args
while [[ $# -gt 0 ]]; do
    case $1 in
        --experiment-id) EXPERIMENT_ID="$2"; shift 2 ;;
        --severity) SEVERITY_PCT="$2"; shift 2 ;;
        -o|--output) OUTPUT_DIR="$2"; shift 2 ;;
        -f|--force) FORCE="-f"; shift ;;
        -h|--help) usage ;;
        *) echo "Unknown option: $1"; usage ;;
    esac
done

# Generate experiment ID if not provided
if [[ -z "$EXPERIMENT_ID" ]]; then
    EXPERIMENT_ID="exp-$(generate_experiment_id)"
fi

# Validate experiment ID format
if ! [[ "$EXPERIMENT_ID" =~ ^[a-z0-9-]+$ ]]; then
    error "Experiment ID must contain only lowercase letters, numbers, and hyphens"
    exit 1
fi

cd "$REPO_ROOT"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RESULTS_BASE="${OUTPUT_DIR}"
if [[ -n "${SEVERITY_PCT}" ]]; then
    RESULTS_BASE="${OUTPUT_DIR}/${SEVERITY_PCT}"
fi
RESULTS="${RESULTS_BASE}/experiment_${EXPERIMENT_ID}_${TIMESTAMP}"
mkdir -p "$RESULTS"

# If --severity is set, generate a config that points to the matching mock diff.
CONFIG_PATH=""
if [[ -n "${SEVERITY_PCT}" ]]; then
    if ! [[ "${SEVERITY_PCT}" =~ ^[0-9]+$ ]]; then
        error "--severity must be an integer percentage (got: ${SEVERITY_PCT})"
        exit 1
    fi

    MOCK_CHANGES_FILE="examples/mock_git/movie_perf_${SEVERITY_PCT}pct.json"
    CHANGES_FILE_ABS="$(uv run python -c "from pathlib import Path; print(str((Path('${REPO_ROOT}')/Path('${MOCK_CHANGES_FILE}')).resolve()))")"
    CONFIG_PATH="${RESULTS}/config.generated.yml"
    log "Generating config for severity ${SEVERITY_PCT}% (${MOCK_CHANGES_FILE})..."
    uv run python -c "from pathlib import Path; import yaml; repo=Path('${REPO_ROOT}').resolve(); p_in=repo/'config.yml'; data=yaml.safe_load(p_in.read_text(encoding='utf-8')) or {}; repo_dir=Path(str(data.get('repository_dir',''))).expanduser(); data['repository_dir']=str((repo/repo_dir).resolve()) if (str(repo_dir) and not repo_dir.is_absolute()) else str(repo_dir); data['mock_changes_file']=str((repo/Path('${MOCK_CHANGES_FILE}')).resolve()); Path('${CONFIG_PATH}').write_text(yaml.safe_dump(data, sort_keys=False), encoding='utf-8')"
else
    CHANGES_FILE_ABS="$(uv run python -c "from pathlib import Path; import yaml; repo=Path('${REPO_ROOT}').resolve(); p=repo/'config.yml'; data=yaml.safe_load(p.read_text(encoding='utf-8')) or {}; v=str(data.get('mock_changes_file','') or '').strip(); mp=Path(v).expanduser() if v else None; print(str((repo/mp).resolve()) if (mp and not mp.is_absolute()) else (str(mp) if mp else ''))")"
fi

# Save experiment metadata for reference
cat > "${RESULTS}/experiment_metadata.json" << EOF
{
  "experiment_id": "${EXPERIMENT_ID}",
  "timestamp": "${TIMESTAMP}",
  "severity_pct": ${SEVERITY_PCT:-null}
}
EOF

# =============================================================================
# Step 1: Instrument the application
# =============================================================================
log "Step 1: Instrumenting application..."

CONFIG_ARGS=()
if [[ -n "${CONFIG_PATH}" ]]; then
    CONFIG_ARGS=(-c "${CONFIG_PATH}")
fi
uv run otel-agent run $FORCE "${CONFIG_ARGS[@]}"

BASE_DIR=".otel-agent/base"
HEAD_DIR=".otel-agent/head"

if [[ ! -d "$BASE_DIR" ]] || [[ ! -d "$HEAD_DIR" ]]; then
    error "Instrumentation failed - no base/head directories"
    exit 1
fi
success "Instrumented: $BASE_DIR, $HEAD_DIR"

# Persist instrumentation plan + changes into the results directory.
if [[ -f ".otel-agent/perf_marker_plan.json" ]]; then
    cp ".otel-agent/perf_marker_plan.json" "${RESULTS}/perf_marker_plan.json"
fi
if [[ -f ".otel-agent/skipped_base.json" ]]; then
    cp ".otel-agent/skipped_base.json" "${RESULTS}/skipped_base.json"
fi
if [[ -f ".otel-agent/skipped_instrumentation.json" ]]; then
    cp ".otel-agent/skipped_instrumentation.json" "${RESULTS}/skipped_instrumentation.json"
fi
if [[ -n "${CHANGES_FILE_ABS}" ]] && [[ -f "${CHANGES_FILE_ABS}" ]]; then
    cp "${CHANGES_FILE_ABS}" "${RESULTS}/mock_changes.json"
else
    warn "No mock changes file found (CHANGES_FILE_ABS='${CHANGES_FILE_ABS}'). Localization metrics will be skipped."
fi

# =============================================================================
# Step 2: Semantic localization metrics (per file)
# =============================================================================
log "Step 2: Computing per-file localization metrics..."
if [[ -f "${RESULTS}/perf_marker_plan.json" ]] && [[ -f "${RESULTS}/mock_changes.json" ]]; then
    uv run otel-agent localize \
      --head-dir "${HEAD_DIR}" \
      --plan "${RESULTS}/perf_marker_plan.json" \
      --changes "${RESULTS}/mock_changes.json" \
      --out "${RESULTS}/localization_report.json" || warn "Localization metrics failed (see output above)."
    if [[ -f "${RESULTS}/localization_report.json" ]]; then
        success "Localization report: ${RESULTS}/localization_report.json"
    fi
else
    warn "Skipping localization metrics (missing perf_marker_plan.json or mock_changes.json in results dir)."
fi

echo ""
success "Instrumentation + localization eval complete!"
success "Results: ${RESULTS}"
