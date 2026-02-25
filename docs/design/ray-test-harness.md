# Ray Test Harness — Design Plan

## Goal

Build a Docker-based test harness that runs a real multi-node Ray cluster,
submits jobs that perform various I/O patterns, and verifies outcomes. This
becomes the foundation for TDD of the roar–Ray integration.

The harness must:

1. Spin up a multi-node Ray cluster (head + N workers) in Docker
2. Include S3-compatible storage (MinIO) for object storage tests
3. Run sample Ray jobs that exercise file I/O, S3 I/O, and Ray Data
4. Be callable from pytest with fixtures for setup/teardown
5. Be fast enough for iterative development (< 60s per test cycle)

---

## Cluster Topology

```
docker compose up
  ├── ray-head       (Ray head node, GCS, driver)
  ├── ray-worker-1   (Ray worker node)
  ├── ray-worker-2   (Ray worker node)
  └── minio          (S3-compatible storage)
```

All containers share a Docker network (`roar-ray-test`).

---

## Docker Images

### Base Image: `roar-ray-base`

```dockerfile
FROM rayproject/ray:2.44.1-py312

# Install roar in editable mode (mounted volume in dev, COPY in CI)
# Install test dependencies
RUN pip install pytest boto3 pyarrow pandas

# Pre-create directories for roar state
RUN mkdir -p /tmp/roar-test/.roar
```

We use the official Ray image as base — it includes Ray, Python, and
standard ML packages. We layer roar on top.

**Key decision:** In development, mount the roar source as a volume so
changes are reflected immediately. In CI, COPY and install.

### MinIO

Use the official `minio/minio` image. Pre-create test buckets on startup.

---

## Docker Compose

```yaml
# tests/e2e/ray/docker-compose.yml

services:
  minio:
    image: minio/minio
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin
    ports:
      - "9000:9000"
    healthcheck:
      test: ["CMD", "mc", "ready", "local"]
      interval: 2s
      retries: 10

  minio-init:
    image: minio/mc
    depends_on:
      minio:
        condition: service_healthy
    entrypoint: >
      /bin/sh -c "
      mc alias set local http://minio:9000 minioadmin minioadmin &&
      mc mb local/test-bucket --ignore-existing &&
      mc mb local/output-bucket --ignore-existing
      "

  ray-head:
    build:
      context: .
      dockerfile: Dockerfile
    command: >
      bash -c "ray start --head --port=6379
      --dashboard-host=0.0.0.0
      --num-cpus=2
      --block"
    environment:
      AWS_ENDPOINT_URL: http://minio:9000
      AWS_ACCESS_KEY_ID: minioadmin
      AWS_SECRET_ACCESS_KEY: minioadmin
    ports:
      - "6379:6379"
      - "8265:8265"    # Ray dashboard
    healthcheck:
      test: ["CMD", "ray", "status"]
      interval: 3s
      retries: 15
    volumes:
      - shared-data:/shared
      - roar-src:/opt/roar

  ray-worker-1:
    build:
      context: .
      dockerfile: Dockerfile
    command: >
      bash -c "ray start --address=ray-head:6379
      --num-cpus=2
      --block"
    environment:
      AWS_ENDPOINT_URL: http://minio:9000
      AWS_ACCESS_KEY_ID: minioadmin
      AWS_SECRET_ACCESS_KEY: minioadmin
    depends_on:
      ray-head:
        condition: service_healthy
    volumes:
      - shared-data:/shared
      - roar-src:/opt/roar

  ray-worker-2:
    build:
      context: .
      dockerfile: Dockerfile
    command: >
      bash -c "ray start --address=ray-head:6379
      --num-cpus=2
      --block"
    environment:
      AWS_ENDPOINT_URL: http://minio:9000
      AWS_ACCESS_KEY_ID: minioadmin
      AWS_SECRET_ACCESS_KEY: minioadmin
    depends_on:
      ray-head:
        condition: service_healthy
    volumes:
      - shared-data:/shared
      - roar-src:/opt/roar

volumes:
  shared-data:
  roar-src:
```

---

## Sample Ray Jobs (Test Fixtures)

These are the workloads we submit to verify I/O capture. Each exercises
a different I/O pattern that roar needs to track.

### Job 1: Basic file I/O in remote tasks

