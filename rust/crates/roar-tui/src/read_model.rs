use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use chrono::{Local, TimeZone};
use rusqlite::{params, Connection, OpenFlags, OptionalExtension, Row};
use serde_json::Value;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LabelLine {
    pub key: String,
    pub value: String,
}

#[derive(Debug, Clone)]
pub struct SessionRow {
    pub id: i64,
    pub hash: Option<String>,
    pub created_at: f64,
    pub display_datetime: String,
    pub display: String,
    pub command: Option<String>,
    pub git_repo: Option<String>,
    pub git_commit_start: Option<String>,
    pub git_commit_end: Option<String>,
    pub job_count: i64,
    pub artifact_count: i64,
    pub labels: Vec<LabelLine>,
}

#[derive(Debug, Clone)]
pub struct JobRow {
    pub id: i64,
    pub session_id: i64,
    pub job_uid: Option<String>,
    pub step_number: Option<i64>,
    pub job_type: Option<String>,
    pub step_ref: String,
    pub display: String,
    pub command: String,
    pub cwd: Option<String>,
    pub timestamp: f64,
    pub duration_seconds: Option<f64>,
    pub exit_code: Option<i64>,
    pub status: Option<String>,
    pub git_commit: Option<String>,
    pub git_branch: Option<String>,
    pub input_count: i64,
    pub output_count: i64,
    pub labels: Vec<LabelLine>,
}

#[derive(Debug, Clone)]
pub struct ArtifactRow {
    pub id: String,
    pub job_id: i64,
    pub role: String,
    pub path: String,
    pub display: String,
    pub size: i64,
    pub kind: String,
    pub hash_algorithm: Option<String>,
    pub hash_digest: Option<String>,
    pub producer: Option<String>,
    pub consumers: Vec<String>,
    pub labels: Vec<LabelLine>,
}

#[derive(Debug, Clone)]
pub struct Preview {
    pub title: String,
    pub summary_lines: Vec<String>,
    pub label_lines: Vec<String>,
    pub context_lines: Vec<String>,
}

impl Preview {
    pub fn lines(&self) -> Vec<String> {
        let mut lines = Vec::new();
        lines.push(self.title.clone());
        lines.extend(self.summary_lines.iter().cloned());
        if !self.label_lines.is_empty() {
            lines.push(String::new());
            lines.push("Labels".to_string());
            lines.extend(self.label_lines.iter().cloned());
        }
        if !self.context_lines.is_empty() {
            lines.push(String::new());
            lines.push("Context".to_string());
            lines.extend(self.context_lines.iter().cloned());
        }
        lines
    }
}

pub struct LineageReadModel {
    conn: Connection,
}

