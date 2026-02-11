#!/bin/bash
# =============================================================================
# Run Simple Experiment: Prepare (no instrumentation) → Run SUT → k6
# =============================================================================
# This is a lightweight variant of scripts/run_experiment.sh:
# - Prepares base/head versions via `uv run otel-agent run` with `instrument: false`
# - Deploys the duet (base+head) compose to the SUT host
# - Runs k6 from the loadgen host and downloads `duet-k6.jsonl`
#
# No SQLite span DBs; includes k6-relative-change eval only.
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
PINNING_STRATEGY_MODE="auto"

# k6 load mode (passed to scripts/k6-movie-server.js)
LOAD_MODE="vus"
TARGET_RPS=""
ARRIVAL_TIME_UNIT="1s"
PRE_ALLOCATED_VUS="20"
MAX_VUS="50"

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

generate_experiment_id() {
  LC_ALL=C tr -dc 'a-z0-9' < /dev/urandom | head -c 8
}

usage() {
  cat << EOF
Usage: $(basename "$0") --jwt-secret SECRET [OPTIONS]

Run a duet benchmark (k6 only) WITHOUT any instrumentation.

What it does:
  - Prepares base/head via: instrument: false (no perf markers / no spans)
  - Deploys duet services to SUT host via Terraform
  - Runs k6 on loadgen host and downloads:
      - duet-k6.jsonl (console logs emitted by k6-movie-server.js)

Required:
  --jwt-secret SECRET    JWT secret used by the app + k6 auth token.
                         If you didn't change the movie server compose, this is typically "unused".

Options:
  --provider NAME        Infrastructure provider: gcp|stackit (default: gcp)
  --stackit              Shortcut for --provider stackit
  --experiment-id ID     Unique experiment ID (auto-generated if not provided)
                         Used for Terraform workspace and resource naming
  --severity PCT         Select mock changes file:
                         examples/mock_git/movie_perf_<pct>pct.json
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
  --skip-destroy         Keep infrastructure after experiment (for debugging)
  --destroy ID           Destroy infrastructure for a specific experiment ID and exit
  -h, --help             Show this help
EOF
  exit 0
}

# -----------------------------------------------------------------------------
# SSH helpers (derive SSH key from terraform.tfvars)
# -----------------------------------------------------------------------------
expand_path() {
  local p="$1"
  if [[ "$p" == "~"* ]]; then
    # shellcheck disable=SC2086
    p=$(eval echo "$p")
  fi
  echo "$p"
}

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
  RESOURCE_OWNER="${base_owner}-${EXPERIMENT_ID}"
  log "Resource owner name: ${RESOURCE_OWNER}"

  terraform -chdir="$TERRAFORM_DIR" init -upgrade >/dev/null

  if terraform -chdir="$TERRAFORM_DIR" workspace list | grep -q "^[* ]*${EXPERIMENT_ID}\$"; then
    log "Selecting existing Terraform workspace: ${EXPERIMENT_ID}"
    terraform -chdir="$TERRAFORM_DIR" workspace select "$EXPERIMENT_ID"
  else
    log "Creating new Terraform workspace: ${EXPERIMENT_ID}"
    terraform -chdir="$TERRAFORM_DIR" workspace new "$EXPERIMENT_ID"
  fi

  terraform -chdir="$TERRAFORM_DIR" apply -auto-approve \
    -var-file="terraform.tfvars" \
    -var "owner_name=${RESOURCE_OWNER}"

  SUT_HOST="$(terraform -chdir="$TERRAFORM_DIR" output -raw vm_public_ip)"
  LOADGEN_HOST="$(terraform -chdir="$TERRAFORM_DIR" output -raw loadgen_vm_public_ip)"

  success "Infrastructure ready: SUT=${SUT_HOST}, LOADGEN=${LOADGEN_HOST}"
}