```python
# tests/e2e/ray/jobs/basic_file_io.py
import ray
import json

@ray.remote
def write_file(path: str, data: str) -> str:
    with open(path, "w") as f:
        f.write(data)
    return path

@ray.remote
def read_file(path: str) -> str:
    with open(path, "r") as f:
        return f.read()

@ray.remote
def transform(input_path: str, output_path: str) -> dict:
    with open(input_path, "r") as f:
        data = json.load(f)
    result = {k: v * 2 for k, v in data.items()}
    with open(output_path, "w") as f:
        json.dump(result, f)
    return {"input": input_path, "output": output_path}

if __name__ == "__main__":
    ray.init()
    # Write → Read → Transform pipeline
    write_file.remote("/shared/input.json", '{"a": 1, "b": 2}')
    result = ray.get(transform.remote("/shared/input.json", "/shared/output.json"))
    print(json.dumps(result))
```

**Verifies:** Per-task file read/write attribution across workers.

### Job 2: S3 I/O via boto3

```python
# tests/e2e/ray/jobs/s3_io.py
import ray
import boto3
import json

@ray.remote
def upload_to_s3(bucket: str, key: str, data: str) -> str:
    s3 = boto3.client("s3")
    s3.put_object(Bucket=bucket, Key=key, Body=data.encode())
    return f"s3://{bucket}/{key}"

@ray.remote
def download_from_s3(bucket: str, key: str) -> str:
    s3 = boto3.client("s3")
    resp = s3.get_object(Bucket=bucket, Key=key)
    return resp["Body"].read().decode()

if __name__ == "__main__":
    ray.init()
    ray.get(upload_to_s3.remote("test-bucket", "input.json", '{"x": 42}'))
    result = ray.get(download_from_s3.remote("test-bucket", "input.json"))
    print(result)
```

**Verifies:** S3 proxy captures puts/gets from Ray workers.

### Job 3: Ray Data (Arrow-based)

```python
# tests/e2e/ray/jobs/ray_data_io.py
import ray

if __name__ == "__main__":
    ray.init()
    # Read parquet from S3, transform, write back
    ds = ray.data.read_parquet("s3://test-bucket/input-data/")
    ds = ds.map(lambda row: {"value": row["value"] * 2})
    ds.write_parquet("s3://output-bucket/results/")
    print(f"Wrote {ds.count()} rows")
```

**Verifies:** Native tracer captures Arrow/Parquet I/O at syscall level.

### Job 4: Multi-step pipeline with lineage

```python
# tests/e2e/ray/jobs/pipeline.py
import ray
import pandas as pd

@ray.remote
def extract(input_path: str) -> pd.DataFrame:
    return pd.read_csv(input_path)

@ray.remote
def transform(df: pd.DataFrame) -> pd.DataFrame:
    df["doubled"] = df["value"] * 2
    return df

@ray.remote
def load(df: pd.DataFrame, output_path: str) -> str:
    df.to_parquet(output_path)
    return output_path

if __name__ == "__main__":
    ray.init()
    df = ray.get(extract.remote("/shared/data.csv"))
    df = ray.get(transform.remote(df))
    result = ray.get(load.remote(df, "/shared/output.parquet"))
    print(result)
```

**Verifies:** Multi-step lineage with data flowing through Ray object store.

---

## Test Structure

```
tests/e2e/ray/
├── __init__.py
├── conftest.py              # Cluster fixtures (setup/teardown)
├── docker-compose.yml
├── Dockerfile
├── jobs/                    # Sample Ray workloads
│   ├── basic_file_io.py
│   ├── s3_io.py
│   ├── ray_data_io.py
│   └── pipeline.py
├── test_harness_smoke.py    # Cluster comes up, basic Ray job works
├── test_file_io.py          # File I/O capture from workers
├── test_s3_io.py            # S3 proxy capture from workers
├── test_ray_data.py         # Ray Data (Arrow) capture
├── test_task_attribution.py # Per-task I/O attribution
└── test_multi_node.py       # Verify I/O captured across nodes
```

### conftest.py — Cluster Management

