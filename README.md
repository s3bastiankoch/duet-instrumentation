# Duet instrumentation

This folder contains the **reference implementation** for Duet Instrumentation.

At a high level, `otel-agent`:

- **prepares two app versions** (“base” and “head”) from either a git diff range or a JSON changeset
- uses an LLM to **identify performance-relevant change locations**
- **instruments** both versions with OpenTelemetry span markers
- generates a **duet docker compose** to run base/head side-by-side under matched conditions
- provides evaluation tooling to compare **span-level** and **end-to-end** performance samples

## Repository map

- **`src/`**: Python package implementing the `otel-agent` CLI
- **`examples/movie/`**: TypeScript “movie server” SUT with configurable performance regressions
- **`scripts/`**: end-to-end experiment runners (cloud) + offline eval utilities
- **`infrastructure/`**: Terraform for provisioning benchmark VMs (GCP + STACKIT)
- **`signoz/`**: optional local SigNoz stack for tracing the CLI itself

## Quickstart (local)

### Prerequisites

- **Python**: \( \ge 3.12 \) (see `pyproject.toml`)
- **uv**: Python package manager (`https://github.com/astral-sh/uv`)
- **Docker**: with `docker compose`
- **LLM credentials**: set at least one provider key (e.g., `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`)
- **Optional (recommended)**: `bun` for TypeScript syntax checks during instrumentation

### Install

```bash
uv sync
```

### Sanity check

```bash
uv run otel-agent doctor
```

### Prepare + instrument base/head (default: JSON mode using `config.yml`)

`otel-agent run` is **config-driven**. By default, this repo ships a `config.yml` that points at `examples/movie` and a mock changeset.

```bash
uv run otel-agent run -c config.yml
```

This writes a working directory (by default `.otel-agent/`) containing:

- `base/` and `head/` (prepared app trees)
- `perf_marker_plan.json` (planned span anchors)
- `docker-compose.experiment.yml` (merged duet compose; base/head + infra)

### Run the duet locally (Docker)

```bash
BASE_DIR="$(pwd)/.otel-agent/base" \
HEAD_DIR="$(pwd)/.otel-agent/head" \
JWT_SECRET="dev-secret" \
docker compose -f .otel-agent/docker-compose.experiment.yml up -d --build
```

The duet exposes (by default):

- base: `http://localhost:3001`
- head: `http://localhost:3002`

To stop:

```bash
docker compose -f .otel-agent/docker-compose.experiment.yml down
```

## End-to-end experiments (cloud: provision SUT + loadgen VMs)

The evaluation uses a two-VM setup:

- **SUT VM**: runs the duet docker compose (base/head + MongoDB)
- **Loadgen VM**: runs `k6` and streams results back

The scripts in `scripts/` automate the full pipeline:

- `otel-agent run` (prepare + optional instrumentation)
- provision infra via Terraform (GCP or STACKIT)
- deploy artifacts to the SUT VM and run the duet
- run `k6` from the loadgen VM
- download logs + span databases
- run `otel-agent eval` and write plots/reports

### Prerequisites

- **Terraform** installed locally
- Provider credentials configured:
  - **GCP**: see `infrastructure/gcp/README.md`
  - **STACKIT**: not used for experiments and not suffienctly documented
- A provider-specific `terraform.tfvars` file present (the scripts expect it):
  - `infrastructure/gcp/terraform.tfvars`
  - or `infrastructure/stackit/terraform.tfvars`

### Full experiment (instrument → k6 → span eval → localization metrics)

```bash
scripts/run_experiment.sh \
  --jwt-secret "unused" \
  --severity 5 \
  --pinning-strategy isolated
```

Notes:

- `--severity PCT` selects `examples/mock_git/movie_perf_<pct>pct.json`.
- `--pinning-strategy isolated` uses CPU topology on the SUT VM to pin base/head/infra to dedicated cores.
- For constant request rate, use `--load-mode rps --target-rps <N>` (see `scripts/run_experiment.sh --help`).

### Lightweight experiment (k6 only; no span instrumentation)

```bash
scripts/run_simple_experiment.sh \
  --jwt-secret "unused" \
  --severity 5 \
  --pinning-strategy isolated
```

### Cleanup (if an experiment was interrupted)

```bash
scripts/run_experiment.sh --destroy <experiment-id>
```

## Outputs and how to re-run evaluation

Each run writes a self-contained folder under `results/`, for example:

```text
results/5/experiment_exp-abc12345_20260208_170554/
  experiment_metadata.json
  docker-compose.experiment.yml
  duet-k6.jsonl
  k6_eval_report.json
  base-spans/   # extracted SQLite *.db* bundles
  head-spans/
  eval_report.json
  localization_report.json
  *.png         # plots
```

Re-run eval for an existing experiment (picks the most recent matching folder):

```bash
scripts/run_eval.sh <experiment-id>
scripts/run_k6_eval.sh <experiment-id>
```

Re-evaluate a whole folder of experiments:

```bash
scripts/run_eval_folder.sh ./results/5
```

## Configuration (`config.yml`)

`otel-agent run` reads a YAML config (`otel-agent.yml` or `config.yml`). Paths are resolved **relative to the config file directory**.

Key fields (see `src/config.py`):

- **`repository_dir`**: local app folder (JSON mode)
- **`mock_changes_file`**: JSON changeset describing diffs to apply (JSON mode)
- **`repository_url`**, **`base_ref`**, **`head_ref`**: git mode inputs (alternative to JSON mode)
- **`entrypoint`**: entrypoint file relative to app root (used for instrumentation context)
- **`instrument`**: enable/disable span marker insertion
- **`model`**: LLM model identifier used by the analysis pipeline
- **`analysis_chunk_size`**, **`analysis_max_workers`**: parallelization controls for analysis
- **`duet_*`**: docker resource limits, ports, and optional CPU pinning

## CLI reference

- **`otel-agent run`**: prepare base/head, optionally plan + insert perf markers, and generate duet compose
- **`otel-agent eval`**: compare performance samples (span DBs or k6 outputs) and optionally plot results
- **`otel-agent localize`**: compute semantic localization metrics of planned spans vs changed lines/perfect spans
- **`otel-agent doctor`**: environment + dependency checks

Run `--help` on any command for details:

```bash
uv run otel-agent --help
uv run otel-agent run --help
uv run otel-agent eval --help
```

```

