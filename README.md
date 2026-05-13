# roar

**Run Observation & Artifact Registration**

`roar` tracks data artifacts and execution steps in ML pipelines, enabling reproducibility and lineage queries. `roar` tracking happens automagically by observing your commands as they run, capturing essential context without requiring you to define a pipeline explicitly.

By identifying files based on their actual content rather than their names, it ensures you can always trace a result back to the exact inputs and code that produced it. This gives you reliable reproducibility and a clear history of your artifacts, all derived naturally from your workflow.

While `roar` captures your work locally, connecting it to a GLaaS (Global Lineage-as-a-Service) server like [glaas.ai](https://glaas.ai) allows you to publish your lineage graphs to a shared global registry for easy visualization and collaboration. Now your team can search for any artifact by its hash to see exactly how it was made and generate the precise commands needed to reproduce it on another machine.

## Installation

```bash
pip install roar-cli
# or with uv
uv pip install roar-cli
```

Requires Python 3.10+.

### Platform Support

| Platform      | Status                                                      |
| ------------- | ----------------------------------------------------------- |
| Linux x86_64  | ✅ Full support                                             |
| Linux aarch64 | ✅ Full support                                             |
| macOS         | 🚧 Experimental ([limitations](#macos-tracing-limitations)) |
| Windows       | Coming soon                                                 |

PyPI wheels are published for Linux (`x86_64`, `aarch64`) and macOS (`x86_64`, `arm64`).

If a matching wheel isn't available, `pip install` falls through to the
source distribution. The sdist ships the Rust tracer source but no
pre-built binaries, so it requires a C toolchain (`gcc` / `clang`), Rust
(`rustup`), and a few minutes to compile the tracers on first install.

### Development Installation

```bash
# Clone the repository
git clone https://github.com/treqs/roar.git
cd roar

# One-shot dev install: Python package + Rust tracer binaries
bash scripts/install-dev.sh
```

`scripts/install-dev.sh` runs `pip install -e ".[dev]"` (preferring `uv`
when available) and then builds the Rust tracer binaries
(`roar-tracer`, `roar-tracer-preload`, `roar-tracer-ebpf`, `roard`,
`roar-proxy`) and stages them into `roar/bin/`. A bare
`pip install -e .` does *not* build the tracer binaries because they
live in separate cargo crates outside the maturin manifest, so
`roar run` would fail with "No tracer binary found" until the script
runs. See [Building from source](#building-from-source) below for
details and the manual flow.

## Quick Start

```bash
# Initialize roar in your project
cd my-ml-project
roar init

# Run commands with provenance tracking
roar run python preprocess.py --input data.csv --output features.parquet
roar run python train.py --data features.parquet --output model.pt
roar run python evaluate.py --model model.pt --output metrics.json
```

## Product Telemetry

`roar` keeps anonymous product telemetry counters by default so maintainers can
prioritize reliability and platform support work. Telemetry is local-first:
small counters are stored under the XDG cache directory and uploaded
opportunistically in a background process. Telemetry never uploads file
contents, command arguments, file paths, environment variables, repository
names, hostnames, usernames, lineage payloads, or GLaaS auth tokens.

Uploaded payloads are limited to:

- A random `install_id`, event id, sequence number, and coarse timestamps.
- The installed `roar` version.
- Coarse platform values: OS family, CPU architecture, Python major/minor,
  shell name, installer class, and whether the process appears containerized.
- Allowlisted command counters such as `run`, `init`, `register`, and
  successful or failed `roar run` outcomes.
- Allowlisted tracer selection counters and coarse feature capability flags.

Inspect the current status and exact next payload preview:

```bash
roar telemetry --status
roar telemetry --print
```

When `telemetry.endpoint` is unset, roar derives the upload endpoint from the
configured GLaaS API URL. For example, `glaas.url = "https://api.dev.glaas.ai"`
uses `https://api.dev.glaas.ai/api/v1/telemetry/roar`.

Disable telemetry globally or for a single project:

```bash
roar telemetry --disable
roar config set telemetry.enabled false
```

Environment opt-outs always win over saved config:

```bash
DO_NOT_TRACK=1 roar run python train.py
ROAR_NO_TELEMETRY=1 roar run python train.py
```

Telemetry is also suppressed automatically in CI, pytest, and Roar-managed
backend worker environments such as Ray and OSMO jobs.

## Tracer Backends

`roar run` relies on a Rust "tracer" binary to observe file I/O. If you see an error like "No tracer binary found", build one of the backends below.

### Backends

| Backend | Binary                                           | Platforms    | Notes                                                                                                                                                                                                      |
| ------- | ------------------------------------------------ | ------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| eBPF    | `roar-tracer-ebpf`                               | Linux        | Fastest, but requires permissions and kernel support.                                                                                                                                                      |
| preload | `roar-tracer-preload` + `libroar_tracer_preload` | macOS, Linux | Uses `DYLD_INSERT_LIBRARIES` (macOS) or `LD_PRELOAD` (Linux). Not compatible with processes that ignore preload env vars (e.g., SIP/hardened runtime on macOS), or fully-static binaries (common with Go). |
| ptrace  | `roar-tracer`                                    | Linux        | Slowest, broadest compatibility on Linux.                                                                                                                                                                  |

### Building

```bash
cd rust

# eBPF (Linux)
cargo build --release -p roar-tracer-ebpf

# preload (macOS & Linux)
cargo build --release -p roar-tracer-preload

# ptrace (Linux)
cargo build --release -p roar-tracer
```

### Selecting A Backend

By default, `roar` uses `auto` mode: prefer eBPF, then preload, then ptrace.

```bash
# Show what roar can currently find and whether it looks usable
roar tracer status

# Set a default backend (auto|ebpf|preload|ptrace)
roar tracer set-default preload
```

### macOS Tracing Limitations

On macOS, `roar` uses the `preload` backend (`DYLD_INSERT_LIBRARIES`). macOS System Integrity Protection (SIP) silently blocks library injection for Apple-signed platform binaries — anything under `/usr/bin/`, `/bin/`, `/sbin/`, or `/System/`. When this happens, `roar run` will complete successfully but capture no file I/O events.

**Affected:** `/usr/bin/python3`, `/bin/sh`, `/usr/bin/ruby`, and all other Apple-shipped binaries.

**Workaround:** Use non-Apple builds of your tools:

```bash
# Homebrew
brew install python3
roar run python3 train.py          # Uses /opt/homebrew/bin/python3 — works

# conda / pyenv / nix also work
roar run ~/.pyenv/shims/python train.py

# This will NOT capture file events (SIP blocks it):
roar run /usr/bin/python3 train.py
```

`roar` prints a warning when it detects no events were captured from a SIP-protected binary.

## Commands

### `roar init`

Initialize roar in the current directory. Creates a `.roar/` directory to store the local database and a `config.toml` with default settings.

```bash
roar init           # Initialize, prompt for gitignore
roar init -y        # Initialize and auto-add to gitignore
roar init -n        # Initialize without modifying gitignore
```

### `roar run <command>`

Run a command with provenance tracking. Roar captures:

- Files read and written
- Git commit and branch
- Execution time and exit code
- Command arguments

```bash
roar run python train.py --epochs 10 --lr 0.001
roar run ./scripts/preprocess.sh
roar run torchrun --nproc_per_node=4 train.py

# Re-run a previous DAG step
roar run @2                    # Re-run DAG node 2
roar run @2 --epochs=10        # Re-run with parameter override
```

### `roar reproduce <hash>`

Reproduce an artifact by tracing its lineage.

```bash
# Show the reproduction plan (preview)
roar reproduce abc123de

# Run full reproduction
roar reproduce abc123de --run

# Run without prompts
roar reproduce abc123de --run -y

# Include system packages during setup
roar reproduce abc123de --run --package-sync

# Show all required packages (no truncation)
roar reproduce abc123de --list-requirements

# Reproduce a full lineage/session by its 64-character DAG hash
roar reproduce <lineage-hash> --lineage
roar reproduce <lineage-hash> --lineage --run
```

Unflagged `roar reproduce <hash>` continues to default to artifact reproduction. Full reproduction clones the git repository, creates a virtual environment, installs recorded packages, and runs the pipeline steps.

### `roar build <command>`

Run a build step with provenance tracking. Build steps run before pipeline steps during reproduction.

```bash
# Compile native extensions
roar build maturin develop --release
roar build make -j4

# Install local packages
roar build pip install -e .
```

Use for setup that should run before the main pipeline (compiling, installing).

### `roar auth`

Manage SSH-key-based GLaaS registration settings.

```bash
roar auth register    # Show SSH public key for registration
roar auth test        # Test connection to GLaaS server
roar auth status      # Show current auth status
```

To register SSH auth with GLaaS:

1. Run `roar auth register` to display your public key
2. Sign up at <https://glaas.ai> where you can paste your public key
3. Run `roar auth test` to verify

### `roar config`

View or set configuration options.

```bash
roar config list
roar config get <key>
roar config set <key> <value>
```

Run `roar config list` to see all available options with descriptions. Common options:

| Key                            | Default                | Description                             |
| ------------------------------ | ---------------------- | --------------------------------------- |
| `output.track_repo_files`      | false                  | Include repo files in provenance        |
| `output.quiet`                 | false                  | Suppress written files report           |
| `filters.ignore_system_reads`  | true                   | Ignore /sys, /etc, /sbin reads          |
| `filters.ignore_package_reads` | true                   | Ignore installed package reads          |
| `filters.ignore_torch_cache`   | true                   | Ignore torch/triton cache               |
| `filters.ignore_tmp_files`     | true                   | Ignore /tmp files                       |
| `glaas.url`                    | <https://api.glaas.ai> | GLaaS server URL                        |
| `glaas.web_url`                | <https://glaas.ai>     | GLaaS web UI URL                        |
| `registration.public_by_default` | false                | Default `register`/`put` visibility     |
| `registration.omit.enabled`    | true                   | Enable secret filtering                 |
| `hash.primary`                 | blake3                 | Primary hash algorithm                  |
| `logging.level`                | warning                | Log level (debug, info, warning, error) |

### `roar dag`

Display the pipeline DAG for the current session.

```bash
roar dag                  # Compact view with colors
roar dag --expanded       # Show all executions including reruns
roar dag --json           # Machine-readable JSON output
roar dag --show-artifacts # Show intermediate artifacts
```

### `roar env`

Manage persistent environment variables injected into `roar run` and `roar build`.

```bash
roar env set FOO bar      # Set FOO=bar
roar env get FOO          # Print value of FOO
roar env list             # List all env vars
roar env unset FOO        # Remove FOO
```

### `roar log`

Display recent job execution history.

```bash
roar log                  # Show recent job history
```

### `roar label`

Manage local labels for DAGs (sessions), jobs, and artifacts.

```bash
# Set labels (patches the current label document)
roar label set dag current owner=alice team=ml
roar label set job @2 phase=train lr=0.001
roar label set artifact ./outputs/model.pt model.name=resnet50 stage=baseline

# Remove labels
roar label unset artifact ./outputs/model.pt stage

# Copy labels from one entity to another
roar label cp job @2 artifact ./outputs/model.pt

# Show current labels
roar label show dag current
roar label show job @2
roar label show artifact ./outputs/model.pt

# Show label history (all versions)
roar label history dag current
roar label history artifact <artifact-hash>

# Sync local user-managed labels to GLaaS
roar label sync
roar label sync job @2
roar label sync artifact ./outputs/model.pt --dry-run
```

**Entity targets:**

- `dag`: `current` or a session hash prefix
- `job`: step ref (`@N` or `@BN`) or job UID
- `artifact`: file path or artifact hash

Labels are stored locally by default. You can explicitly reconcile current local user-managed labels to GLaaS with `roar label sync ...`, and labels are also included in lineage registration/publish flows when supported by the configured server.

### `roar register`

Register session, job, step, or artifact lineage with GLaaS.

```bash
roar register model.pt              # Register model lineage
roar register --dry-run model.pt    # Preview without registering
roar register -y model.pt           # Skip confirmation prompt
roar register @4                    # Register lineage for DAG step 4
roar register deadbeef              # Register lineage for a local job UID
roar register 7f1e...c9a4           # Register lineage for a tracked artifact hash
roar register 8d7a1f2c...           # Register a whole local session
roar register s3://bucket/run/out   # Register a tracked remote S3 artifact
```

**Supported targets:**

- Local artifact path: `model.pt`, `./outputs/metrics.json`
- Tracked artifact hash: primitive or composite
- Local job UID: full UID or unique prefix
- Step reference: `@N` or `@BN`
- Local session hash: full hash or unique prefix
- Tracked remote path: `s3://...`

For bare 8-character hex targets, `roar register` prefers a matching local job UID before falling back to session-hash-prefix resolution.

To make public publication the default for `roar register` and `roar put`:

```bash
roar config set registration.public_by_default true
```

Override per command with `--public` or `--private`. Use `--anonymous` on `roar register` or `roar put` to force public anonymous publication even when local GLaaS auth is configured. When public visibility comes from config rather than an explicit flag, `roar` prints a warning before publishing.

### `roar put`

Upload artifacts to cloud storage and register lineage with GLaaS.

```bash
roar put model.pt s3://bucket/models/ -m "Final model"
roar put ./checkpoints/ gs://bucket/run-42/ -m "All checkpoints"
roar put @2 s3://bucket/outputs/ -m "Step 2 outputs"
```

**Options:**

- `-m, --message` — Description of the upload (required)
- `--dry-run` — Preview without uploading
- `--no-tag` — Skip git tagging
- `--public` / `--private` — Override configured publish visibility
- `--anonymous` — Force public anonymous registration even when local GLaaS auth is configured

**Source formats:**

- File path: `model.pt`, `./data/output.csv`
- Directory: `./checkpoints/` (uploads all files recursively)
- Job reference: `@2` (uploads outputs from step 2)
- No source: uploads all outputs from the current session

### `roar get`

Download artifacts from cloud storage.

```bash
roar get s3://bucket/models/model.pt ./local/
roar get gs://bucket/data/train.csv
roar get https://example.com/weights.pt --hash abc123...
roar get s3://bucket/checkpoints/ ./local/ # Download all files under prefix
```

**Options:**

- `-m, --message` — Annotation for this download
- `--hash` — Expected BLAKE3 hash (for verification)
- `--tag` — Create a git tag for this download
- `--force` — Overwrite existing files
- `--dry-run` — Preview without downloading

Downloads are registered locally as source nodes in the DAG (outputs only, no inputs). They appear in GLaaS when downstream jobs are registered via `roar put` or `roar register`.

### `roar reset`

Start a fresh session. Previous session data is preserved in the database.

```bash
roar reset                # Reset with confirmation prompt
roar reset -y             # Reset without confirmation
```

### `roar show`

Show session, job, or artifact details.

```bash
roar show                          # Show active session overview
roar show @1                       # Show details for step 1
roar show @B1                      # Show details for build step 1
roar show a1b2c3d4                 # Show job by UID
roar show ./output/model.pkl       # Show artifact by path
```

### `roar status`

Show a summary of the active session, including the current DAG hash.

```bash
roar status
```

### `roar workflow`

Generate TReqs workflow YAML from a local session.

```bash
roar workflow generate
roar workflow generate .treqs/workflows/train.yaml
roar workflow generate --session 8d7a1f2c --name train
```

Generated workflows follow the TReqs workflow format: `name`, optional
`working_directory`, and one YAML key per task in session step order.
By default, `roar workflow generate` uses the active session and writes the
workflow under `.treqs/workflows/` at the repo root.

### `roar pop`

Remove the most recent job from the active session. Useful for undoing a mistaken `roar run` or correcting the pipeline before registration.

```bash
roar pop              # Pop with confirmation prompt
roar pop -y           # Pop without confirmation (skip prompt)
```

**What it does:**

- Removes the last job from the session history
- Deletes output artifacts created by that job (unless they're packages/system files)
- Does not affect the original input files

## Concepts

### Artifacts

Data files tracked by their content hash (BLAKE3). The same file content always has the same hash, regardless of filename or location.

### Jobs

Recorded executions that consume input artifacts and produce output artifacts. Each `roar run` creates a job record.

### Collections

Named groups of artifacts, used for downloaded datasets or upload bundles.

## Workflow Example

```bash
# Record your pipeline
roar run python preprocess.py
roar run python train.py --epochs 10
roar run python evaluate.py

# Later, reproduce an artifact
roar reproduce <model-hash> --run
```

## Git Integration

Roar automatically captures git metadata:

- Current commit hash
- Branch name
- Repository path

## Data Storage

All data is stored locally in `.roar/roar.db` (SQLite). The database includes:

- Artifact hashes and metadata
- Job records with inputs/outputs
- Hash cache for performance

Add `.roar/` to your `.gitignore` (roar offers to do this during `roar init`).

## GLaaS Server

Roar can register sessions, jobs, steps, and artifacts with a GLaaS (Global Lineage-as-a-Service) server using the `roar register` command.

### Server Setup

```bash
# Install with server dependencies
uv pip install -e ".[server]"
# or without uv
pip install -e ".[server]"

# Run the server
glaas-server

# Or with custom host/port
GLAAS_HOST=0.0.0.0 GLAAS_PORT=8080 glaas-server
```

The server provides:

- REST API for artifact and job registration
- Web UI at `/` with artifact and job browsers
- Search and filtering by command, GPU, file type, etc.

### Client Configuration

```bash
# Set the GLaaS server URL
roar config set glaas.url http://localhost:8000

# Show your SSH key (copy to GLaaS web UI)
roar auth register

# Test authentication
roar auth test
```

> [!TIP]
> Roar activity can be registered without authentication. Unauthenticated registrations are attributed to a public "anonymous" user, but are not guaranteed persistence. For persistent attribution, we recommend setting up `roar auth`.

## Development

### Prerequisites

- Python 3.10+
- Rust toolchain (for building the tracer) - install from <https://rustup.rs/>

### Setup

```bash
bash scripts/install-dev.sh
```

The script handles Python install + Rust tracer builds + staging
binaries into `roar/bin/`. See [Building from source](#building-from-source)
for what it does and how to run the steps manually.

### Building from source

`pip install -e .` runs `maturin develop` to build the `artifact-hash-py`
pyo3 extension, but the tracer binaries (`roar-tracer*`, `roard`,
`roar-proxy`) are separate cargo packages outside the maturin manifest.
The PyPI wheels bundle them under `roar/bin/`; an editable install
does not, and `roar run` fails until they're built and staged.

The fastest path is `scripts/install-dev.sh`, which does this:

```bash
# 1. Python package (editable, with dev extras)
uv pip install -e ".[dev]"   # or pip install -e ".[dev]"

# 2. Build the per-platform tracer crates
cd rust
# Linux:
cargo build --release \
  -p roar-tracer -p roar-tracer-preload -p roar-tracer-ebpf -p roar-proxy
# macOS:
cargo build --release -p roar-tracer-preload -p roar-proxy

# 3. Stage the built binaries where the editable install looks for them
cd ..
mkdir -p roar/bin
# Linux: install five binaries + the preload .so
install -m 0755 rust/target/release/{roar-tracer,roar-tracer-preload,roar-tracer-ebpf,roard,roar-proxy} roar/bin/
install -m 0755 rust/target/release/libroar_tracer_preload.so roar/bin/
# macOS: install the launcher + the preload .dylib + roar-proxy
# install -m 0755 rust/target/release/{roar-tracer-preload,roar-proxy} roar/bin/
# install -m 0755 rust/target/release/libroar_tracer_preload.dylib roar/bin/
```

The eBPF tracer (Linux only) needs `bpf-linker` and a Rust nightly
toolchain with `rust-src` for the BPF probe build:

```bash
cargo install bpf-linker
rustup install nightly
rustup component add rust-src --toolchain nightly
```

`scripts/install-dev.sh` skips eBPF gracefully when `bpf-linker` is
absent — the other tracers still work.

Verify the install with `roar tracer status`; every backend listed
should be `ready` (or have a clear platform-specific reason it isn't,
like `perf_event_paranoid=4 (needs <= 1)` for eBPF on a hardened
kernel).

### Running Quality Checks

```bash
# Linting
ruff check .

# Format check
ruff format --check

# Type checking
mypy roar

# Run all checks at once
ruff check . && ruff format --check && mypy roar
```

### Running Tests

```bash
# Run all tests (excluding those requiring a live GLaaS server)
pytest tests/ -v -m "not glaas and not live_glaas"

# Run with coverage
pytest tests/ -v --cov=roar --cov-report=term-missing -m "not glaas and not live_glaas"

# Run tests in parallel
pytest tests/ -v -n auto -m "not glaas and not live_glaas"

# Run only unit tests (fast)
pytest tests/ -v -m "not integration and not e2e and not glaas and not live_glaas"
```

## License

Apache 2.0
