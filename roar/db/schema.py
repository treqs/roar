"""
Database schema definitions for roar.

This module contains the SQL schema and migration logic.
"""

SCHEMA = """
-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

-- =============================================================================
-- ARTIFACTS
-- Content-addressed files. Each artifact has a unique ID and can have multiple
-- hash digests (different algorithms). If any hash matches, it's the same artifact.
-- =============================================================================
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,                -- UUID identifier
    size INTEGER NOT NULL,
    first_seen_at REAL NOT NULL,        -- When first registered locally
    first_seen_path TEXT,               -- Original path when first seen
    source_type TEXT,                   -- 'https', NULL=local
    source_url TEXT,                    -- Original download URL
    uploaded_to TEXT,                   -- Where artifact was uploaded (JSON list)
    synced_at REAL,                     -- When synced to GLaaS (NULL = local only)
    metadata TEXT                       -- JSON: mime type, description, etc.
);

CREATE INDEX IF NOT EXISTS idx_artifacts_first_seen ON artifacts(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_artifacts_synced ON artifacts(synced_at);

-- =============================================================================
-- ARTIFACT HASHES
-- Hash digests for artifacts. Multiple algorithms supported per artifact.
-- Primary key is (algorithm, digest) to enable content-addressing lookups.
-- =============================================================================
CREATE TABLE IF NOT EXISTS artifact_hashes (
    artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    algorithm TEXT NOT NULL,            -- 'blake3', 'sha256', 'sha512', 'md5'
    digest TEXT NOT NULL,               -- Hex-encoded hash
    PRIMARY KEY (algorithm, digest)
);

CREATE INDEX IF NOT EXISTS idx_artifact_hashes_artifact ON artifact_hashes(artifact_id);
CREATE INDEX IF NOT EXISTS idx_artifact_hashes_digest ON artifact_hashes(digest);

-- =============================================================================
-- JOBS
-- Executions that consume inputs and produce outputs. Each run creates a new
-- job even if it reproduces identical artifacts.
-- =============================================================================
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_uid TEXT UNIQUE,                -- Random unique ID (like git short hash)
    timestamp REAL NOT NULL,            -- Start time
    command TEXT NOT NULL,              -- Full command string
    script TEXT,                        -- Primary script (e.g., "train.py")
    step_identity TEXT,                 -- Hash of (command, input_hashes) for dedup
    session_id INTEGER REFERENCES sessions(id),
    step_number INTEGER,                -- Position in pipeline (@1, @2, etc.)
    step_name TEXT,                     -- User-assigned name (optional)
    git_repo TEXT,                      -- Repo URL or path
    git_commit TEXT,
    git_branch TEXT,
    duration_seconds REAL,
    exit_code INTEGER,
    synced_at REAL,                     -- When synced to GLaaS
    status TEXT,                        -- NULL=completed, 'pending'=from server not yet run
    job_type TEXT,                      -- NULL='run', 'build'=build step (runs before DAG)
    metadata TEXT,                      -- JSON: env vars, hardware info, etc.
    telemetry TEXT                      -- JSON: external service links (wandb, etc.)
);

CREATE INDEX IF NOT EXISTS idx_jobs_timestamp ON jobs(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_script ON jobs(script);
CREATE INDEX IF NOT EXISTS idx_jobs_git_commit ON jobs(git_commit);
CREATE INDEX IF NOT EXISTS idx_jobs_synced ON jobs(synced_at);
CREATE INDEX IF NOT EXISTS idx_jobs_session ON jobs(session_id);
CREATE INDEX IF NOT EXISTS idx_jobs_step_identity ON jobs(step_identity);

-- Full-text search on commands
CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5(
    command,
    script,
    content=jobs,
    content_rowid=id
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS jobs_ai AFTER INSERT ON jobs BEGIN
    INSERT INTO jobs_fts(rowid, command, script) VALUES (new.id, new.command, new.script);
END;

CREATE TRIGGER IF NOT EXISTS jobs_ad AFTER DELETE ON jobs BEGIN
    INSERT INTO jobs_fts(jobs_fts, rowid, command, script) VALUES ('delete', old.id, old.command, old.script);
END;

CREATE TRIGGER IF NOT EXISTS jobs_au AFTER UPDATE ON jobs BEGIN
    INSERT INTO jobs_fts(jobs_fts, rowid, command, script) VALUES ('delete', old.id, old.command, old.script);
    INSERT INTO jobs_fts(rowid, command, script) VALUES (new.id, new.command, new.script);
END;

-- =============================================================================
-- JOB INPUTS & OUTPUTS
-- Lineage edges connecting jobs to artifacts.
-- =============================================================================
CREATE TABLE IF NOT EXISTS job_inputs (
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    path TEXT NOT NULL,                 -- Path at time of read
    PRIMARY KEY (job_id, artifact_id, path)
);

CREATE INDEX IF NOT EXISTS idx_job_inputs_artifact ON job_inputs(artifact_id);
CREATE INDEX IF NOT EXISTS idx_job_inputs_path ON job_inputs(path);

CREATE TABLE IF NOT EXISTS job_outputs (
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    path TEXT NOT NULL,                 -- Path at time of write
    PRIMARY KEY (job_id, artifact_id, path)
);

CREATE INDEX IF NOT EXISTS idx_job_outputs_artifact ON job_outputs(artifact_id);
CREATE INDEX IF NOT EXISTS idx_job_outputs_path ON job_outputs(path);

-- =============================================================================
-- COLLECTIONS
-- Named sets of artifacts and/or other collections (tree structure).
-- Used for: downloaded datasets, upload bundles, model checkpoints.
-- =============================================================================
CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,                 -- e.g., "s3://bucket/dataset/"
    collection_type TEXT,               -- "download", "upload", "checkpoint", etc.
    source_type TEXT,                   -- 'https', NULL=local
    source_url TEXT,                    -- Original download URL
    uploaded_to TEXT,                   -- Where collection was uploaded
    created_at REAL NOT NULL,
    synced_at REAL,
    metadata TEXT                       -- JSON: description, etc.
);

CREATE INDEX IF NOT EXISTS idx_collections_name ON collections(name);
CREATE INDEX IF NOT EXISTS idx_collections_type ON collections(collection_type);
CREATE INDEX IF NOT EXISTS idx_collections_source ON collections(source_url);

-- Collection members: either an artifact OR a child collection (not both)
CREATE TABLE IF NOT EXISTS collection_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    artifact_id TEXT REFERENCES artifacts(id),
    child_collection_id INTEGER REFERENCES collections(id) ON DELETE CASCADE,
    path_in_collection TEXT,            -- Relative path within collection
    CHECK ((artifact_id IS NULL) != (child_collection_id IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_collection_members_collection ON collection_members(collection_id);
CREATE INDEX IF NOT EXISTS idx_collection_members_artifact ON collection_members(artifact_id);
CREATE INDEX IF NOT EXISTS idx_collection_members_child ON collection_members(child_collection_id);

-- =============================================================================
-- SESSIONS
-- Ordered sequence of steps. Can be inferred from jobs or created for reproduction.
-- =============================================================================
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hash TEXT UNIQUE,                   -- Content hash for dedup/reference
    created_at REAL NOT NULL,
    source_artifact_hash TEXT,          -- Artifact this reproduces (if from reproduce)
    current_step INTEGER DEFAULT 1,     -- Current position for roar step
    is_active INTEGER DEFAULT 0,        -- Is this the active session?
    git_repo TEXT,
    git_commit_start TEXT,              -- First commit in session
    git_commit_end TEXT,                -- Last commit in session
    synced_at REAL,
    metadata TEXT                       -- YAML content for export/edit
);

CREATE INDEX IF NOT EXISTS idx_sessions_hash ON sessions(hash);
CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source_artifact_hash);
CREATE INDEX IF NOT EXISTS idx_sessions_active ON sessions(is_active);

-- =============================================================================
-- HASH CACHE
-- Local cache for path -> hash mapping with mtime/size for invalidation.
-- Stores multiple hash algorithms per file. Primary key is (path, algorithm).
-- =============================================================================
CREATE TABLE IF NOT EXISTS hash_cache (
    path TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    digest TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    cached_at REAL NOT NULL,
    PRIMARY KEY (path, algorithm)
);

CREATE INDEX IF NOT EXISTS idx_hash_cache_path ON hash_cache(path);
CREATE INDEX IF NOT EXISTS idx_hash_cache_updated ON hash_cache(cached_at);
"""


def run_migrations(conn) -> None:
    """
    Run any needed schema migrations.

    Args:
        conn: SQLite connection
    """
    # Check columns in jobs table
    cursor = conn.execute("PRAGMA table_info(jobs)")
    columns = {row["name"] for row in cursor.fetchall()}

    if "status" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN status TEXT")
    if "job_type" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN job_type TEXT")
