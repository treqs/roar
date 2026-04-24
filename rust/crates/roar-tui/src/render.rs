use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, List, ListItem, Paragraph, Wrap};
use ratatui::Frame;

use crate::app::{App, Depth};
use crate::read_model::LineageReadModel;

pub fn draw(frame: &mut Frame<'_>, app: &App, model: &LineageReadModel) {
    let root = frame.area();
    let vertical = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1),
            Constraint::Min(1),
            Constraint::Length(1),
        ])
        .split(root);

    let (active, total) = app.active_position();
    let path = format!("{}  {}/{}", app.current_path(), active, total);
    frame.render_widget(Paragraph::new(path), vertical[0]);

    if app.sessions.is_empty() {
        let empty = Paragraph::new(
            "No sessions found.\n\nCreate lineage first with:\n  roar run -- <your command>",
        )
        .block(Block::default().borders(Borders::NONE))
        .wrap(Wrap { trim: false });
        frame.render_widget(empty, vertical[1]);
    } else {
        draw_columns(frame, app, model, vertical[1]);
    }

    frame.render_widget(Paragraph::new(app.status_line()), vertical[2]);

    if app.overlay.is_some() {
        draw_overlay(frame, app, model, root);
    }
}

fn draw_columns(frame: &mut Frame<'_>, app: &App, model: &LineageReadModel, area: Rect) {
    match app.depth {
        Depth::Sessions => {
            let chunks = Layout::default()
                .direction(Direction::Horizontal)
                .constraints([Constraint::Percentage(38), Constraint::Percentage(62)])
                .split(area);
            draw_list(
                frame,
                chunks[0],
                "Sessions",
                session_items(app),
                app.selected_session,
            );
            draw_preview(frame, chunks[1], app, model, "Preview");
        }
        Depth::Jobs => {
            let chunks = Layout::default()
                .direction(Direction::Horizontal)
                .constraints([
                    Constraint::Ratio(1, 8),
                    Constraint::Ratio(3, 8),
                    Constraint::Ratio(4, 8),
                ])
                .split(area);
            draw_list(
                frame,
                chunks[0],
                "Sessions",
                session_items(app),
                app.selected_session,
            );
            let items = if app.jobs.is_empty() {
                vec!["No jobs found".to_string()]
            } else {
                job_items(app)
            };
            draw_list(frame, chunks[1], "Jobs", items, app.selected_job);
            draw_preview(frame, chunks[2], app, model, "Preview");
        }
        Depth::Artifacts => {
            let chunks = Layout::default()
                .direction(Direction::Horizontal)
                .constraints([
                    Constraint::Ratio(1, 8),
                    Constraint::Ratio(3, 8),
                    Constraint::Ratio(4, 8),
                ])
                .split(area);
            draw_list(frame, chunks[0], "Jobs", job_items(app), app.selected_job);
            let items = if app.artifacts.is_empty() {
                vec!["No artifacts found".to_string()]
            } else {
                artifact_items(app)
            };
            draw_list(frame, chunks[1], "Artifacts", items, app.selected_artifact);
            draw_preview(frame, chunks[2], app, model, "Preview");
        }
    }
}

fn draw_list(frame: &mut Frame<'_>, area: Rect, title: &str, items: Vec<String>, selected: usize) {
    let rows = items
        .into_iter()
        .enumerate()
        .map(|(index, item)| {
            let style = if index == selected {
                Style::default().add_modifier(Modifier::UNDERLINED)
            } else {
                Style::default()
            };
            ListItem::new(Line::from(Span::styled(item, style)))
        })
        .collect::<Vec<_>>();
    let list = List::new(rows).block(Block::default().title(title).borders(Borders::RIGHT));
    frame.render_widget(list, area);
}

fn draw_preview(
    frame: &mut Frame<'_>,
    area: Rect,
    app: &App,
    model: &LineageReadModel,
    title: &str,
) {
    let lines = app.preview_lines(model).join("\n");
    let paragraph = Paragraph::new(lines)
        .block(Block::default().title(title))
        .scroll((app.preview_scroll, 0))
        .wrap(Wrap { trim: false });
    frame.render_widget(paragraph, area);
}

