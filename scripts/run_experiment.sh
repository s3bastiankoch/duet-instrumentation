#!/bin/bash
# =============================================================================
# Run Experiment: Instrument → Run SUT  → k6 → Analyze
# =============================================================================
# Supports parallel execution: each experiment gets its own infrastructure
# via Terraform workspaces and unique resource naming.
# =============================================================================
set -e

# Route all standard output to stderr by default.
exec 1>&2

# Defaults
VUS=5
DURATION="60s"
CALIBRATE="false"
TARGET_SATURATION="95"
OUTPUT_DIR="./results"
FORCE=""
SSH_KEY=""
SUT_BASE_PORT=3001
SUT_HEAD_PORT=3002
TERRAFORM_DIR=""
INFRA_PROVIDER="gcp"
SUT_HOST=""
LOADGEN_HOST=""
JWT_SECRET=""
EXPERIMENT_ID=""
DESTROY_ONLY="false"
SKIP_DESTROY="false"
SEVERITY_PCT=""
CHANGES_FILE_ABS=""

# k6 load mode (passed to scripts/k6-movie-server.js)
# - vus: constant-vus (default)
# - rps: constant-arrival-rate (constant request rate)
LOAD_MODE="vus"
TARGET_RPS=""
ARRIVAL_TIME_UNIT="1s"
PRE_ALLOCATED_VUS="20"
MAX_VUS="50"
PINNING_STRATEGY_MODE="auto"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
K6_SCRIPT="${SCRIPT_DIR}/k6-movie-server.js"
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

# Generate a short random ID for experiments
generate_experiment_id() {
    # Use /dev/urandom for a short alphanumeric ID (8 chars)
    LC_ALL=C tr -dc 'a-z0-9' < /dev/urandom | head -c 8
}

usage() {
    cat << EOF
Usage: $(basename "$0") --jwt-secret SECRET [OPTIONS]

Run a complete benchmark experiment: instrument → k6 load test → analyze

This script supports parallel execution - each experiment gets its own
dedicated SUT and LoadGen VMs via Terraform workspaces.

Required:
    --jwt-secret SECRET    JWT secret for authentication (required)

Options:
    --provider NAME        Infrastructure provider: gcp|stackit (default: gcp)
    --stackit              Shortcut for --provider stackit
    --experiment-id ID     Unique experiment ID (auto-generated if not provided)
                           Used for Terraform workspace and resource naming
    --severity PCT         Experiment severity percentage (selects mock changes file:
                           examples/mock_git/movie_perf_<pct>pct.json). Also recorded in metadata.
    --vus NUM              k6 virtual users (default: 5, ignored if --calibrate is used)
    --duration TIME        k6 duration (default: 60s)
    --load-mode MODE       k6 load mode: vus|rps (default: vus)
    --target-rps NUM       Target constant RPS (required if --load-mode rps)
    --k6-pre-allocated-vus NUM  Pre-allocated VUs for RPS executor (default: 20)
    --k6-max-vus NUM       Max VUs for RPS executor (default: 50)
    --arrival-time-unit TIME    Time unit for RPS rate (default: 1s)
    --pinning-strategy MODE     CPU pinning strategy: auto|shared|isolated|sibling (default: auto)
    --calibrate            Find ideal VU count for target saturation before running
    --target-saturation    Target CPU saturation % (default: 95)
    -o, --output DIR       Results directory (default: ./results)
    -f, --force            Force re-instrumentation
    --skip-destroy         Keep infrastructure after experiment (for debugging)
    --destroy ID           Destroy infrastructure for a specific experiment ID and exit
    -h, --help             Show this help

Parallel execution example:
    # Terminal 1
    $(basename "$0") --jwt-secret "secret" --experiment-id exp-a --duration 120s
    
    # Terminal 2 (run simultaneously)
    $(basename "$0") --jwt-secret "secret" --experiment-id exp-b --duration 120s

Cleanup example:
    $(basename "$0") --destroy exp-a

EOF
    exit 0
}

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
ssh_base_args() {
    local args=()
    args+=("-o" "StrictHostKeyChecking=no" "-o" "UserKnownHostsFile=/dev/null")
    args+=("-i" "$SSH_KEY")
    echo "${args[@]}"
}

ssh_run() {
    local host="$1"; shift
    local ssh_args
    ssh_args="$(ssh_base_args)"
    # shellcheck disable=SC2086
    ssh $ssh_args "ubuntu@${host}" "$@"
}

scp_to() {
    local src="$1"
    local host="$2"
    local dest="$3"
    local ssh_args
    ssh_args="$(ssh_base_args)"
    # shellcheck disable=SC2086
    scp $ssh_args "$src" "ubuntu@${host}:$dest"
}

scp_from() {
    local host="$1"
    local src="$2"
    local dest="$3"
    local ssh_args
    ssh_args="$(ssh_base_args)"
    # shellcheck disable=SC2086
    scp $ssh_args "ubuntu@${host}:$src" "$dest"
}

expand_path() {
    local p="$1"
    # Expand leading "~" (bash only expands it unquoted).
    if [[ "$p" == "~"* ]]; then
        # shellcheck disable=SC2086
        p=$(eval echo "$p")
    fi
    echo "$p"
}