terraform_destroy() {
  TERRAFORM_DIR="${REPO_ROOT}/infrastructure/${INFRA_PROVIDER}"
  local tfvars="${TERRAFORM_DIR}/terraform.tfvars"

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
  RESOURCE_OWNER="${base_owner}-${EXPERIMENT_ID}"

  terraform -chdir="$TERRAFORM_DIR" init -upgrade >/dev/null

  if ! terraform -chdir="$TERRAFORM_DIR" workspace list | grep -q "^[* ]*${EXPERIMENT_ID}\$"; then
    warn "Terraform workspace '${EXPERIMENT_ID}' does not exist. Nothing to destroy."
    return 0
  fi

  log "Selecting Terraform workspace: ${EXPERIMENT_ID}"
  terraform -chdir="$TERRAFORM_DIR" workspace select "$EXPERIMENT_ID"

  log "Destroying infrastructure for experiment ${EXPERIMENT_ID} (owner: ${RESOURCE_OWNER})..."
  terraform -chdir="$TERRAFORM_DIR" destroy -auto-approve \
    -var-file="terraform.tfvars" \
    -var "owner_name=${RESOURCE_OWNER}" || true

  log "Cleaning up Terraform workspace..."
  terraform -chdir="$TERRAFORM_DIR" workspace select default
  terraform -chdir="$TERRAFORM_DIR" workspace delete "$EXPERIMENT_ID" || true

  success "Infrastructure destroyed for experiment ${EXPERIMENT_ID}"
}

# -----------------------------------------------------------------------------
# Parse args
# -----------------------------------------------------------------------------
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

if [[ -z "$EXPERIMENT_ID" ]]; then
  EXPERIMENT_ID="exp-$(generate_experiment_id)"
fi
if ! [[ "$EXPERIMENT_ID" =~ ^[a-z0-9-]+$ ]]; then
  error "Experiment ID must contain only lowercase letters, numbers, and hyphens"
  exit 1
fi

if [[ "$DESTROY_ONLY" != "true" ]] && [[ -z "$JWT_SECRET" ]]; then
  error "--jwt-secret is required"
  echo "" >&2
  usage
fi

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
RESULTS="${RESULTS_BASE}/simple_experiment_${EXPERIMENT_ID}_${TIMESTAMP}"
mkdir -p "$RESULTS"

# Local working dir (self-contained; avoids touching .otel-agent/)
LOCAL_WORKDIR="${RESULTS}/workdir"

# Compute remote workdir unique to this experiment
REMOTE_WORKDIR="/tmp/llm-exp-${EXPERIMENT_ID}"

# Compose project name for Docker (used for container isolation)
COMPOSE_PROJECT="exp-${EXPERIMENT_ID}"

log "Experiment ID: $EXPERIMENT_ID"
log "Results: $RESULTS"
log "Local workdir: $LOCAL_WORKDIR"
log "Remote workdir: $REMOTE_WORKDIR"

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
# Step 1: Prepare base/head + compose (NO instrumentation)
# =============================================================================
log "Step 1: Preparing base/head (no instrumentation) + compose..."

CONFIG_PATH="${RESULTS}/config.generated.yml"

# Determine mock changes file (relative to repo root)
MOCK_CHANGES_FILE=""
if [[ -n "${SEVERITY_PCT}" ]]; then
  if ! [[ "${SEVERITY_PCT}" =~ ^[0-9]+$ ]]; then
    error "--severity must be an integer percentage (got: ${SEVERITY_PCT})"
    exit 1
  fi
  MOCK_CHANGES_FILE="examples/mock_git/movie_perf_${SEVERITY_PCT}pct.json"
fi

# Generate a config derived from repo config.yml, but:
# - enforce instrument: false
# - set a self-contained working_dir under results
# - optionally override mock_changes_file via --severity
# - keep duet ports consistent with this runner script
uv run python -c "from pathlib import Path; import yaml
repo=Path('${REPO_ROOT}').resolve()
cfg_in=repo/'config.yml'
data=yaml.safe_load(cfg_in.read_text(encoding='utf-8')) or {}

def abs_path(v):
    if not v:
        return v
    p=Path(str(v)).expanduser()
    if not p.is_absolute():
        p=(repo/p).resolve()
    return str(p)