fn draw_overlay(frame: &mut Frame<'_>, app: &App, model: &LineageReadModel, root: Rect) {
    let area = centered_rect(88, 88, root);
    frame.render_widget(Clear, area);
    let content = app.overlay_lines(model).join("\n");
    let paragraph = Paragraph::new(content)
        .block(Block::default().title("Read-only").borders(Borders::ALL))
        .scroll((app.overlay_scroll, 0))
        .wrap(Wrap { trim: false });
    frame.render_widget(paragraph, area);
}

fn centered_rect(percent_x: u16, percent_y: u16, area: Rect) -> Rect {
    let vertical = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Percentage((100 - percent_y) / 2),
            Constraint::Percentage(percent_y),
            Constraint::Percentage((100 - percent_y) / 2),
        ])
        .split(area);
    Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Percentage((100 - percent_x) / 2),
            Constraint::Percentage(percent_x),
            Constraint::Percentage((100 - percent_x) / 2),
        ])
        .split(vertical[1])[1]
}

fn session_items(app: &App) -> Vec<String> {
    app.sessions
        .iter()
        .map(|session| session.display.clone())
        .collect()
}

fn job_items(app: &App) -> Vec<String> {
    app.jobs.iter().map(|job| job.display.clone()).collect()
}

fn artifact_items(app: &App) -> Vec<String> {
    app.artifacts
        .iter()
        .map(|artifact| artifact.display.clone())
        .collect()
}

pub fn render_ascii(app: &App, model: &LineageReadModel) -> String {
    let mut lines = vec![app.current_path()];
    match app.depth {
        Depth::Sessions => {
            lines.push("Sessions | Preview".to_string());
            push_rows(&mut lines, session_items(app), app.selected_session);
        }
        Depth::Jobs => {
            lines.push("Sessions | Jobs | Preview".to_string());
            push_rows(&mut lines, job_items(app), app.selected_job);
        }
        Depth::Artifacts => {
            lines.push("Jobs | Artifacts | Preview".to_string());
            push_rows(&mut lines, artifact_items(app), app.selected_artifact);
        }
    }
    lines.push("--- Preview ---".to_string());
    lines.extend(app.preview_lines(model));
    lines.push("--- Status ---".to_string());
    lines.push(app.status_line());
    lines.join("\n")
}

fn push_rows(lines: &mut Vec<String>, rows: Vec<String>, selected: usize) {
    for (index, row) in rows.into_iter().enumerate() {
        let width = row.chars().count().max(1);
        lines.push(row);
        if index == selected {
            lines.push("─".repeat(width));
        }
    }
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use rusqlite::Connection;
    use tempfile::tempdir;

    use crate::app::App;
    use crate::read_model::LineageReadModel;

    use super::render_ascii;

    #[test]
    fn ascii_render_underlines_selection_and_includes_preview_labels() {
        let dir = tempdir().unwrap();
        let db = dir.path().join("roar.db");
        seed_db(&db);
        let model = LineageReadModel::open_read_only(&db).unwrap();
        let app = App::load(db.clone(), &model, None, None, None).unwrap();
        let rendered = render_ascii(&app, &model);

        assert!(rendered.contains("Sessions | Preview"));
        assert!(rendered.contains("2026"));
        assert!(rendered.contains("────────"));
        assert!(rendered.contains("Labels"));
        assert!(rendered.contains("project=mnist"));
        assert!(rendered.contains("j/k move"));
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
            INSERT INTO jobs (id, job_uid, timestamp, command, session_id, step_number, duration_seconds, exit_code, metadata) VALUES (1, 'job_train', 1777022000.0, 'python train.py', 1, 1, 20.0, 0, '{}');
            INSERT INTO labels VALUES (1, 'dag', 1, NULL, NULL, 1, '{"project":"mnist"}');
            "#,
        ).unwrap();
    }
}