wait_http_ready() {
    local host="$1"
    local name="$2"
    log "Waiting for ${name} cloud-init (/ready)..."
    for i in {1..90}; do
        code=$(curl -s -o /dev/null -w "%{http_code}" "http://${host}/ready" 2>/dev/null || echo "000")
        [[ "$code" == "200" ]] && { success "${name} ready"; return 0; }
        sleep 5
    done
    echo "Error: ${name} did not become ready in time (${host})" >&2
    return 1
}

terraform_apply_if_needed() {
    TERRAFORM_DIR="${REPO_ROOT}/infrastructure/${INFRA_PROVIDER}"
    local tfvars="${TERRAFORM_DIR}/terraform.tfvars"
    local tf_data_dir="${TERRAFORM_DIR}/.terraform-${EXPERIMENT_ID}"

    log "Starting infrastructure with Terraform (workspace: ${EXPERIMENT_ID})..."
    if [[ ! -f "$tfvars" ]]; then
        error "Missing ${tfvars}. This script expects Terraform configuration via terraform.tfvars."
        exit 1
    fi

    # Derive SSH private key path from terraform.tfvars (ssh_public_key_path).
    local pub
    pub="$(awk -F= '/^[[:space:]]*ssh_public_key_path[[:space:]]*=/{v=$2; gsub(/^[[:space:]]+|[[:space:]]+$/,"",v); gsub(/"/,"",v); print v; exit}' "$tfvars")"
    if [[ -z "$pub" ]]; then
        error "ssh_public_key_path not set in ${tfvars}."
        exit 1
    fi
    pub="$(expand_path "$pub")"
    SSH_KEY="${pub%.pub}"

    # Extract owner_name from terraform.tfvars (default to "user" if not set)
    local base_owner
    base_owner="$(awk -F= '/^[[:space:]]*owner_name[[:space:]]*=/{v=$2; gsub(/^[[:space:]]+|[[:space:]]+$/,"",v); gsub(/"/,"",v); print v; exit}' "$tfvars")"
    if [[ -z "$base_owner" ]]; then
        base_owner="user"
    fi
    
    # Combine base owner_name with experiment ID for unique resource naming
    RESOURCE_OWNER="${base_owner}-${EXPERIMENT_ID}"
    log "Resource owner name: ${RESOURCE_OWNER}"

    mkdir -p "$tf_data_dir"

    # Initialize Terraform
    TF_DATA_DIR="$tf_data_dir" terraform -chdir="$TERRAFORM_DIR" init -upgrade >/dev/null

    # Create or select workspace for this experiment (enables parallel runs)
    if TF_DATA_DIR="$tf_data_dir" terraform -chdir="$TERRAFORM_DIR" workspace list | grep -q "^[* ]*${EXPERIMENT_ID}\$"; then
        log "Using existing Terraform workspace: ${EXPERIMENT_ID}"
    else
        log "Creating new Terraform workspace: ${EXPERIMENT_ID}"
        TF_DATA_DIR="$tf_data_dir" terraform -chdir="$TERRAFORM_DIR" workspace new "$EXPERIMENT_ID"
    fi

    # Apply with combined owner_name for unique resource naming
    TF_DATA_DIR="$tf_data_dir" TF_WORKSPACE="$EXPERIMENT_ID" terraform -chdir="$TERRAFORM_DIR" apply -auto-approve \
        -var-file="terraform.tfvars" \
        -var "owner_name=${RESOURCE_OWNER}"

    SUT_HOST="$(TF_DATA_DIR="$tf_data_dir" TF_WORKSPACE="$EXPERIMENT_ID" terraform -chdir="$TERRAFORM_DIR" output -raw vm_public_ip)"
    LOADGEN_HOST="$(TF_DATA_DIR="$tf_data_dir" TF_WORKSPACE="$EXPERIMENT_ID" terraform -chdir="$TERRAFORM_DIR" output -raw loadgen_vm_public_ip)"
    
    success "Infrastructure ready: SUT=${SUT_HOST}, LOADGEN=${LOADGEN_HOST}"
}

terraform_destroy() {
    TERRAFORM_DIR="${REPO_ROOT}/infrastructure/${INFRA_PROVIDER}"
    local tfvars="${TERRAFORM_DIR}/terraform.tfvars"
    local tf_data_dir="${TERRAFORM_DIR}/.terraform-${EXPERIMENT_ID}"

    if [[ ! -f "$tfvars" ]]; then
        error "Missing ${tfvars}."
        exit 1
    fi

    # Extract owner_name from terraform.tfvars (default to "user" if not set)
    local base_owner
    base_owner="$(awk -F= '/^[[:space:]]*owner_name[[:space:]]*=/{v=$2; gsub(/^[[:space:]]+|[[:space:]]+$/,"",v); gsub(/"/,"",v); print v; exit}' "$tfvars")"
    if [[ -z "$base_owner" ]]; then
        base_owner="user"
    fi
    
    # Combine base owner_name with experiment ID (must match what was used in apply)
    RESOURCE_OWNER="${base_owner}-${EXPERIMENT_ID}"

    mkdir -p "$tf_data_dir"
    TF_DATA_DIR="$tf_data_dir" terraform -chdir="$TERRAFORM_DIR" init -upgrade >/dev/null

    # Check if workspace exists
    if ! TF_DATA_DIR="$tf_data_dir" terraform -chdir="$TERRAFORM_DIR" workspace list | grep -q "^[* ]*${EXPERIMENT_ID}\$"; then
        warn "Terraform workspace '${EXPERIMENT_ID}' does not exist. Nothing to destroy."
        return 0
    fi

    log "Destroying infrastructure for experiment ${EXPERIMENT_ID} (owner: ${RESOURCE_OWNER})..."
    TF_DATA_DIR="$tf_data_dir" TF_WORKSPACE="$EXPERIMENT_ID" terraform -chdir="$TERRAFORM_DIR" destroy -auto-approve \
        -var-file="terraform.tfvars" \
        -var "owner_name=${RESOURCE_OWNER}" || true

    # Switch back to default workspace and delete the experiment workspace
    log "Cleaning up Terraform workspace..."
    TF_DATA_DIR="$tf_data_dir" TF_WORKSPACE="default" terraform -chdir="$TERRAFORM_DIR" workspace delete "$EXPERIMENT_ID" || true

    success "Infrastructure destroyed for experiment ${EXPERIMENT_ID}"
}