impl LineageReadModel {
    pub fn open_read_only(db_path: &Path) -> Result<Self> {
        let conn = Connection::open_with_flags(
            db_path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .with_context(|| {
            format!(
                "failed to open ROAR database read-only: {}",
                db_path.display()
            )
        })?;
        Ok(Self { conn })
    }

    pub fn list_sessions(&self) -> Result<Vec<SessionRow>> {
        let mut stmt = self.conn.prepare(
            r#"
            SELECT s.id, s.hash, s.created_at, s.git_repo, s.git_commit_start, s.git_commit_end,
                   (SELECT command FROM jobs WHERE session_id = s.id ORDER BY timestamp ASC, id ASC LIMIT 1) AS command,
                   (SELECT COUNT(*) FROM jobs WHERE session_id = s.id) AS job_count,
                   (SELECT COUNT(DISTINCT artifact_id) FROM (
                        SELECT ji.artifact_id FROM job_inputs ji JOIN jobs j ON j.id = ji.job_id WHERE j.session_id = s.id
                        UNION
                        SELECT jo.artifact_id FROM job_outputs jo JOIN jobs j ON j.id = jo.job_id WHERE j.session_id = s.id
                    )) AS artifact_count
            FROM sessions s
            ORDER BY s.created_at DESC, s.id DESC
            "#,
        )?;
        let rows = stmt.query_map([], |row| self.session_from_row(row))?;
        collect_rows(rows)
    }

    pub fn list_jobs(&self, session_id: i64) -> Result<Vec<JobRow>> {
        let mut stmt = self.conn.prepare(
            r#"
            SELECT j.id, j.session_id, j.job_uid, j.step_number, j.job_type, j.command,
                   j.timestamp, j.duration_seconds, j.exit_code, j.status, j.git_commit, j.git_branch,
                   json_extract(j.metadata, '$.cwd') AS cwd,
                   (SELECT COUNT(DISTINCT artifact_id) FROM job_inputs WHERE job_id = j.id) AS input_count,
                   (SELECT COUNT(DISTINCT artifact_id) FROM job_outputs WHERE job_id = j.id) AS output_count
            FROM jobs j
            WHERE j.session_id = ?
            ORDER BY CASE WHEN j.job_type = 'build' THEN 0 ELSE 1 END,
                     j.step_number IS NULL, j.step_number ASC, j.timestamp ASC, j.id ASC
            "#,
        )?;
        let rows = stmt.query_map(params![session_id], |row| self.job_from_row(row))?;
        collect_rows(rows)
    }

    pub fn list_artifacts(&self, job_id: i64) -> Result<Vec<ArtifactRow>> {
        let mut rows = Vec::new();
        rows.extend(self.list_artifacts_for_table(job_id, "input", "job_inputs")?);
        rows.extend(self.list_artifacts_for_table(job_id, "output", "job_outputs")?);
        rows.sort_by(|a, b| a.role.cmp(&b.role).then_with(|| a.path.cmp(&b.path)));
        Ok(rows)
    }

    fn list_artifacts_for_table(
        &self,
        job_id: i64,
        role: &str,
        table_name: &str,
    ) -> Result<Vec<ArtifactRow>> {
        let sql = format!(
            r#"
            SELECT io.artifact_id, io.path, a.size, COALESCE(a.kind, 'primitive') AS kind,
                   ah.algorithm, ah.digest
            FROM {table_name} io
            JOIN artifacts a ON a.id = io.artifact_id
            LEFT JOIN artifact_hashes ah ON ah.artifact_id = a.id
            WHERE io.job_id = ?
              AND (ah.algorithm IS NULL OR ah.algorithm = (
                  SELECT algorithm FROM artifact_hashes WHERE artifact_id = a.id
                  ORDER BY CASE WHEN algorithm = 'blake3' THEN 0 WHEN algorithm = 'sha256' THEN 1 ELSE 2 END,
                           algorithm ASC, digest ASC LIMIT 1
              ))
            ORDER BY io.path ASC, io.artifact_id ASC
            "#
        );
        let mut stmt = self.conn.prepare(&sql)?;
        let rows = stmt.query_map(params![job_id], |row| {
            let id: String = row.get(0)?;
            let path: String = row.get(1)?;
            let size: i64 = row.get(2)?;
            let kind: String = row.get(3)?;
            let hash_algorithm: Option<String> = row.get(4)?;
            let hash_digest: Option<String> = row.get(5)?;
            let producer = self.producer_for_artifact(&id)?;
            let consumers = self.consumers_for_artifact(&id)?;
            let labels = self.current_labels("artifact", None, None, Some(&id))?;
            Ok(ArtifactRow {
                id: id.clone(),
                job_id,
                role: role.to_string(),
                display: format!("{role}: {}", compact_path(&path)),
                path,
                size,
                kind,
                hash_algorithm,
                hash_digest,
                producer,
                consumers,
                labels,
            })
        })?;
        collect_rows(rows)
    }

    pub fn find_session_index(&self, sessions: &[SessionRow], reference: &str) -> Option<usize> {
        let needle = reference.trim();
        sessions.iter().position(|session| {
            session.id.to_string() == needle
                || session
                    .hash
                    .as_deref()
                    .is_some_and(|hash| hash.starts_with(needle))
        })
    }

    pub fn get_job_by_id(&self, job_id: i64) -> Result<Option<JobRow>> {
        let mut stmt = self.conn.prepare(
            r#"
            SELECT id, session_id, job_uid, step_number, job_type, command, timestamp,
                   duration_seconds, exit_code, status, git_commit, git_branch,
                   json_extract(metadata, '$.cwd') AS cwd,
                   (SELECT COUNT(DISTINCT artifact_id) FROM job_inputs WHERE job_id = jobs.id) AS input_count,
                   (SELECT COUNT(DISTINCT artifact_id) FROM job_outputs WHERE job_id = jobs.id) AS output_count
            FROM jobs
            WHERE id = ?
            LIMIT 1
            "#,
        )?;
        stmt.query_row(params![job_id], |row| self.job_from_row(row))
            .optional()
            .map_err(Into::into)
    }

    pub fn find_job(
        &self,
        reference: &str,
        default_session_id: Option<i64>,
    ) -> Result<Option<JobRow>> {
        if let Some(step_ref) = reference.strip_prefix('@') {
            let Some(session_id) = default_session_id else {
                return Ok(None);
            };
            let (job_type, number_text) = if let Some(build_ref) = step_ref.strip_prefix('B') {
                (Some("build"), build_ref)
            } else {
                (None, step_ref)
            };
            let Ok(step_number) = number_text.parse::<i64>() else {
                return Ok(None);
            };
            let mut jobs = self.list_jobs(session_id)?;
            return Ok(jobs.drain(..).find(|job| {
                job.step_number == Some(step_number)
                    && match job_type {
                        Some(expected) => job.job_type.as_deref() == Some(expected),
                        None => job.job_type.as_deref() != Some("build"),
                    }
            }));
        }

        let mut stmt = self.conn.prepare(
            r#"
            SELECT id, session_id, job_uid, step_number, job_type, command, timestamp,
                   duration_seconds, exit_code, status, git_commit, git_branch,
                   json_extract(metadata, '$.cwd') AS cwd,
                   (SELECT COUNT(DISTINCT artifact_id) FROM job_inputs WHERE job_id = jobs.id) AS input_count,
                   (SELECT COUNT(DISTINCT artifact_id) FROM job_outputs WHERE job_id = jobs.id) AS output_count
            FROM jobs
            WHERE job_uid = ? OR job_uid LIKE ?
            ORDER BY CASE WHEN job_uid = ? THEN 0 ELSE 1 END, timestamp DESC
            LIMIT 1
            "#,
        )?;
        stmt.query_row(
            params![reference, format!("{reference}%"), reference],
            |row| self.job_from_row(row),
        )
        .optional()
        .map_err(Into::into)
    }

    pub fn find_artifact_job(&self, artifact_ref: &str) -> Result<Option<(i64, String)>> {
        let artifact_id = self
            .conn
            .query_row(
                r#"
                SELECT a.id
                FROM artifacts a
                LEFT JOIN artifact_hashes ah ON ah.artifact_id = a.id
                WHERE a.id = ? OR a.id LIKE ? OR ah.digest = ? OR ah.digest LIKE ?
                ORDER BY CASE WHEN a.id = ? OR ah.digest = ? THEN 0 ELSE 1 END
                LIMIT 1
                "#,
                params![
                    artifact_ref,
                    format!("{artifact_ref}%"),
                    artifact_ref,
                    format!("{artifact_ref}%"),
                    artifact_ref,
                    artifact_ref,
                ],
                |row| row.get::<_, String>(0),
            )
            .optional()?;
        let Some(artifact_id) = artifact_id else {
            return Ok(None);
        };
        let job_id = self
            .conn
            .query_row(
                r#"
                SELECT job_id FROM job_outputs WHERE artifact_id = ?
                UNION ALL
                SELECT job_id FROM job_inputs WHERE artifact_id = ?
                LIMIT 1
                "#,
                params![artifact_id, artifact_id],
                |row| row.get::<_, i64>(0),
            )
            .optional()?;
        Ok(job_id.map(|id| (id, artifact_id)))
    }

    pub fn preview_session(&self, session: &SessionRow) -> Preview {
        let mut summary = Vec::new();
        if let Some(command) = &session.command {
            summary.push(format!("command: {command}"));
        }
        if let Some(repo) = &session.git_repo {
            summary.push(format!("repo: {repo}"));
        }
        if session.git_commit_start.is_some() || session.git_commit_end.is_some() {
            let start = session.git_commit_start.as_deref().unwrap_or("?");
            let end = session.git_commit_end.as_deref().unwrap_or(start);
            summary.push(format!("git: {}..{}", short(start), short(end)));
        }
        summary.push(format!(
            "started: {}",
            timestamp_seconds(session.created_at)
        ));
        summary.push(format!("jobs: {}", session.job_count));
        summary.push(format!("artifacts: {}", session.artifact_count));
        Preview {
            title: format!("Session {}", session_short(session)),
            summary_lines: summary,
            label_lines: format_labels(&session.labels),
            context_lines: Vec::new(),
        }
    }

    pub fn preview_job(&self, jobs: &[JobRow], index: usize) -> Preview {
        let job = &jobs[index];
        let mut summary = vec![
            format!("uid: {}", job.job_uid.as_deref().unwrap_or("?")),
            format!("command: {}", job.command),
        ];
        if let Some(cwd) = &job.cwd {
            summary.push(format!("cwd: {cwd}"));
        }
        summary.push(format!("status: {}", display_status(job)));
        summary.push(format!(
            "exit: {}",
            job.exit_code.map_or("?".to_string(), |v| v.to_string())
        ));
        summary.push(format!("started: {}", time_of_day(job.timestamp)));
        summary.push(format!(
            "duration: {}",
            format_duration(job.duration_seconds)
        ));
        summary.push(format!("inputs: {}", job.input_count));
        summary.push(format!("outputs: {}", job.output_count));

        let mut context = Vec::new();
        if index > 0 {
            context.push(format!("previous: {}", jobs[index - 1].display));
        }
        if index + 1 < jobs.len() {
            context.push(format!("next: {}", jobs[index + 1].display));
        }

        Preview {
            title: format!("Job {}", job.step_ref),
            summary_lines: summary,
            label_lines: format_labels(&job.labels),
            context_lines: context,
        }
    }

    pub fn preview_artifact(&self, artifact: &ArtifactRow, job: Option<&JobRow>) -> Preview {
        let hash = artifact.hash_digest.as_deref().unwrap_or(&artifact.id);
        let algo = artifact.hash_algorithm.as_deref().unwrap_or("artifact");
        let mut summary = vec![
            format!("path: {}", artifact.path),
            format!("role: {}", artifact.role),
            format!("size: {}", format_size(artifact.size)),
            format!("kind: {}", artifact.kind),
        ];
        if let Some(producer) = &artifact.producer {
            summary.push(format!("producer: {producer}"));
        }
        if !artifact.consumers.is_empty() {
            summary.push(format!("consumers: {}", artifact.consumers.join(", ")));
        }
        if let Some(job) = job {
            summary.push(format!("job: {}", job.step_ref));
        }
        let mut context = Vec::new();
        if let Some(job) = job {
            context.push(format!(
                "{} -> {} -> {}",
                artifact.role,
                job.step_ref,
                compact_path(&artifact.path)
            ));
        }
        Preview {
            title: format!("Artifact {algo}:{}", short(hash)),
            summary_lines: summary,
            label_lines: format_labels(&artifact.labels),
            context_lines: context,
        }
    }

    fn session_from_row(&self, row: &Row<'_>) -> rusqlite::Result<SessionRow> {
        let id: i64 = row.get(0)?;
        let hash: Option<String> = row.get(1)?;
        let created_at: f64 = row.get(2)?;
        let git_repo: Option<String> = row.get(3)?;
        let git_commit_start: Option<String> = row.get(4)?;
        let git_commit_end: Option<String> = row.get(5)?;
        let command: Option<String> = row.get(6)?;
        let job_count: i64 = row.get(7)?;
        let artifact_count: i64 = row.get(8)?;
        let labels = self.current_labels("dag", Some(id), None, None)?;
        let display_datetime = timestamp_minutes(created_at);
        let display = format!(
            "{} {}",
            display_datetime,
            short(hash.as_deref().unwrap_or("?"))
        );
        Ok(SessionRow {
            id,
            hash,
            created_at,
            display_datetime,
            display,
            command,
            git_repo,
            git_commit_start,
            git_commit_end,
            job_count,
            artifact_count,
            labels,
        })
    }

    fn job_from_row(&self, row: &Row<'_>) -> rusqlite::Result<JobRow> {
        let id: i64 = row.get(0)?;
        let session_id: i64 = row.get(1)?;
        let job_uid: Option<String> = row.get(2)?;
        let step_number: Option<i64> = row.get(3)?;
        let job_type: Option<String> = row.get(4)?;
        let command: String = row.get(5)?;
        let timestamp: f64 = row.get(6)?;
        let duration_seconds: Option<f64> = row.get(7)?;
        let exit_code: Option<i64> = row.get(8)?;
        let status: Option<String> = row.get(9)?;
        let git_commit: Option<String> = row.get(10)?;
        let git_branch: Option<String> = row.get(11)?;
        let cwd: Option<String> = row.get(12)?;
        let input_count: i64 = row.get(13)?;
        let output_count: i64 = row.get(14)?;
        let labels = self.current_labels("job", None, Some(id), None)?;
        let step_ref = step_reference(step_number, job_type.as_deref(), job_uid.as_deref());
        let display = format!("{step_ref} {command}");
        Ok(JobRow {
            id,
            session_id,
            job_uid,
            step_number,
            job_type,
            step_ref,
            display,
            command,
            cwd,
            timestamp,
            duration_seconds,
            exit_code,
            status,
            git_commit,
            git_branch,
            input_count,
            output_count,
            labels,
        })
    }

    fn current_labels(
        &self,
        entity_type: &str,
        session_id: Option<i64>,
        job_id: Option<i64>,
        artifact_id: Option<&str>,
    ) -> rusqlite::Result<Vec<LabelLine>> {
        let (clause, id_param): (&str, rusqlite::types::Value) = if let Some(id) = session_id {
            (
                "session_id = ? AND job_id IS NULL AND artifact_id IS NULL",
                id.into(),
            )
        } else if let Some(id) = job_id {
            (
                "session_id IS NULL AND job_id = ? AND artifact_id IS NULL",
                id.into(),
            )
        } else if let Some(id) = artifact_id {
            (
                "session_id IS NULL AND job_id IS NULL AND artifact_id = ?",
                id.to_string().into(),
            )
        } else {
            return Ok(Vec::new());
        };
        let sql = format!(
            "SELECT metadata FROM labels WHERE entity_type = ? AND {clause} ORDER BY version DESC LIMIT 1"
        );
        let raw: Option<String> = match self
            .conn
            .query_row(&sql, params![entity_type, id_param], |row| row.get(0))
            .optional()
        {
            Ok(value) => value,
            Err(err) if is_missing_table(&err) => None,
            Err(err) => return Err(err),
        };
        Ok(raw
            .and_then(|text| serde_json::from_str::<Value>(&text).ok())
            .map(flatten_labels)
            .unwrap_or_default())
    }

    fn producer_for_artifact(&self, artifact_id: &str) -> rusqlite::Result<Option<String>> {
        self.conn
            .query_row(
                r#"
                SELECT j.step_number, j.job_type, j.job_uid, j.command
                FROM jobs j JOIN job_outputs jo ON jo.job_id = j.id
                WHERE jo.artifact_id = ?
                ORDER BY j.timestamp DESC, j.id DESC
                LIMIT 1
                "#,
                params![artifact_id],
                |row| {
                    let step_number: Option<i64> = row.get(0)?;
                    let job_type: Option<String> = row.get(1)?;
                    let job_uid: Option<String> = row.get(2)?;
                    let command: String = row.get(3)?;
                    Ok(format!(
                        "{} {}",
                        step_reference(step_number, job_type.as_deref(), job_uid.as_deref()),
                        command
                    ))
                },
            )
            .optional()
    }

    fn consumers_for_artifact(&self, artifact_id: &str) -> rusqlite::Result<Vec<String>> {
        let mut stmt = self.conn.prepare(
            r#"
            SELECT j.step_number, j.job_type, j.job_uid, j.command
            FROM jobs j JOIN job_inputs ji ON ji.job_id = j.id
            WHERE ji.artifact_id = ?
            ORDER BY j.timestamp ASC, j.id ASC
            LIMIT 5
            "#,
        )?;
        let rows = stmt.query_map(params![artifact_id], |row| {
            let step_number: Option<i64> = row.get(0)?;
            let job_type: Option<String> = row.get(1)?;
            let job_uid: Option<String> = row.get(2)?;
            let command: String = row.get(3)?;
            Ok(format!(
                "{} {}",
                step_reference(step_number, job_type.as_deref(), job_uid.as_deref()),
                command
            ))
        })?;
        rows.collect::<rusqlite::Result<Vec<_>>>()
    }
}

fn collect_rows<T, F>(rows: rusqlite::MappedRows<'_, F>) -> Result<Vec<T>>
where
    F: FnMut(&Row<'_>) -> rusqlite::Result<T>,
{
    rows.collect::<rusqlite::Result<Vec<_>>>()
        .map_err(Into::into)
}

fn is_missing_table(err: &rusqlite::Error) -> bool {
    matches!(err, rusqlite::Error::SqliteFailure(_, Some(message)) if message.contains("no such table"))
}

pub fn resolve_database(
    path: Option<&Path>,
    explicit_db: Option<&Path>,
) -> Result<ResolvedDatabase> {
    if let Some(db) = explicit_db {
        if db.exists() {
            return Ok(ResolvedDatabase {
                db_path: db.to_path_buf(),
                looked_for: vec![db.to_path_buf()],
            });
        }
        return Ok(ResolvedDatabase {
            db_path: db.to_path_buf(),
            looked_for: vec![db.to_path_buf()],
        });
    }

    let start = path.unwrap_or_else(|| Path::new(".")).to_path_buf();
    let start_dir = if start.is_file() {
        start
            .parent()
            .unwrap_or_else(|| Path::new("."))
            .to_path_buf()
    } else {
        start
    };
    let mut looked_for = Vec::new();
    let mut current = start_dir.canonicalize().unwrap_or(start_dir);
    loop {
        let candidate = current.join(".roar").join("roar.db");
        looked_for.push(candidate.clone());
        if candidate.exists() {
            return Ok(ResolvedDatabase {
                db_path: candidate,
                looked_for,
            });
        }
        if !current.pop() {
            break;
        }
    }
    Ok(ResolvedDatabase {
        db_path: looked_for
            .first()
            .cloned()
            .unwrap_or_else(|| PathBuf::from(".roar/roar.db")),
        looked_for,
    })
}

#[derive(Debug, Clone)]
pub struct ResolvedDatabase {
    pub db_path: PathBuf,
    pub looked_for: Vec<PathBuf>,
}

pub fn no_database_message(looked_for: &[PathBuf]) -> String {
    let mut lines = vec![
        "No ROAR database found.".to_string(),
        String::new(),
        "Looked for:".to_string(),
    ];
    for path in looked_for {
        lines.push(format!("  {}", path.display()));
    }
    lines.push(String::new());
    lines.push("Try:".to_string());
    lines.push("  roar run -- <your command>".to_string());
    lines.push("  roar tui --db /path/to/.roar/roar.db".to_string());
    lines.join("\n")
}

fn flatten_labels(value: Value) -> Vec<LabelLine> {
    let mut out = BTreeMap::new();
    flatten_value("", &value, &mut out);
    out.into_iter()
        .filter(|(key, _)| !key.starts_with("roar.") && !key.starts_with("_roar"))
        .map(|(key, value)| LabelLine { key, value })
        .collect()
}

fn flatten_value(prefix: &str, value: &Value, out: &mut BTreeMap<String, String>) {
    match value {
        Value::Object(map) => {
            for (key, nested) in map {
                let next = if prefix.is_empty() {
                    key.clone()
                } else {
                    format!("{prefix}.{key}")
                };
                flatten_value(&next, nested, out);
            }
        }
        Value::Array(values) => {
            let rendered = values
                .iter()
                .map(render_json_scalar)
                .collect::<Vec<_>>()
                .join(",");
            if !prefix.is_empty() {
                out.insert(prefix.to_string(), rendered);
            }
        }
        _ => {
            if !prefix.is_empty() {
                out.insert(prefix.to_string(), render_json_scalar(value));
            }
        }
    }
}

fn render_json_scalar(value: &Value) -> String {
    match value {
        Value::String(value) => value.clone(),
        Value::Bool(value) => value.to_string(),
        Value::Number(value) => value.to_string(),
        Value::Null => "null".to_string(),
        other => other.to_string(),
    }
}

fn format_labels(labels: &[LabelLine]) -> Vec<String> {
    labels
        .iter()
        .map(|label| format!("{}={}", label.key, label.value))
        .collect()
}

fn step_reference(step_number: Option<i64>, job_type: Option<&str>, uid: Option<&str>) -> String {
    match step_number {
        Some(number) if job_type == Some("build") => format!("@B{number}"),
        Some(number) => format!("@{number}"),
        None => uid.map(short).unwrap_or_else(|| "job-?".to_string()),
    }
}

fn session_short(session: &SessionRow) -> String {
    session
        .hash
        .as_deref()
        .map(short)
        .unwrap_or_else(|| session.id.to_string())
}

fn short(value: &str) -> String {
    value.chars().take(8).collect()
}

fn compact_path(path: &str) -> String {
    Path::new(path)
        .file_name()
        .and_then(|name| name.to_str())
        .filter(|name| !name.is_empty())
        .unwrap_or(path)
        .to_string()
}

fn timestamp_minutes(ts: f64) -> String {
    Local
        .timestamp_opt(ts as i64, 0)
        .single()
        .map(|dt| dt.format("%Y-%m-%d %H:%M").to_string())
        .unwrap_or_else(|| "?".to_string())
}

fn timestamp_seconds(ts: f64) -> String {
    Local
        .timestamp_opt(ts as i64, 0)
        .single()
        .map(|dt| dt.format("%Y-%m-%d %H:%M:%S").to_string())
        .unwrap_or_else(|| "?".to_string())
}

fn time_of_day(ts: f64) -> String {
    Local
        .timestamp_opt(ts as i64, 0)
        .single()
        .map(|dt| dt.format("%H:%M:%S").to_string())
        .unwrap_or_else(|| "?".to_string())
}

fn format_duration(seconds: Option<f64>) -> String {
    let Some(seconds) = seconds else {
        return "?".to_string();
    };
    if seconds < 60.0 {
        format!("{seconds:.1}s")
    } else if seconds < 3600.0 {
        format!(
            "{}m {:.0}s",
            (seconds / 60.0).floor() as i64,
            seconds % 60.0
        )
    } else {
        format!(
            "{}h {}m",
            (seconds / 3600.0).floor() as i64,
            ((seconds % 3600.0) / 60.0).floor() as i64
        )
    }
}

fn format_size(size: i64) -> String {
    if size < 1024 {
        format!("{size}B")
    } else if size < 1024 * 1024 {
        format!("{:.1}KB", size as f64 / 1024.0)
    } else if size < 1024 * 1024 * 1024 {
        format!("{:.1}MB", size as f64 / 1024.0 / 1024.0)
    } else {
        format!("{:.1}GB", size as f64 / 1024.0 / 1024.0 / 1024.0)
    }
}

fn display_status(job: &JobRow) -> String {
    if let Some(status) = &job.status {
        return status.clone();
    }
    match job.exit_code {
        Some(0) => "complete".to_string(),
        Some(_) => "failed".to_string(),
        None => "?".to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;
    use tempfile::tempdir;

    #[test]
    fn read_model_lists_sessions_jobs_artifacts_and_labels() {
        let dir = tempdir().unwrap();
        let db = dir.path().join("roar.db");
        seed_db(&db);
        let model = LineageReadModel::open_read_only(&db).unwrap();

        let sessions = model.list_sessions().unwrap();
        assert_eq!(sessions.len(), 1);
        assert_eq!(sessions[0].display, "2026-04-24 09:13 ses_8g1s");
        assert_eq!(
            sessions[0].labels,
            vec![LabelLine {
                key: "project".into(),
                value: "mnist".into()
            }]
        );

        let jobs = model.list_jobs(sessions[0].id).unwrap();
        assert_eq!(jobs[0].display, "@1 python preprocess.py");
        assert_eq!(jobs[1].display, "@2 python train.py");
        assert_eq!(jobs[1].output_count, 1);

        let artifacts = model.list_artifacts(jobs[1].id).unwrap();
        assert!(artifacts
            .iter()
            .any(|artifact| artifact.display == "input: clean.parquet"));
        assert!(artifacts
            .iter()
            .any(|artifact| artifact.display == "output: model.pkl"));
    }

    #[test]
    fn resolve_database_walks_up_from_path() {
        let dir = tempdir().unwrap();
        let nested = dir.path().join("a/b");
        std::fs::create_dir_all(nested.join("child")).unwrap();
        std::fs::create_dir_all(dir.path().join(".roar")).unwrap();
        std::fs::write(dir.path().join(".roar/roar.db"), "").unwrap();

        let resolved = resolve_database(Some(&nested.join("child")), None).unwrap();
        assert_eq!(resolved.db_path, dir.path().join(".roar/roar.db"));
    }

    fn seed_db(path: &Path) {
        let conn = Connection::open(path).unwrap();
        conn.execute_batch(
            r#"
            CREATE TABLE sessions (id INTEGER PRIMARY KEY, hash TEXT, created_at REAL NOT NULL, git_repo TEXT, git_commit_start TEXT, git_commit_end TEXT);
            CREATE TABLE jobs (id INTEGER PRIMARY KEY, job_uid TEXT, parent_job_uid TEXT, timestamp REAL NOT NULL, command TEXT NOT NULL, script TEXT, step_identity TEXT, session_id INTEGER, step_number INTEGER, step_name TEXT, git_repo TEXT, git_commit TEXT, git_branch TEXT, duration_seconds REAL, exit_code INTEGER, synced_at REAL, status TEXT, execution_backend TEXT, execution_role TEXT, job_type TEXT, metadata TEXT, telemetry TEXT);
            CREATE TABLE artifacts (id TEXT PRIMARY KEY, size INTEGER NOT NULL, first_seen_at REAL NOT NULL, first_seen_path TEXT, kind TEXT NOT NULL DEFAULT 'primitive', metadata TEXT);
            CREATE TABLE artifact_hashes (artifact_id TEXT NOT NULL, algorithm TEXT NOT NULL, digest TEXT NOT NULL);
            CREATE TABLE job_inputs (job_id INTEGER NOT NULL, artifact_id TEXT NOT NULL, path TEXT NOT NULL);
            CREATE TABLE job_outputs (job_id INTEGER NOT NULL, artifact_id TEXT NOT NULL, path TEXT NOT NULL);
            CREATE TABLE labels (id INTEGER PRIMARY KEY, entity_type TEXT NOT NULL, session_id INTEGER, job_id INTEGER, artifact_id TEXT, version INTEGER NOT NULL, metadata TEXT NOT NULL);
            INSERT INTO sessions VALUES (1, 'ses_8g1s7q', 1777021984.0, '/repo', 'a1b2c3d', 'a1b2c3d');
            INSERT INTO jobs (id, job_uid, timestamp, command, session_id, step_number, duration_seconds, exit_code, metadata) VALUES
                (1, 'job_pre', 1777021990.0, 'python preprocess.py', 1, 1, 10.0, 0, '{"cwd":"/tmp"}'),
                (2, 'job_train', 1777022000.0, 'python train.py', 1, 2, 20.0, 0, '{"cwd":"/tmp"}');
            INSERT INTO artifacts VALUES ('art_clean', 100, 1777021999.0, 'clean.parquet', 'primitive', NULL), ('art_model', 2048, 1777022020.0, 'model.pkl', 'primitive', NULL);
            INSERT INTO artifact_hashes VALUES ('art_clean', 'blake3', 'cleanhash'), ('art_model', 'blake3', 'modelhash');
            INSERT INTO job_outputs VALUES (1, 'art_clean', 'clean.parquet'), (2, 'art_model', 'model.pkl');
            INSERT INTO job_inputs VALUES (2, 'art_clean', 'clean.parquet');
            INSERT INTO labels VALUES (1, 'dag', 1, NULL, NULL, 1, '{"project":"mnist"}');
            "#,
        )
        .unwrap();
    }
}
