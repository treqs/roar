# ROAR — Run Observation & Artifact Registration

**Version:** 0.2.1 (Alpha)
**License:** Apache 2.0

## Overview

ROAR is a provenance tracking system for ML/data pipelines that serves as a local front-end to TReqs' Graph Lineage-as-a-Service (GLaaS). It captures complete execution history — files read and written, git state, commands executed — enabling reproducibility, lineage queries, and full pipeline reconstruction from artifact hashes.

## Key Technologies

- **Python 3.10+** — Core implementation
- **Rust** — ptrace-based syscall tracer for file I/O interception (Linux only)
- **SQLite** — Local storage (`.roar/roar.db`)
- **BLAKE3** — Primary content-addressing hash algorithm
- **Pydantic v2, SQLAlchemy 2.0, Click** — Data modeling, ORM, CLI

## Project Structure

```
roar/
├── roar/                  # Main Python package
│   ├── cli/commands/      # CLI commands (run, build, auth, config, etc.)
│   ├── core/models/       # Pydantic data models (artifact, job, config, etc.)
│   ├── db/                # SQLite schema, ORM models, repositories
│   ├── services/          # Business logic
│   │   ├── execution/     # Tracer integration, run coordination, provenance
│   │   ├── registration/  # GLaaS registration
│   │   ├── reproduction/  # Artifact reproduction
│   │   └── vcs/           # Git operations
│   ├── filters/           # File filtering
│   ├── presenters/        # Output formatting
│   └── analyzers/         # Experiment tracker detection (W&B, MLflow, Neptune)
├── tracer/                # Rust ptrace syscall tracer
├── tests/                 # Unit, integration, e2e, happy_path tests
└── scripts/               # Utility scripts
```

## Main Features

| Command | Description |
|---------|-------------|
| `roar init` | Initialize provenance tracking in a project directory |
| `roar run <cmd>` | Execute a command with full provenance capture (file I/O, git state, packages) |
| `roar build <cmd>` | Like `run`, but marks steps as setup (executed first during reproduction) |
| `roar reproduce <hash>` | Reconstruct an artifact's creation: clone repo, install deps, re-run pipeline |
| `roar register` | Push local artifacts and jobs to a GLaaS server |
| `roar auth` | Manage SSH-based authentication with GLaaS |
| `roar config` | View/set 40+ configuration parameters |
| `roar show` / `roar lineage` | Query stored artifacts, jobs, and lineage relationships |
| `roar env` | Manage persistent environment variables injected into runs |
| `roar status` / `roar log` | View execution status and logs |
| `roar pop` / `roar reset` | Remove artifacts or clear the local database |

## Core Concepts

- **Artifacts** — Content-addressed files identified by hash (same content = same identity)
- **Jobs** — Execution records linking input artifacts to output artifacts, with full context (command, git info, duration, exit code)
- **Sessions** — Grouped job executions representing pipeline runs
- **Collections** — Named artifact groups
- **Provenance Context** — Captured environment: git commit/branch, hardware, Python packages, experiment tracker links, container info

## Architecture

- Dependency injection via `dependency-injector` (container pattern)
- Repository pattern for database abstraction
- Modular service layer with clean separation of concerns
- Gradual typing with mypy; linting with ruff

## Platform Support

| Feature | Linux x86_64 | Linux aarch64 | macOS | Windows |
|---------|:---:|:---:|:---:|:---:|
| `roar run` (tracing) | ✅ | ✅ | ❌ | ❌ |
| All other commands | ✅ | ✅ | ✅ | ✅ |

The ptrace-based tracer requires Linux. All non-tracing functionality works cross-platform.
