use std::path::PathBuf;

use anyhow::Result;

use crate::read_model::{ArtifactRow, JobRow, LineageReadModel, Preview, SessionRow};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Depth {
    Sessions,
    Jobs,
    Artifacts,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Overlay {
    Help,
    Preview,
}

pub struct App {
    pub db_path: PathBuf,
    pub sessions: Vec<SessionRow>,
    pub jobs: Vec<JobRow>,
    pub artifacts: Vec<ArtifactRow>,
    pub depth: Depth,
    pub selected_session: usize,
    pub selected_job: usize,
    pub selected_artifact: usize,
    pub preview_scroll: u16,
    pub overlay_scroll: u16,
    pub overlay: Option<Overlay>,
    pending_g: bool,
}

impl App {
    pub fn load(
        db_path: PathBuf,
        model: &LineageReadModel,
        initial_session: Option<&str>,
        initial_job: Option<&str>,
        initial_artifact: Option<&str>,
    ) -> Result<Self> {
        let sessions = model.list_sessions()?;
        let mut app = Self {
            db_path,
            sessions,
            jobs: Vec::new(),
            artifacts: Vec::new(),
            depth: Depth::Sessions,
            selected_session: 0,
            selected_job: 0,
            selected_artifact: 0,
            preview_scroll: 0,
            overlay_scroll: 0,
            overlay: None,
            pending_g: false,
        };

        if let Some(session_ref) = initial_session {
            if let Some(index) = model.find_session_index(&app.sessions, session_ref) {
                app.selected_session = index;
            }
        }
        app.reload_jobs(model)?;

        if let Some(job_ref) = initial_job {
            if let Some(job) = model.find_job(job_ref, app.selected_session_id())? {
                if let Some(session_index) = app
                    .sessions
                    .iter()
                    .position(|session| session.id == job.session_id)
                {
                    app.selected_session = session_index;
                    app.reload_jobs(model)?;
                }
                if let Some(job_index) =
                    app.jobs.iter().position(|candidate| candidate.id == job.id)
                {
                    app.selected_job = job_index;
                    app.depth = Depth::Jobs;
                }
            }
        }

        if let Some(artifact_ref) = initial_artifact {
            if let Some((job_id, artifact_id)) = model.find_artifact_job(artifact_ref)? {
                if let Some(job) = model.get_job_by_id(job_id)? {
                    if let Some(session_index) = app
                        .sessions
                        .iter()
                        .position(|session| session.id == job.session_id)
                    {
                        app.selected_session = session_index;
                        app.reload_jobs(model)?;
                    }
                }
                if let Some(job_index) =
                    app.jobs.iter().position(|candidate| candidate.id == job_id)
                {
                    app.selected_job = job_index;
                    app.depth = Depth::Artifacts;
                    app.reload_artifacts(model)?;
                    if let Some(artifact_index) = app
                        .artifacts
                        .iter()
                        .position(|artifact| artifact.id == artifact_id)
                    {
                        app.selected_artifact = artifact_index;
                    }
                }
            }
        }

        if app.depth == Depth::Artifacts {
            app.reload_artifacts(model)?;
        }
        Ok(app)
    }

    pub fn current_path(&self) -> String {
        let mut path = format!("roar://local/{}/sessions", self.db_path.display());
        if matches!(self.depth, Depth::Jobs | Depth::Artifacts) {
            if let Some(session) = self.selected_session() {
                let session_ref = session.hash.as_deref().unwrap_or("session");
                path.push_str(&format!("/{session_ref}/jobs"));
            }
        }
        if self.depth == Depth::Artifacts {
            if let Some(job) = self.selected_job() {
                path.push_str(&format!("/{}/artifacts", job.step_ref));
            }
        }
        path
    }

    pub fn status_line(&self) -> String {
        match self.depth {
            Depth::Sessions => "j/k move  l enter jobs  i details  ? help  q quit".to_string(),
            Depth::Jobs => {
                "h sessions  j/k move jobs  l enter artifacts  i details  ? help  q quit"
                    .to_string()
            }
            Depth::Artifacts => {
                "h jobs  j/k move artifacts  i view details  ? help  q quit".to_string()
            }
        }
    }

    pub fn active_position(&self) -> (usize, usize) {
        match self.depth {
            Depth::Sessions => (self.selected_session.saturating_add(1), self.sessions.len()),
            Depth::Jobs => (self.selected_job.saturating_add(1), self.jobs.len()),
            Depth::Artifacts => (
                self.selected_artifact.saturating_add(1),
                self.artifacts.len(),
            ),
        }
    }

    pub fn selected_session(&self) -> Option<&SessionRow> {
        self.sessions.get(self.selected_session)
    }

    pub fn selected_job(&self) -> Option<&JobRow> {
        self.jobs.get(self.selected_job)
    }

    pub fn selected_artifact(&self) -> Option<&ArtifactRow> {
        self.artifacts.get(self.selected_artifact)
    }

    pub fn selected_session_id(&self) -> Option<i64> {
        self.selected_session().map(|session| session.id)
    }

    pub fn preview(&self, model: &LineageReadModel) -> Option<Preview> {
        match self.depth {
            Depth::Sessions => self
                .selected_session()
                .map(|session| model.preview_session(session)),
            Depth::Jobs => {
                (!self.jobs.is_empty()).then(|| model.preview_job(&self.jobs, self.selected_job))
            }
            Depth::Artifacts => self
                .selected_artifact()
                .map(|artifact| model.preview_artifact(artifact, self.selected_job())),
        }
    }

    pub fn preview_lines(&self, model: &LineageReadModel) -> Vec<String> {
        self.preview(model)
            .map(|preview| preview.lines())
            .unwrap_or_default()
    }

    pub fn help_lines() -> Vec<String> {
        vec![
            "ROAR TUI help".to_string(),
            "".to_string(),
            "j / Down        move selection down".to_string(),
            "k / Up          move selection up".to_string(),
            "h / Left        move one level up".to_string(),
            "l / Right/Enter move one level down".to_string(),
            "gg              jump to first row".to_string(),
            "G               jump to last row".to_string(),
            "J / K           move half-page down/up".to_string(),
            "Alt-j / Alt-k   scroll preview pane".to_string(),
            "i               expand read-only preview".to_string(),
            "?               show this help".to_string(),
            "Esc / q         close overlay or quit".to_string(),
            "".to_string(),
            "This first version is read-only: no edit, replay, copy, open, delete, or publish actions are available.".to_string(),
        ]
    }

    pub fn overlay_lines(&self, model: &LineageReadModel) -> Vec<String> {
        match self.overlay {
            Some(Overlay::Help) => Self::help_lines(),
            Some(Overlay::Preview) => self.preview_lines(model),
            None => Vec::new(),
        }
    }

    pub fn move_down(&mut self, amount: usize) {
        self.pending_g = false;
        let len = self.active_len();
        let selected = self.active_selected_mut();
        if len > 0 {
            *selected = (*selected + amount).min(len - 1);
        }
        self.preview_scroll = 0;
    }

    pub fn move_up(&mut self, amount: usize) {
        self.pending_g = false;
        let selected = self.active_selected_mut();
        *selected = selected.saturating_sub(amount);
        self.preview_scroll = 0;
    }

    pub fn first(&mut self) {
        *self.active_selected_mut() = 0;
        self.preview_scroll = 0;
        self.pending_g = false;
    }

    pub fn last(&mut self) {
        let len = self.active_len();
        if len > 0 {
            *self.active_selected_mut() = len - 1;
        }
        self.preview_scroll = 0;
        self.pending_g = false;
    }

    pub fn handle_g(&mut self) {
        if self.pending_g {
            self.first();
        } else {
            self.pending_g = true;
        }
    }

    pub fn enter(&mut self, model: &LineageReadModel) -> Result<()> {
        self.pending_g = false;
        match self.depth {
            Depth::Sessions if !self.sessions.is_empty() => {
                self.depth = Depth::Jobs;
                self.reload_jobs(model)?;
            }
            Depth::Jobs if !self.jobs.is_empty() => {
                self.depth = Depth::Artifacts;
                self.reload_artifacts(model)?;
            }
            _ => {}
        }
        self.preview_scroll = 0;
        Ok(())
    }

    pub fn back(&mut self) {
        self.pending_g = false;
        self.depth = match self.depth {
            Depth::Sessions => Depth::Sessions,
            Depth::Jobs => Depth::Sessions,
            Depth::Artifacts => Depth::Jobs,
        };
        self.preview_scroll = 0;
    }

    pub fn open_help(&mut self) {
        self.overlay = Some(Overlay::Help);
        self.overlay_scroll = 0;
        self.pending_g = false;
    }

    pub fn open_preview(&mut self) {
        self.overlay = Some(Overlay::Preview);
        self.overlay_scroll = 0;
        self.pending_g = false;
    }

    pub fn close_overlay(&mut self) -> bool {
        let had_overlay = self.overlay.take().is_some();
        self.overlay_scroll = 0;
        self.pending_g = false;
        had_overlay
    }

    pub fn scroll_preview_down(&mut self) {
        self.preview_scroll = self.preview_scroll.saturating_add(1);
    }

    pub fn scroll_preview_up(&mut self) {
        self.preview_scroll = self.preview_scroll.saturating_sub(1);
    }

    pub fn scroll_overlay_down(&mut self) {
        self.overlay_scroll = self.overlay_scroll.saturating_add(1);
    }

    pub fn scroll_overlay_up(&mut self) {
        self.overlay_scroll = self.overlay_scroll.saturating_sub(1);
    }

    pub fn reload_jobs(&mut self, model: &LineageReadModel) -> Result<()> {
        self.jobs = if let Some(session) = self.selected_session() {
            model.list_jobs(session.id)?
        } else {
            Vec::new()
        };
        self.selected_job = self.selected_job.min(self.jobs.len().saturating_sub(1));
        Ok(())
    }

    pub fn reload_artifacts(&mut self, model: &LineageReadModel) -> Result<()> {
        self.artifacts = if let Some(job) = self.selected_job() {
            model.list_artifacts(job.id)?
        } else {
            Vec::new()
        };
        self.selected_artifact = self
            .selected_artifact
            .min(self.artifacts.len().saturating_sub(1));
        Ok(())
    }

    fn active_selected_mut(&mut self) -> &mut usize {
        match self.depth {
            Depth::Sessions => &mut self.selected_session,
            Depth::Jobs => &mut self.selected_job,
            Depth::Artifacts => &mut self.selected_artifact,
        }
    }

    fn active_len(&self) -> usize {
        match self.depth {
            Depth::Sessions => self.sessions.len(),
            Depth::Jobs => self.jobs.len(),
            Depth::Artifacts => self.artifacts.len(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_app() -> App {
        App {
            db_path: PathBuf::from("/tmp/.roar/roar.db"),
            sessions: vec![SessionRow {
                id: 1,
                hash: Some("ses_1".into()),
                created_at: 0.0,
                display_datetime: "1970-01-01 00:00".into(),
                display: "1970-01-01 00:00 ses_1".into(),
                command: None,
                git_repo: None,
                git_commit_start: None,
                git_commit_end: None,
                job_count: 2,
                artifact_count: 1,
                labels: vec![],
            }],
            jobs: vec![
                JobRow {
                    id: 10,
                    session_id: 1,
                    job_uid: Some("job_1".into()),
                    step_number: Some(1),
                    job_type: None,
                    step_ref: "@1".into(),
                    display: "@1 one".into(),
                    command: "one".into(),
                    cwd: None,
                    timestamp: 0.0,
                    duration_seconds: None,
                    exit_code: Some(0),
                    status: None,
                    git_commit: None,
                    git_branch: None,
                    input_count: 0,
                    output_count: 0,
                    labels: vec![],
                },
                JobRow {
                    id: 11,
                    session_id: 1,
                    job_uid: Some("job_2".into()),
                    step_number: Some(2),
                    job_type: None,
                    step_ref: "@2".into(),
                    display: "@2 two".into(),
                    command: "two".into(),
                    cwd: None,
                    timestamp: 1.0,
                    duration_seconds: None,
                    exit_code: Some(0),
                    status: None,
                    git_commit: None,
                    git_branch: None,
                    input_count: 0,
                    output_count: 0,
                    labels: vec![],
                },
            ],
            artifacts: vec![],
            depth: Depth::Jobs,
            selected_session: 0,
            selected_job: 0,
            selected_artifact: 0,
            preview_scroll: 0,
            overlay_scroll: 0,
            overlay: None,
            pending_g: false,
        }
    }

    #[test]
    fn navigation_moves_and_bounds_active_column() {
        let mut app = sample_app();
        app.move_down(1);
        assert_eq!(app.selected_job, 1);
        app.move_down(10);
        assert_eq!(app.selected_job, 1);
        app.move_up(1);
        assert_eq!(app.selected_job, 0);
        app.move_up(10);
        assert_eq!(app.selected_job, 0);
    }

    #[test]
    fn gg_and_g_jump_to_first_and_last() {
        let mut app = sample_app();
        app.last();
        assert_eq!(app.selected_job, 1);
        app.handle_g();
        assert_eq!(app.selected_job, 1);
        app.handle_g();
        assert_eq!(app.selected_job, 0);
    }

    #[test]
    fn back_moves_up_hierarchy_without_modal_state() {
        let mut app = sample_app();
        app.depth = Depth::Artifacts;
        app.back();
        assert_eq!(app.depth, Depth::Jobs);
        app.back();
        assert_eq!(app.depth, Depth::Sessions);
        app.back();
        assert_eq!(app.depth, Depth::Sessions);
    }
}
