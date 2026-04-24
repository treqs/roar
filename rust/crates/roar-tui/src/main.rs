use std::path::PathBuf;

use anyhow::Result;
use clap::Parser;
use roar_tui::app::App;
use roar_tui::read_model::{no_database_message, resolve_database, LineageReadModel};

#[derive(Debug, Parser)]
#[command(
    name = "roar-tui",
    about = "Read-only terminal explorer for local ROAR lineage"
)]
struct Args {
    /// Project path used to find the nearest .roar/roar.db
    path: Option<PathBuf>,

    /// Open this ROAR SQLite database instead of searching from PATH/current directory
    #[arg(long)]
    db: Option<PathBuf>,

    /// Select a session by id or hash prefix
    #[arg(long)]
    session: Option<String>,

    /// Select a job by @N/@BN or job UID prefix
    #[arg(long)]
    job: Option<String>,

    /// Select an artifact by id/hash prefix
    #[arg(long)]
    artifact: Option<String>,
}

fn main() {
    if let Err(err) = run() {
        eprintln!("error: {err:#}");
        std::process::exit(1);
    }
}

fn run() -> Result<()> {
    let args = Args::parse();
    let resolved = resolve_database(args.path.as_deref(), args.db.as_deref())?;
    if !resolved.db_path.exists() {
        println!("{}", no_database_message(&resolved.looked_for));
        std::process::exit(1);
    }

    let model = LineageReadModel::open_read_only(&resolved.db_path)?;
    let app = App::load(
        resolved.db_path,
        &model,
        args.session.as_deref(),
        args.job.as_deref(),
        args.artifact.as_deref(),
    )?;
    roar_tui::terminal::run(app, model)
}