# Resolve known paths to absolutes so this config is relocatable.
data['repository_dir']=abs_path(data.get('repository_dir'))
data['mock_changes_file']=abs_path(data.get('mock_changes_file'))
data['working_dir']=abs_path(data.get('working_dir'))

# Force no-instrument run.
data['instrument']=False
data['working_dir']=str(Path('${LOCAL_WORKDIR}').resolve())

# Optional severity override.
if '${MOCK_CHANGES_FILE}':
    data['mock_changes_file']=str((repo/Path('${MOCK_CHANGES_FILE}')).resolve())

# Keep ports aligned with the runner script.
data['duet_base_port']=int(${SUT_BASE_PORT})
data['duet_head_port']=int(${SUT_HEAD_PORT})

Path('${CONFIG_PATH}').write_text(yaml.safe_dump(data, sort_keys=False), encoding='utf-8')
print('Wrote ' + str(Path('${CONFIG_PATH}').resolve()))"

uv run otel-agent run -c "${CONFIG_PATH}"

EXPERIMENT_COMPOSE_LOCAL="${LOCAL_WORKDIR}/docker-compose.experiment.yml"
if [[ ! -f "${EXPERIMENT_COMPOSE_LOCAL}" ]]; then
  error "Expected compose file not found: ${EXPERIMENT_COMPOSE_LOCAL}"
  exit 1
fi

cp "${EXPERIMENT_COMPOSE_LOCAL}" "${RESULTS}/docker-compose.experiment.yml"
EXPERIMENT_COMPOSE="${RESULTS}/docker-compose.experiment.yml"

