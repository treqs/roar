use std::io::{self, Stdout};
use std::time::Duration;

use anyhow::Result;
use crossterm::event::{self, Event, KeyCode, KeyEvent, KeyModifiers};
use crossterm::execute;
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use ratatui::backend::CrosstermBackend;
use ratatui::Terminal;

use crate::app::App;
use crate::read_model::LineageReadModel;
use crate::render;

pub fn run(mut app: App, model: LineageReadModel) -> Result<()> {
    let mut terminal = TerminalSession::enter()?;
    let result = run_loop(&mut terminal.terminal, &mut app, &model);
    terminal.leave()?;
    result
}

fn run_loop(
    terminal: &mut Terminal<CrosstermBackend<Stdout>>,
    app: &mut App,
    model: &LineageReadModel,
) -> Result<()> {
    loop {
        terminal.draw(|frame| render::draw(frame, app, model))?;
        if !event::poll(Duration::from_millis(250))? {
            continue;
        }
        let Event::Key(key) = event::read()? else {
            continue;
        };
        if handle_key(app, model, key)? {
            break;
        }
    }
    Ok(())
}

fn handle_key(app: &mut App, model: &LineageReadModel, key: KeyEvent) -> Result<bool> {
    if app.overlay.is_some() {
        match key.code {
            KeyCode::Char('q') | KeyCode::Esc => {
                app.close_overlay();
            }
            KeyCode::Char('j') | KeyCode::Down => app.scroll_overlay_down(),
            KeyCode::Char('k') | KeyCode::Up => app.scroll_overlay_up(),
            _ => {}
        }
        return Ok(false);
    }

    match (key.code, key.modifiers) {
        (KeyCode::Char('q'), _) => return Ok(true),
        (KeyCode::Char('?'), _) => app.open_help(),
        (KeyCode::Char('i'), _) => app.open_preview(),
        (KeyCode::Esc, _) => {
            app.close_overlay();
        }
        (KeyCode::Char('j'), KeyModifiers::ALT) => app.scroll_preview_down(),
        (KeyCode::Char('k'), KeyModifiers::ALT) => app.scroll_preview_up(),
        (KeyCode::Char('j'), _) | (KeyCode::Down, _) => app.move_down(1),
        (KeyCode::Char('k'), _) | (KeyCode::Up, _) => app.move_up(1),
        (KeyCode::Char('J'), _) => app.move_down(8),
        (KeyCode::Char('K'), _) => app.move_up(8),
        (KeyCode::Char('G'), _) => app.last(),
        (KeyCode::Char('g'), _) => app.handle_g(),
        (KeyCode::Char('h'), _) | (KeyCode::Left, _) => app.back(),
        (KeyCode::Char('l'), _) | (KeyCode::Right, _) | (KeyCode::Enter, _) => app.enter(model)?,
        _ => {}
    }
    Ok(false)
}

struct TerminalSession {
    terminal: Terminal<CrosstermBackend<Stdout>>,
    active: bool,
}

impl TerminalSession {
    fn enter() -> Result<Self> {
        enable_raw_mode()?;
        let mut stdout = io::stdout();
        execute!(stdout, EnterAlternateScreen)?;
        let backend = CrosstermBackend::new(stdout);
        let terminal = Terminal::new(backend)?;
        Ok(Self {
            terminal,
            active: true,
        })
    }

    fn leave(&mut self) -> Result<()> {
        if self.active {
            disable_raw_mode()?;
            execute!(self.terminal.backend_mut(), LeaveAlternateScreen)?;
            self.terminal.show_cursor()?;
            self.active = false;
        }
        Ok(())
    }
}

impl Drop for TerminalSession {
    fn drop(&mut self) {
        let _ = self.leave();
    }
}
