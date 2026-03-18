# Profiling Roar

`roar` already has targeted benchmarks under `tests/benchmarks/`. The profiling harness in
[`scripts/profile_roar.py`](/home/trevor/dev/roar-cli-polish/scripts/profile_roar.py) adds a
repeatable way to capture wall-time summaries and Python hot spots for representative local
workflows.

## What It Profiles

- top-level CLI startup: `roar --help`
- a simple local `roar run`
- active-session query commands: `status` and `show --session`
- local publish flows without remote side effects: `register --dry-run` and `put --dry-run`
- Python startup overhead for `ROAR_WRAP=1`, with and without `ROAR_LOG_FILE`

Each CLI scenario records:

- repeated wall-time samples
- one `cProfile` run
- captured stdout/stderr
- top cumulative and internal Python hot spots

The startup scenario records:

- baseline vs wrapped wall time
- import-time breakdown from `python -X importtime -c pass`

## Run It

From the repo root:

```bash
uv run --extra dev python scripts/profile_roar.py
```

Useful options:

```bash
uv run --extra dev python scripts/profile_roar.py --iterations 5 --top 20
uv run --extra dev python scripts/profile_roar.py --scenario cli_run_simple --scenario startup_wrap
```

## Output Files

The harness writes:

- JSON summary: `tests/benchmarks/results/profile_suite_latest.json`
- Markdown summary: `tests/benchmarks/results/profile_suite_latest.md`
- raw profile artifacts: `tests/benchmarks/results/profiles/<timestamp>/`
- a copied latest artifact set: `tests/benchmarks/results/profiles/latest/`

The raw artifact directory contains:

- `*.prof` `cProfile` files
- `*.stdout.txt` and `*.stderr.txt` for profiled CLI runs
- `startup_wrap.importtime.txt` for import-time output

## How To Read It

- Start with the wall-time means to find the slowest end-user workflows.
- For a slow CLI scenario, inspect `top_cumulative` first; it shows what dominates total time.
- Inspect `top_internal` when cumulative time is dominated by wrappers and you need the leaf work.
- For `startup_wrap`, compare:
  - import overhead: `ROAR_WRAP=1` minus baseline
  - atexit overhead: `ROAR_WRAP=1 + LOG_FILE` minus `ROAR_WRAP=1`

## Current Focus

The existing performance guardrail in
[`tests/execution/runtime/test_sitecustomize_perf.py`](/home/trevor/dev/roar-cli-polish/tests/execution/runtime/test_sitecustomize_perf.py)
is close to the local threshold. The profiling harness is intended to make that startup/runtime
path measurable enough to optimize, not just to rerun the guardrail test.
