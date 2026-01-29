# roar

**Run Observation & Artifact Registration**

A local front-end to TReqs' Graph Lineage-as-a-Service (GLaaS). Roar tracks data artifacts and execution steps in ML pipelines, enabling reproducibility and lineage queries.

## Installation

```bash
pip install roar-cli
# or with uv
uv pip install roar-cli
```

Requires Python 3.10+ and Linux (x86_64 or aarch64) for full functionality.

### Platform Support

| Platform | `roar run` | Other commands |
|----------|------------|----------------|
| Linux x86_64 | Full support | Full support |
| Linux aarch64 | Full support | Full support |
| macOS | Not supported | Full support |
| Windows | Not supported | Full support |

The `roar run` command uses a native tracer binary that requires Linux. Other commands work on all platforms.

### Development Installation

```bash
# Clone the repository
git clone https://github.com/treqs/roar.git
cd roar

# Install in development mode (automatically builds tracer if Rust is installed)
uv pip install -e ".[dev]"
# or without uv
pip install -e ".[dev]"
```

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

## Commands

### `roar init`

Initialize roar in the current directory. Creates a `.roar/` directory to store the local database.

```bash
roar init
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
```

### `roar reproduce <hash>`

Reproduce an artifact by tracing its lineage.

```bash
# Show the reproduction plan
roar reproduce abc123de

# Run reproduction immediately
roar reproduce abc123de --run

# Run without prompts
roar reproduce abc123de --run -y
```

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

Manage GLaaS (Graph Lineage-as-a-Service) authentication.

```bash
roar auth register    # Register SSH key with GLaaS server
roar auth test        # Test authentication
roar auth status      # Show authentication status
```

### `roar config`

View or set configuration options.

```bash
roar config list
roar config get <key>
roar config set <key> <value>
```

Available configuration options:

| Key | Default | Description |
|-----|---------|-------------|
| `output.track_repo_files` | false | Include repo files in provenance |
| `output.quiet` | false | Suppress written files report |
| `filters.ignore_system_reads` | true | Ignore /sys, /etc reads |
| `filters.ignore_package_reads` | true | Ignore installed package reads |
| `filters.ignore_torch_cache` | true | Ignore torch/triton cache |
| `filters.ignore_tmp_files` | true | Ignore /tmp files |
| `glaas.url` | (none) | GLaaS server URL |

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

Roar can register artifacts and jobs with a GLaaS (Graph Lineage-as-a-Service) server using the `roar register` command.

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

# Register your SSH key
roar auth register

# Test authentication
roar auth test
```

## Development

### Prerequisites

- Python 3.10+
- Rust toolchain (for building the tracer) - install from https://rustup.rs/

### Setup

```bash
# Install dev dependencies (automatically builds tracer if Rust is installed)
uv pip install -e ".[dev]"
```

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