# Save minimal metadata for later evaluation (k6 only)
cat > "${RESULTS}/experiment_metadata.json" << EOF
{
  "kind": "simple_experiment_k6_only",
  "experiment_id": "${EXPERIMENT_ID}",
  "timestamp": "${TIMESTAMP}",
  "severity_pct": ${SEVERITY_PCT:-null},
  "mock_changes_file": "${MOCK_CHANGES_FILE}",
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

success "Prepared duet artifacts (no instrumentation)"

# =============================================================================
# Step 2: Start infra + deploy SUT
# =============================================================================
log "Step 2: Starting infrastructure + deploying SUT..."
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
  "kind": "simple_experiment_k6_only",
  "experiment_id": "${EXPERIMENT_ID}",
  "resource_owner": "${RESOURCE_OWNER}",
  "timestamp": "${TIMESTAMP}",
  "severity_pct": ${SEVERITY_PCT:-null},
  "mock_changes_file": "${MOCK_CHANGES_FILE}",
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

# Bundle prepared workdir for remote deployment
WORKDIR_TGZ="${RESULTS}/duet-workdir.tgz"
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

COPYFILE_DISABLE=1 tar "${TAR_CREATE_OPTS[@]}" -czf "${WORKDIR_TGZ}" \
  --exclude="**/node_modules" \
  --exclude="__pycache__" \
  --exclude="*.pyc" \
  --exclude=".otel-agent/smolagents_cache" \
  -C "${RESULTS}" "workdir"

log "Uploading duet workdir + compose to SUT host..."
ssh_run "$SUT_HOST" "mkdir -p '${REMOTE_WORKDIR}'"
scp_to "${WORKDIR_TGZ}" "$SUT_HOST" "${REMOTE_WORKDIR}/duet-workdir.tgz"
scp_to "${RESULTS}/docker-compose.experiment.yml" "$SUT_HOST" "${REMOTE_WORKDIR}/docker-compose.experiment.yml"

log "Deploying on SUT host..."
ssh_run "$SUT_HOST" "set -euo pipefail
  export DOCKER_CONFIG='${REMOTE_WORKDIR}/.docker'
  mkdir -p \"\${DOCKER_CONFIG}\"
  rm -rf '${REMOTE_WORKDIR}/workdir'
  tar -xzf '${REMOTE_WORKDIR}/duet-workdir.tgz' -C '${REMOTE_WORKDIR}'

  export DOCKER_BUILDKIT=1
  export COMPOSE_DOCKER_CLI_BUILD=1
  export BUILDKIT_PROGRESS=plain

  BASE_DIR='${REMOTE_WORKDIR}/workdir/base' HEAD_DIR='${REMOTE_WORKDIR}/workdir/head' \
    timeout 25m docker compose -p '${COMPOSE_PROJECT}' -f '${REMOTE_WORKDIR}/docker-compose.experiment.yml' build --progress=plain

  BASE_DIR='${REMOTE_WORKDIR}/workdir/base' HEAD_DIR='${REMOTE_WORKDIR}/workdir/head' \
    JWT_SECRET='${JWT_SECRET}' \
    docker compose -p '${COMPOSE_PROJECT}' -f '${REMOTE_WORKDIR}/docker-compose.experiment.yml' up -d
"

log "Uploading k6 script to loadgen host..."
ssh_run "$LOADGEN_HOST" "mkdir -p '${REMOTE_WORKDIR}/k6' '${REMOTE_WORKDIR}/results'"
scp_to "$K6_SCRIPT" "$LOADGEN_HOST" "${REMOTE_WORKDIR}/k6/k6-movie-server.js"

log "Waiting for SUT services..."
ssh_run "$SUT_HOST" "set -euo pipefail
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
"

success "Services ready on SUT host (base:${SUT_BASE_PORT}, head:${SUT_HEAD_PORT})"

# Give services a short warm-up before any k6 run.
log "Waiting 20s before starting k6..."
sleep 20

# =============================================================================
# Step 2.5: Calibrate workload (optional)
# =============================================================================
log "Using JWT_SECRET for authentication"
K6_JWT_SECRET="${JWT_SECRET}"
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

  CALIBRATED_VUS=$(grep "\[CALIBRATION\]" "$CALIBRATION_LOG" | tail -n 1 | sed -n 's/.* at \([0-9]*\) VUs/\1/p')
  if [[ -n "$CALIBRATED_VUS" ]]; then
    VUS="$CALIBRATED_VUS"
    success "Calibration complete: Using ${VUS} VUs for the benchmark"
  else
    warn "Calibration did not report a VU count. Falling back to default: ${VUS} VUs"
  fi
fi

# =============================================================================
# Step 3: Run k6 load test (DUET)
# =============================================================================
log "Step 3: Running k6 benchmark..."

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
success "k6 benchmark complete"

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
# Step 4: Download SUT logs (optional but helpful)
# =============================================================================
log "Step 4: Downloading SUT logs..."
ssh_run "$SUT_HOST" "DOCKER_CONFIG='${REMOTE_WORKDIR}/.docker' docker compose -p '${COMPOSE_PROJECT}' -f '${REMOTE_WORKDIR}/docker-compose.experiment.yml' logs --no-color" > "${RESULTS}/sut.log" 2>/dev/null || true

# =============================================================================
# Cleanup
# =============================================================================
log "Cleaning up containers and remote files..."
ssh_run "$SUT_HOST" "DOCKER_CONFIG='${REMOTE_WORKDIR}/.docker' docker compose -p '${COMPOSE_PROJECT}' -f '${REMOTE_WORKDIR}/docker-compose.experiment.yml' down 2>/dev/null || true; rm -rf '${REMOTE_WORKDIR}' 2>/dev/null || true" || true
ssh_run "$LOADGEN_HOST" "rm -rf '${REMOTE_WORKDIR}' 2>/dev/null || true" || true

if [[ "$SKIP_DESTROY" == "true" ]]; then
  warn "Skipping infrastructure destruction (--skip-destroy). Remember to clean up later with:"
  warn "  $0 --destroy ${EXPERIMENT_ID}"
else
  log "Destroying infrastructure..."
  terraform_destroy
fi

echo ""
success "Simple experiment complete!"
success "Results: ${RESULTS}"
success "k6:      ${RESULTS}/duet-k6.jsonl"
success "k6 eval: ${RESULTS}/k6_eval_report.json"
if [[ "$SKIP_DESTROY" == "true" ]]; then
  warn "Infrastructure still running. To destroy: $0 --destroy ${EXPERIMENT_ID}"
fi