# Parse args
while [[ $# -gt 0 ]]; do
    case $1 in
        --jwt-secret) JWT_SECRET="$2"; shift 2 ;;
        --provider) INFRA_PROVIDER="$(echo "$2" | tr '[:upper:]' '[:lower:]')"; shift 2 ;;
        --stackit) INFRA_PROVIDER="stackit"; shift ;;
        --experiment-id) EXPERIMENT_ID="$2"; shift 2 ;;
        --severity) SEVERITY_PCT="$2"; shift 2 ;;
        --vus) VUS="$2"; shift 2 ;;
        --duration) DURATION="$2"; shift 2 ;;
        --load-mode) LOAD_MODE="$2"; shift 2 ;;
        --target-rps) TARGET_RPS="$2"; shift 2 ;;
        --k6-pre-allocated-vus) PRE_ALLOCATED_VUS="$2"; shift 2 ;;
        --k6-max-vus) MAX_VUS="$2"; shift 2 ;;
        --arrival-time-unit) ARRIVAL_TIME_UNIT="$2"; shift 2 ;;
        --pinning-strategy) PINNING_STRATEGY_MODE="$(echo "$2" | tr '[:upper:]' '[:lower:]')"; shift 2 ;;
        --calibrate) CALIBRATE="true"; shift ;;
        --target-saturation) TARGET_SATURATION="$2"; shift 2 ;;
        -o|--output) OUTPUT_DIR="$2"; shift 2 ;;
        -f|--force) FORCE="-f"; shift ;;
        --skip-destroy) SKIP_DESTROY="true"; shift ;;
        --destroy) EXPERIMENT_ID="$2"; DESTROY_ONLY="true"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "Unknown option: $1"; usage ;;
    esac
done

# Validate infrastructure provider
if [[ "$INFRA_PROVIDER" != "gcp" ]] && [[ "$INFRA_PROVIDER" != "stackit" ]]; then
    error "--provider must be one of: gcp|stackit (got: $INFRA_PROVIDER)"
    exit 1
fi

# Generate experiment ID if not provided
if [[ -z "$EXPERIMENT_ID" ]]; then
    EXPERIMENT_ID="exp-$(generate_experiment_id)"
fi

# Validate experiment ID format (lowercase alphanumeric and hyphens only)
if ! [[ "$EXPERIMENT_ID" =~ ^[a-z0-9-]+$ ]]; then
    error "Experiment ID must contain only lowercase letters, numbers, and hyphens"
    exit 1
fi

# Compute remote workdir unique to this experiment
REMOTE_WORKDIR="/tmp/llm-exp-${EXPERIMENT_ID}"

# Compose project name for Docker (used for container isolation)
COMPOSE_PROJECT="exp-${EXPERIMENT_ID}"

log "Experiment ID: $EXPERIMENT_ID"
log "Remote workdir: $REMOTE_WORKDIR"

# Validate required parameters (unless destroy-only mode)
if [[ "$DESTROY_ONLY" != "true" ]] && [[ -z "$JWT_SECRET" ]]; then
    error "--jwt-secret is required"
    echo "" >&2
    usage
fi

# Validate load mode
LOAD_MODE="$(echo "$LOAD_MODE" | tr '[:upper:]' '[:lower:]')"
if [[ "$LOAD_MODE" != "vus" ]] && [[ "$LOAD_MODE" != "rps" ]]; then
    error "--load-mode must be one of: vus|rps (got: $LOAD_MODE)"
    exit 1
fi
if [[ "$LOAD_MODE" == "rps" ]] && [[ -z "$TARGET_RPS" ]]; then
    error "--target-rps is required when --load-mode rps"
    exit 1
fi
if [[ "$LOAD_MODE" == "rps" ]] && [[ "$CALIBRATE" == "true" ]]; then
    warn "Calibration is VU-based and not compatible with --load-mode rps; disabling --calibrate."
    CALIBRATE="false"
