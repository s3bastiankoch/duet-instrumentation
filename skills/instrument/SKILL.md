---
name: instrument
description: Checks whether a code change made a service slower, span by span. Finds the performance-relevant changes between two git versions, puts identical span markers around them in both, runs both versions side by side on one machine under the same paired load (a duet benchmark), and reports per-span and end-to-end changes with bootstrap confidence intervals. Use when someone asks whether a PR, branch or commit range regressed performance, wants a performance check before merging, or mentions duet benchmarking or Duet Instrumentation.
argument-hint: "[base-ref] [head-ref]"
allowed-tools:
  - Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*)
  - Bash(git worktree *)
  - Bash(git diff *)
---

# Duet Instrumentation

Method from *Duet Instrumentation: An Agentic Approach to Improving Sensitivity
in Cloud Service Benchmarking* (Koch, Japke, Bermbach, IC2E 2026,
arXiv:2605.18397). End-to-end latency hides small regressions behind I/O and
cloud noise; a span around the changed code does not. Running both versions at
once on one machine makes the noise hit both alike.

Work in `.duet/` at the repository root and add it to `.git/info/exclude`.
Never commit instrumented code. Tell the user what you are doing at each step.

## 1. Pick the versions

`base` and `head` come from the arguments. Defaults: `head` is `HEAD`, `base` is
`git merge-base HEAD origin/main` (fall back to `main`, then `master`). If the
working tree is dirty, ask whether to include the uncommitted changes in head.

```bash
git worktree add --detach .duet/base <base>
git worktree add --detach .duet/head <head>
```

## 2. Locate the performance-relevant changes

Read `git diff <base> <head>` and the code around each hunk. A change is
**relevant** if it can change time or resources per request: loops and their
bounds, algorithms and data structures, allocations and copies, serialisation,
database queries, network or file I/O, caching, locking and concurrency,
regular expressions, logging inside hot paths, dependency upgrades in the call
path. It is **neutral** if it only touches comments, names, types, formatting,
tests, or code no request reaches.

Prefer high recall: an extra span costs little, a missed change costs the
answer. For each relevant change pick the smallest enclosing unit that exists
in **both** versions (a function, a method, a block around a loop or a query),
so the same span measures the old and the new code. If the code is new in
head, put the span around its caller in both versions.

Write `.duet/plan.json`:

```json
{"base": "<sha>", "head": "<sha>", "spans": [
  {"name": "search.rank", "file": "src/search.ts", "base_lines": [40, 71], "head_lines": [40, 75],
   "reason": "limit raised from 20 to 100; the ranking loop runs over all results"}
]}
```

Show the plan to the user before instrumenting. Ten spans or fewer is typical;
more than twenty means the units are too small.

## 3. Instrument both versions

1. Copy the marker for the service's language from
   [markers/](markers/README.md) into the same path in `.duet/base` and
   `.duet/head`. For other languages write the equivalent (monotonic clock,
   buffer, flush every second and on exit, no-op without `DUET_SPAN_FILE`).
2. Wrap every planned unit in both versions with the same span name. End the
   span on every exit path (`finally`, `defer`, `try`-with-resources). For
   async code, end it when the awaited work completes.
3. Make sure the buffer is flushed on shutdown. Python, TypeScript and Java
   do this themselves; in Go, call `duetspan.Close()` where `main` shuts down
   (after the server returns, before `os.Exit`), in both versions.
4. Change nothing else, and do not run formatters over whole files: they can
   realign lines you did not touch. `git -C .duet/base diff` and
   `git -C .duet/head diff` must show only marker lines, the shutdown flush and
   the helper import.
5. Build both versions and run their unit tests if they are quick. A version
   that does not build is a stop: fix the marker, never the code under test.

## 4. Run the duet

Both versions run at the same time on this machine, each on its own CPU cores
where possible, with identical configuration.

- **Docker Compose** (preferred when the repo has one): write
  `.duet/compose.yml` with one service built from `.duet/base`, one from
  `.duet/head`, ports 18081 and 18082, `DUET_SPAN_FILE=/duet/spans.jsonl` and a
  volume to `.duet/base-out` or `.duet/head-out`. Pin them with `cpuset`
  (for example `0-1` and `2-3`) when the machine has at least four cores.
  Give each version its own copy of stateful dependencies (database, cache)
  seeded the same way; share one only if it is read-only.
- **No containers**: start each version as a process with
  `DUET_SPAN_FILE=$PWD/.duet/base-spans.jsonl` (and `head-spans.jsonl`), on
  ports 18081 and 18082, pinned with `taskset` on Linux.

Then the load. Use the project's own load or integration requests if it has
them; otherwise write `.duet/requests.jsonl` with requests that reach every
planned span (one JSON object per line: `method`, `path`, optional `body`,
`headers`, `name`). Confirm with the user that the requests are representative.

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/duet_load.py \
  --base http://localhost:18081 --head http://localhost:18082 \
  --requests .duet/requests.jsonl --warmup 30 --duration 300 --out .duet/e2e.jsonl \
  --reset-spans <base span file> <head span file>
```

`--reset-spans` empties the span files when the warm-up ends, so the span
analysis sees the same measured window as the end-to-end one. Before the run,
send each request once with `curl` to check that it reaches its span.

Five minutes after a 30-second warm-up is the default; at least 1,000 pairs per
endpoint. Stop both versions with SIGTERM (`docker compose stop`) so the
markers flush, then collect the span files into `.duet/base-spans.jsonl` and
`.duet/head-spans.jsonl`.

## 5. Analyse and report

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/analyze.py \
  --base-spans .duet/base-spans.jsonl --head-spans .duet/head-spans.jsonl \
  --e2e .duet/e2e.jsonl --plan .duet/plan.json --json .duet/report.json
```

The test is the paper's: the median of paired relative changes
`head / base - 1` with a 95% bootstrap interval; the change counts when the
interval excludes 0. Report to the user:

1. One line first: which spans got slower or faster, by how much, and whether
   end-to-end latency moved.
2. The table from `analyze.py`, with the plan's reason next to each span.
3. Where a slower span sits in the diff (`file:line` in head).
4. Caveats that apply: span counts that differ between versions, too few
   samples, a single run on a shared or laptop machine. A change below about
   1% from one run is a lead, not a verdict; suggest an A/A run (base against
   itself) to see the noise floor, or a second run.

## 6. Clean up

```bash
docker compose -f .duet/compose.yml down -v   # if used
git worktree remove --force .duet/base
git worktree remove --force .duet/head
```

Keep `.duet/plan.json`, `.duet/report.json` and the raw files unless the user
asks to delete them.