```python
# tests/e2e/ray/conftest.py

import subprocess
import time
import pytest
import ray

COMPOSE_FILE = Path(__file__).parent / "docker-compose.yml"
STARTUP_TIMEOUT = 90  # seconds

@pytest.fixture(scope="session")
def ray_cluster():
    """Start the Docker Compose Ray cluster for the test session."""
    subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d", "--build"],
        check=True,
    )
    # Wait for Ray cluster to be ready
    _wait_for_ray_ready()
    yield {
        "head_address": "ray://localhost:6379",
        "dashboard": "http://localhost:8265",
        "minio_endpoint": "http://localhost:9000",
    }
    subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "down", "-v"],
        check=True,
    )

@pytest.fixture
def ray_connection(ray_cluster):
    """Connect to the test Ray cluster."""
    ray.init(address=ray_cluster["head_address"], ignore_reinit_error=True)
    yield
    ray.shutdown()

def _wait_for_ray_ready(timeout=STARTUP_TIMEOUT):
    """Poll Ray dashboard until the cluster is healthy."""
    ...
```

### Smoke Test

```python
# tests/e2e/ray/test_harness_smoke.py

import ray

def test_cluster_is_reachable(ray_connection):
    """Verify we can connect and run a trivial task."""
    @ray.remote
    def ping():
        return "pong"

    assert ray.get(ping.remote()) == "pong"

def test_cluster_has_multiple_nodes(ray_connection):
    """Verify the cluster has head + 2 workers."""
    nodes = ray.nodes()
    alive = [n for n in nodes if n["Alive"]]
    assert len(alive) >= 3  # head + 2 workers

def test_minio_is_accessible(ray_cluster):
    """Verify MinIO is reachable and test bucket exists."""
    import boto3
    s3 = boto3.client(
        "s3",
        endpoint_url=ray_cluster["minio_endpoint"],
        aws_access_key_id="minioadmin",
        aws_secret_access_key="minioadmin",
    )
    buckets = [b["Name"] for b in s3.list_buckets()["Buckets"]]
    assert "test-bucket" in buckets
```

---

## Build Order

### Step 1: Docker infrastructure (Day 1)

- [ ] Write `Dockerfile` (base Ray image + test deps)
- [ ] Write `docker-compose.yml` (head + 2 workers + MinIO)
- [ ] Write `conftest.py` with cluster fixture
- [ ] Write smoke test: cluster up, 3 nodes alive, MinIO accessible
- [ ] Verify: `pytest tests/e2e/ray/test_harness_smoke.py -v`

### Step 2: Sample jobs without roar (Day 1–2)

- [ ] Write `basic_file_io.py` — submit, verify output files exist
- [ ] Write `s3_io.py` — submit, verify objects in MinIO
- [ ] Write `pipeline.py` — submit, verify end-to-end data flow
- [ ] Write `ray_data_io.py` — submit, verify parquet output

These tests run *without* roar to establish baseline — proving the
cluster works and the I/O patterns are real.

### Step 3: Add roar to the cluster (Day 2–3)

- [ ] Install roar-cli in the Docker image
- [ ] Run `roar run python jobs/basic_file_io.py` on the head node
- [ ] Observe: what does the tracer capture? What does it miss?
- [ ] Write tests asserting current behavior (the "before" snapshot)

### Step 4: TDD the integration (Day 3+)

Write failing tests first, then implement:

- [ ] `test_file_io.py`: Assert roar captures file reads/writes from
      remote Ray workers (will fail initially)
- [ ] `test_s3_io.py`: Assert roar captures S3 ops from remote workers
- [ ] `test_task_attribution.py`: Assert each I/O event has a task ID
- [ ] `test_multi_node.py`: Assert I/O captured from workers on
      different Docker containers (not just the head)

Each failing test becomes the spec for the feature we build.

---

## CI Integration

```yaml
# .github/workflows/ray-e2e.yml (future)
ray-e2e:
  runs-on: ubuntu-latest
  services:
    docker:
      image: docker:dind
  steps:
    - uses: actions/checkout@v4
    - run: docker compose -f tests/e2e/ray/docker-compose.yml up -d --build
    - run: pip install -e ".[test]"
    - run: pytest tests/e2e/ray/ -v --timeout=120
    - run: docker compose -f tests/e2e/ray/docker-compose.yml down -v
```

Mark these tests with `@pytest.mark.ray_e2e` so they don't run in the
default unit test suite.

---

## Performance Budget

| Phase | Target |
|-------|--------|
| `docker compose up --build` (cold) | < 120s |
| `docker compose up` (warm, images cached) | < 30s |
| Individual test (submit job + verify) | < 15s |
| Full e2e suite | < 5min |

Use `scope="session"` on the cluster fixture to avoid rebuilding between tests.