fi
if [[ "$PINNING_STRATEGY_MODE" != "auto" ]] && \
   [[ "$PINNING_STRATEGY_MODE" != "shared" ]] && \
   [[ "$PINNING_STRATEGY_MODE" != "isolated" ]] && \
   [[ "$PINNING_STRATEGY_MODE" != "sibling" ]]; then
    error "--pinning-strategy must be one of: auto|shared|isolated|sibling (got: $PINNING_STRATEGY_MODE)"
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
# This keeps experiments self-contained and avoids modifying repo config.yml.
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
    # NOTE: Config loader resolves relative paths against the config file directory.
    # Since we write this config into the results folder, make paths absolute.
    uv run python -c "from pathlib import Path; import yaml; repo=Path('${REPO_ROOT}').resolve(); p_in=repo/'config.yml'; data=yaml.safe_load(p_in.read_text(encoding='utf-8')) or {}; repo_dir=Path(str(data.get('repository_dir',''))).expanduser(); data['repository_dir']=str((repo/repo_dir).resolve()) if (str(repo_dir) and not repo_dir.is_absolute()) else str(repo_dir); data['mock_changes_file']=str((repo/Path('${MOCK_CHANGES_FILE}')).resolve()); Path('${CONFIG_PATH}').write_text(yaml.safe_dump(data, sort_keys=False), encoding='utf-8')"
else
    # Try to discover mock_changes_file from the default repo config (if present).
    # This keeps the localize metrics step working even when --severity isn't used.
    CHANGES_FILE_ABS="$(uv run python -c "from pathlib import Path; import yaml; repo=Path('${REPO_ROOT}').resolve(); p=repo/'config.yml'; data=yaml.safe_load(p.read_text(encoding='utf-8')) or {}; v=str(data.get('mock_changes_file','') or '').strip(); mp=Path(v).expanduser() if v else None; print(str((repo/mp).resolve()) if (mp and not mp.is_absolute()) else (str(mp) if mp else ''))")"
fi

# Save experiment metadata for reference
cat > "${RESULTS}/experiment_metadata.json" << EOF
{
  "experiment_id": "${EXPERIMENT_ID}",
  "timestamp": "${TIMESTAMP}",
  "severity_pct": ${SEVERITY_PCT:-null},
  "vus": ${VUS},
  "duration": "${DURATION}",
  "load_mode": "${LOAD_MODE}",
  "target_rps": ${TARGET_RPS:-null},
  "arrival_time_unit": "${ARRIVAL_TIME_UNIT}",
  "pre_allocated_vus": ${PRE_ALLOCATED_VUS:-null},
  "max_vus": ${MAX_VUS:-null},
  "calibrate": ${CALIBRATE},
  "target_saturation": ${TARGET_SATURATION},
  "compose_project": "${COMPOSE_PROJECT}",
  "remote_workdir": "${REMOTE_WORKDIR}"
}
EOF

# Trap for graceful cleanup on interrupt
cleanup_on_interrupt() {
    echo ""
    error "Interrupted! Cleaning up..."
    if [[ -n "$SUT_HOST" ]] && [[ -n "$SSH_KEY" ]]; then
        ssh_run "$SUT_HOST" "DOCKER_CONFIG='${REMOTE_WORKDIR}/.docker' docker compose -p '${COMPOSE_PROJECT}' -f '${REMOTE_WORKDIR}/docker-compose.experiment.yml' down 2>/dev/null || true" || true
    fi
    if [[ "$SKIP_DESTROY" != "true" ]] && [[ -n "$TERRAFORM_DIR" ]]; then
        warn "Destroying infrastructure due to interrupt..."
        terraform_destroy || true
    else
        warn "Infrastructure may still be running. To destroy: $0 --destroy ${EXPERIMENT_ID}"
    fi
    exit 130
}
trap cleanup_on_interrupt SIGINT SIGTERM

# =============================================================================
# Handle destroy-only mode
# =============================================================================
if [[ "$DESTROY_ONLY" == "true" ]]; then
    log "Destroy-only mode: cleaning up experiment ${EXPERIMENT_ID}"
    terraform_destroy
    success "Cleanup complete"
    exit 0
fi

# =============================================================================
# Step 1: Instrument the application
# =============================================================================
log "Step 1: Instrumenting application..."

CONFIG_ARGS=()
if [[ -n "${CONFIG_PATH}" ]]; then
    CONFIG_ARGS=(-c "${CONFIG_PATH}")
fi
uv run otel-agent run $FORCE "${CONFIG_ARGS[@]}"

# `otel-agent run` now also generates the experiment compose in the working directory.
cp ".otel-agent/docker-compose.experiment.yml" "$RESULTS/docker-compose.experiment.yml"

BASE_DIR=".otel-agent/base"
HEAD_DIR=".otel-agent/head"
EXPERIMENT_COMPOSE="$RESULTS/docker-compose.experiment.yml"

if [[ ! -d "$BASE_DIR" ]] || [[ ! -d "$HEAD_DIR" ]]; then
    echo "Error: Instrumentation failed - no base/head directories"
    exit 1
fi
success "Instrumented: $BASE_DIR, $HEAD_DIR"

# Persist instrumentation plan + changes into the results directory (for traceability/repro).
if [[ -f ".otel-agent/perf_marker_plan.json" ]]; then
    cp ".otel-agent/perf_marker_plan.json" "${RESULTS}/perf_marker_plan.json"
fi
if [[ -f ".otel-agent/skipped_base.json" ]]; then
    cp ".otel-agent/skipped_base.json" "${RESULTS}/skipped_base.json"
fi
if [[ -n "${CHANGES_FILE_ABS}" ]] && [[ -f "${CHANGES_FILE_ABS}" ]]; then
    cp "${CHANGES_FILE_ABS}" "${RESULTS}/mock_changes.json"
else
    warn "No mock changes file found (CHANGES_FILE_ABS='${CHANGES_FILE_ABS}'). Localization metrics will be skipped."
fi

# =============================================================================
# k6 Auth config (JWT)
# =============================================================================
# The movie example enforces JWT auth when IS_BENCHMARK=true. We use the
# JWT_SECRET provided via command line for both the SUT containers and k6.
K6_JWT_SECRET="${JWT_SECRET}"
log "Using JWT_SECRET for authentication"

# =============================================================================
# Step 2: Start SUT with docker compose
# =============================================================================
log "Step 2: Starting SUT containers..."

terraform_apply_if_needed
log "Remote mode (Terraform): SUT=${SUT_HOST}, LOADGEN=${LOADGEN_HOST}"

wait_http_ready "$SUT_HOST" "SUT"
wait_http_ready "$LOADGEN_HOST" "LOADGEN"

# Update metadata with actual host IPs, resource owner, and hardware metrics
log "Gathering SUT hardware metrics..."
SUT_GOVERNOR=$(ssh_run "$SUT_HOST" "cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null | sort | uniq -c | awk '{print \$2 \":\" \$1}' | xargs | tr ' ' ','" || echo "unknown")
SUT_STEAL_TIME=$(ssh_run "$SUT_HOST" "vmstat 1 2 | tail -1 | awk '{print \$16}'" || echo "unknown")
SUT_TOPOLOGY=$(ssh_run "$SUT_HOST" "lscpu -e=CPU,CORE,SOCKET,NODE | tail -n +2 | xargs | tr ' ' ','" || echo "unknown")

if [[ "$SUT_GOVERNOR" != *"performance"* ]]; then
    warn "SUT CPU governor is not set to 'performance' (current: $SUT_GOVERNOR). Results may be jittery."
fi
if [[ "$SUT_STEAL_TIME" != "0" ]] && [[ "$SUT_STEAL_TIME" != "unknown" ]]; then
    warn "SUT CPU steal time is non-zero ($SUT_STEAL_TIME%). Results may be affected by noisy neighbors."
fi

PINNING_STRATEGY=""
PINNING_BASE=""
PINNING_HEAD=""
PINNING_INFRA=""
PINNING_OS=""
PINNING_WARNINGS="[]"
if [[ -n "$SUT_TOPOLOGY" ]] && [[ "$SUT_TOPOLOGY" != "unknown" ]]; then
    log "Computing CPU pinning from SUT topology..."
    PINNING_VARS=$(SUT_TOPOLOGY="$SUT_TOPOLOGY" RESULTS="$RESULTS" EXPERIMENT_COMPOSE="$EXPERIMENT_COMPOSE" PINNING_STRATEGY_MODE="$PINNING_STRATEGY_MODE" uv run python - <<'PY'
from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

from src.cpu_topology import (
    PinningStrategy,
    compute_optimal_pinning,
    generate_config_snippet,
    parse_topology_metadata,
    recommend_pinning,
)

topology_str = os.environ.get("SUT_TOPOLOGY", "").strip()
strategy_mode = os.environ.get("PINNING_STRATEGY_MODE", "auto").strip().lower()
results_dir = Path(os.environ["RESULTS"])
compose_path = Path(os.environ["EXPERIMENT_COMPOSE"])

topology = parse_topology_metadata(topology_str)
if strategy_mode == "auto":
    config, _ = recommend_pinning(topology)
else:
    config = compute_optimal_pinning(topology, strategy=PinningStrategy(strategy_mode))

results_dir.mkdir(parents=True, exist_ok=True)
(results_dir / "cpu_pinning.json").write_text(
    json.dumps(config.to_dict(), indent=2),
    encoding="utf-8",
)
(results_dir / "cpu_pinning.yml").write_text(
    generate_config_snippet(config) + "\n",
    encoding="utf-8",
)

compose_data = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
services = compose_data.get("services", {})
for name, svc in services.items():
    if not isinstance(svc, dict):
        continue
    lname = name.lower()
    if config.base_cpuset and (lname.endswith("-base") or lname == "app-base"):
        svc["cpuset"] = config.base_cpuset
    elif config.head_cpuset and (lname.endswith("-head") or lname == "app-head"):
        svc["cpuset"] = config.head_cpuset
    elif config.infra_cpuset and "mongo" in lname:
        svc["cpuset"] = config.infra_cpuset

compose_path.write_text(
    yaml.safe_dump(compose_data, sort_keys=False),
    encoding="utf-8",
)

print(f'PINNING_STRATEGY="{config.strategy.value}"')
print(f'PINNING_BASE="{config.base_cpuset}"')
print(f'PINNING_HEAD="{config.head_cpuset}"')
print(f'PINNING_INFRA="{config.infra_cpuset}"')
print(f'PINNING_OS="{config.os_cpuset}"')
print("PINNING_WARNINGS=" + json.dumps(config.warnings))
PY
)
    eval "$PINNING_VARS"
else
    warn "SUT CPU topology unavailable; skipping CPU pinning optimization."
fi

cat > "${RESULTS}/experiment_metadata.json" << EOF
{
  "experiment_id": "${EXPERIMENT_ID}",
  "resource_owner": "${RESOURCE_OWNER}",
  "timestamp": "${TIMESTAMP}",
  "severity_pct": ${SEVERITY_PCT:-null},
  "vus": ${VUS},
  "duration": "${DURATION}",
  "load_mode": "${LOAD_MODE}",
  "target_rps": ${TARGET_RPS:-null},
  "arrival_time_unit": "${ARRIVAL_TIME_UNIT}",
  "pre_allocated_vus": ${PRE_ALLOCATED_VUS:-null},
  "max_vus": ${MAX_VUS:-null},
  "calibrate": ${CALIBRATE},
  "target_saturation": ${TARGET_SATURATION},
  "compose_project": "${COMPOSE_PROJECT}",
  "remote_workdir": "${REMOTE_WORKDIR}",
  "sut_host": "${SUT_HOST}",
  "loadgen_host": "${LOADGEN_HOST}",
  "sut_governor": "${SUT_GOVERNOR}",
  "sut_steal_time": "${SUT_STEAL_TIME}",
  "sut_topology": "${SUT_TOPOLOGY}",
  "cpu_pinning": {
    "strategy": "${PINNING_STRATEGY}",
    "os_cpuset": "${PINNING_OS}",
    "base_cpuset": "${PINNING_BASE}",
    "head_cpuset": "${PINNING_HEAD}",
    "infra_cpuset": "${PINNING_INFRA}",
    "warnings": ${PINNING_WARNINGS}
  }
}
EOF

log "Preparing artifacts..."
OTEL_TGZ="${RESULTS}/otel-agent.tgz"
#
# Create a "portable" tarball (avoid macOS xattrs / metadata).
# Otherwise GNU tar on Linux may warn:
#   Ignoring unknown extended header keyword 'LIBARCHIVE.xattr.com.apple.provenance'
#
TAR_CREATE_OPTS=()
if tar --help 2>/dev/null | grep -q -- '--no-xattrs'; then
    TAR_CREATE_OPTS+=("--no-xattrs")
fi
if tar --help 2>/dev/null | grep -q -- '--no-mac-metadata'; then
    TAR_CREATE_OPTS+=("--no-mac-metadata")
fi
if tar --help 2>/dev/null | grep -q -- '--disable-copyfile'; then
    TAR_CREATE_OPTS+=("--disable-copyfile")
fi

COPYFILE_DISABLE=1 tar "${TAR_CREATE_OPTS[@]}" -czf "$OTEL_TGZ" \
  --exclude="**/node_modules" \
  --exclude=".otel-agent/smolagents_cache" \
  --exclude="__pycache__" \
  --exclude="*.pyc" \
  -C "$REPO_ROOT" ".otel-agent"

log "Uploading .otel-agent + docker compose to SUT host..."
ssh_run "$SUT_HOST" "mkdir -p '${REMOTE_WORKDIR}'"
scp_to "$OTEL_TGZ" "$SUT_HOST" "${REMOTE_WORKDIR}/otel-agent.tgz"
scp_to "$EXPERIMENT_COMPOSE" "$SUT_HOST" "${REMOTE_WORKDIR}/docker-compose.experiment.yml"

log "Deploying on SUT host..."
ssh_run "$SUT_HOST" "set -euo pipefail
    # Docker CLI may try to create \$HOME/.docker; on some images /home/ubuntu
    # can be non-writable. Use a writable config dir under the remote workdir.
    export DOCKER_CONFIG='${REMOTE_WORKDIR}/.docker'
    mkdir -p \"\${DOCKER_CONFIG}\"
    rm -rf '${REMOTE_WORKDIR}/.otel-agent'
    tar -xzf '${REMOTE_WORKDIR}/otel-agent.tgz' -C '${REMOTE_WORKDIR}'
    # Make image builds verbose so we can see if bun is downloading/compiling,
    # and fail fast instead of silently hanging.
    export DOCKER_BUILDKIT=1
    export COMPOSE_DOCKER_CLI_BUILD=1
    export BUILDKIT_PROGRESS=plain

    # Use compose project name for container isolation (enables parallel experiments)
    BASE_DIR='${REMOTE_WORKDIR}/.otel-agent/base' HEAD_DIR='${REMOTE_WORKDIR}/.otel-agent/head' \
      timeout 25m docker compose -p '${COMPOSE_PROJECT}' -f '${REMOTE_WORKDIR}/docker-compose.experiment.yml' build --progress=plain

    BASE_DIR='${REMOTE_WORKDIR}/.otel-agent/base' HEAD_DIR='${REMOTE_WORKDIR}/.otel-agent/head' \
      JWT_SECRET='${JWT_SECRET}' \
      docker compose -p '${COMPOSE_PROJECT}' -f '${REMOTE_WORKDIR}/docker-compose.experiment.yml' up -d
"

log "Uploading k6 script to loadgen host..."
ssh_run "$LOADGEN_HOST" "mkdir -p '${REMOTE_WORKDIR}/k6' '${REMOTE_WORKDIR}/results'"
scp_to "$K6_SCRIPT" "$LOADGEN_HOST" "${REMOTE_WORKDIR}/k6/k6-movie-server.js"

log "Waiting for SUT services..."
if ! ssh_run "$SUT_HOST" "set -euo pipefail
    for i in {1..30}; do
      if curl -s --connect-timeout 1 --max-time 2 -o /dev/null \"http://localhost:${SUT_BASE_PORT}/health\" >/dev/null 2>&1; then
        BASE_OK=1
      else
        BASE_OK=0
      fi

      if curl -s --connect-timeout 1 --max-time 2 -o /dev/null \"http://localhost:${SUT_HEAD_PORT}/health\" >/dev/null 2>&1; then
        HEAD_OK=1
      else
        HEAD_OK=0
      fi

      [[ \"\$BASE_OK\" == '1' ]] && [[ \"\$HEAD_OK\" == '1' ]] && exit 0
      sleep 2
    done
    echo 'Error: services did not become healthy in time' >&2
    exit 1
"; then
    warn "SUT services failed health checks; downloading logs..."
    ssh_run "$SUT_HOST" "DOCKER_CONFIG='${REMOTE_WORKDIR}/.docker' docker compose -p '${COMPOSE_PROJECT}' -f '${REMOTE_WORKDIR}/docker-compose.experiment.yml' logs --no-color" > "${RESULTS}/sut.log" 2>/dev/null || true
    if [[ -f "${RESULTS}/sut.log" ]]; then
        echo ""
        error "SUT logs (from ${RESULTS}/sut.log):"
        cat "${RESULTS}/sut.log" >&2
    fi
    exit 1
fi

success "Services ready on SUT host (base:${SUT_BASE_PORT}, head:${SUT_HEAD_PORT})"

# Give services a short warm-up before any k6 run.
log "Waiting 20s before starting k6..."
sleep 20

# =============================================================================
# Step 2.5: Calibrate workload (optional)
# =============================================================================
if [[ "$CALIBRATE" == "true" ]]; then
    log "Step 2.5: Calibrating workload to reach ${TARGET_SATURATION}% saturation..."
    CALIBRATION_LOG="${RESULTS}/calibration.log"
    
    ssh_run "$LOADGEN_HOST" "set -euo pipefail
        k6 run \
          -e BASE_URL='http://${SUT_HOST}:${SUT_BASE_PORT}' \
          -e CALIBRATE=true \
          -e TARGET_SATURATION='${TARGET_SATURATION}' \
          -e JWT_SECRET='${K6_JWT_SECRET}' \
          -e LOG_SATURATION=false \
          '${REMOTE_WORKDIR}/k6/k6-movie-server.js'
    " | tee "$CALIBRATION_LOG"

    # Extract the last reported VU count from the calibration log
    # Example log: [CALIBRATION] Target saturation 95% reached/exceeded: 96.2% at 42 VUs
    CALIBRATED_VUS=$(grep "\[CALIBRATION\]" "$CALIBRATION_LOG" | tail -n 1 | sed -n 's/.* at \([0-9]*\) VUs/\1/p')
    
    if [[ -n "$CALIBRATED_VUS" ]]; then
        VUS="$CALIBRATED_VUS"
        success "Calibration complete: Using ${VUS} VUs for the benchmark"
    else
        log "Warning: Calibration did not report a VU count. Falling back to default: ${VUS} VUs"
    fi
fi

# =============================================================================
# Step 3: Run k6 load tests
# =============================================================================
log "Step 3: Running k6 benchmarks..."

BASE_URL="http://${SUT_HOST}:${SUT_BASE_PORT}"
HEAD_URL="http://${SUT_HOST}:${SUT_HEAD_PORT}"

log "Testing DUET (remote loadgen → base=${BASE_URL}, head=${HEAD_URL})..."
ssh_run "$LOADGEN_HOST" "set -euo pipefail
    k6 run --log-format raw \
      --console-output '${REMOTE_WORKDIR}/results/duet-k6.jsonl' \
      -e BASE_URL='${BASE_URL}' \
      -e HEAD_URL='${HEAD_URL}' \
      -e JWT_SECRET='${K6_JWT_SECRET}' \
      -e LOG_SATURATION=false \
      -e LOAD_MODE='${LOAD_MODE}' \
      -e TARGET_RPS='${TARGET_RPS}' \
      -e ARRIVAL_TIME_UNIT='${ARRIVAL_TIME_UNIT}' \
      -e PRE_ALLOCATED_VUS='${PRE_ALLOCATED_VUS}' \
      -e MAX_VUS='${MAX_VUS}' \
      '${REMOTE_WORKDIR}/k6/k6-movie-server.js'
 "

log "Downloading k6 results from loadgen host..."
scp_from "$LOADGEN_HOST" "${REMOTE_WORKDIR}/results/duet-k6.jsonl" "${RESULTS}/duet-k6.jsonl"

success "k6 benchmarks complete"

# =============================================================================
# Step 3.5: Evaluate k6 relative change distributions
# =============================================================================
log "Step 3.5: Evaluating k6 relative change distributions..."
uv run python -m src.k6_jsonl_eval \
  --input "${RESULTS}/duet-k6.jsonl" \
  --out "${RESULTS}/k6_eval_report.json" \
  --base-port "${SUT_BASE_PORT}" \
  --head-port "${SUT_HEAD_PORT}" \
  --plot \
  --plot-dir "${RESULTS}" \
  --plot-format png || warn "k6 eval failed (see output above)"
success "k6 eval report: ${RESULTS}/k6_eval_report.json"

# =============================================================================
# Step 4: Download SUT logs (runtime logs)
# =============================================================================
log "Step 4: Downloading SUT logs..."
ssh_run "$SUT_HOST" "DOCKER_CONFIG='${REMOTE_WORKDIR}/.docker' docker compose -p '${COMPOSE_PROJECT}' -f '${REMOTE_WORKDIR}/docker-compose.experiment.yml' logs --no-color" > "${RESULTS}/sut.log" 2>/dev/null || true

# =============================================================================
# Step 5: Download SQLite span databases from SUT containers
# =============================================================================
log "Step 5: Downloading span DBs from SUT containers..."

# Wait a moment to allow the periodic flush to finish.
sleep 6

ssh_run "$SUT_HOST" "mkdir -p '${REMOTE_WORKDIR}/results'"

log "Creating spans bundles inside containers (supports multi-process DBs)..."
# NOTE: SQLite may use WAL mode depending on configuration; span data may be in *.db-wal/*.db-shm.
# Bundle *.db* to include the base DB plus any WAL/shm sidecars.
ssh_run "$SUT_HOST" "DOCKER_CONFIG='${REMOTE_WORKDIR}/.docker' docker exec duet-base sh -lc 'ls -1 /usr/src/app/.otel-agent/spans/*.db >/dev/null 2>&1 || exit 1; cd /usr/src/app/.otel-agent/spans && tar -czf /tmp/base-spans.tgz ./*.db*'"
ssh_run "$SUT_HOST" "DOCKER_CONFIG='${REMOTE_WORKDIR}/.docker' docker exec duet-head sh -lc 'ls -1 /usr/src/app/.otel-agent/spans/*.db >/dev/null 2>&1 || exit 1; cd /usr/src/app/.otel-agent/spans && tar -czf /tmp/head-spans.tgz ./*.db*'"

log "Copying spans bundles to remote results dir..."
ssh_run "$SUT_HOST" "DOCKER_CONFIG='${REMOTE_WORKDIR}/.docker' docker cp \"duet-base:/tmp/base-spans.tgz\" \"${REMOTE_WORKDIR}/results/base-spans.tgz\""
ssh_run "$SUT_HOST" "DOCKER_CONFIG='${REMOTE_WORKDIR}/.docker' docker cp \"duet-head:/tmp/head-spans.tgz\" \"${REMOTE_WORKDIR}/results/head-spans.tgz\""

log "Downloading spans bundles..."
scp_from "$SUT_HOST" "${REMOTE_WORKDIR}/results/base-spans.tgz" "${RESULTS}/base-spans.tgz"
scp_from "$SUT_HOST" "${REMOTE_WORKDIR}/results/head-spans.tgz" "${RESULTS}/head-spans.tgz"

log "Extracting spans bundles..."
mkdir -p "${RESULTS}/base-spans" "${RESULTS}/head-spans"
tar -xzf "${RESULTS}/base-spans.tgz" -C "${RESULTS}/base-spans"
tar -xzf "${RESULTS}/head-spans.tgz" -C "${RESULTS}/head-spans"

success "Downloaded spans DBs to directories: ${RESULTS}/base-spans/, ${RESULTS}/head-spans/"

# =============================================================================
# Step 6: Evaluate via CLI
# =============================================================================
log "Step 6: Evaluating..."

uv run otel-agent eval \
  -o "${RESULTS}/base-spans" \
  -n "${RESULTS}/head-spans" \
  --out "${RESULTS}/eval_report.json" \
  --fast \
  --plot

echo ""
success "Eval report: ${RESULTS}/eval_report.json"

# =============================================================================
# Step 6.5: Semantic localization metrics (per file)
# =============================================================================
log "Step 6.5: Computing per-file localization metrics..."
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

# =============================================================================
# Cleanup
# =============================================================================
log "Cleaning up containers and remote files..."
ssh_run "$SUT_HOST" "DOCKER_CONFIG='${REMOTE_WORKDIR}/.docker' docker compose -p '${COMPOSE_PROJECT}' -f '${REMOTE_WORKDIR}/docker-compose.experiment.yml' down 2>/dev/null || true; rm -rf '${REMOTE_WORKDIR}' 2>/dev/null || true" || true
ssh_run "$LOADGEN_HOST" "rm -rf '${REMOTE_WORKDIR}' 2>/dev/null || true" || true

# Destroy infrastructure unless --skip-destroy was specified
if [[ "$SKIP_DESTROY" == "true" ]]; then
    warn "Skipping infrastructure destruction (--skip-destroy). Remember to clean up later with:"
    warn "  $0 --destroy ${EXPERIMENT_ID}"
else
    log "Destroying infrastructure..."
    terraform_destroy
fi

echo ""
success "Experiment complete!"
success "Results: $RESULTS"
success "Eval:    $RESULTS/eval_report.json"
success "k6 eval: $RESULTS/k6_eval_report.json"
if [[ "$SKIP_DESTROY" == "true" ]]; then
    warn "Infrastructure still running. To destroy: $0 --destroy ${EXPERIMENT_ID}"
fi
