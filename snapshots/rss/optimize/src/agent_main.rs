//! `tinycoder-agent`: the full program (agent loop, batch, and both servers). Normally reached
//! through `tinycoder run|batch`, which execs it; `run`'s MCP servers are the lean `tinycoder`
//! binary next to it, `batch` serves them in-process.

mod agent;
mod batch;
mod cli;
mod files;
mod mcp_util;
mod shell;

use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use clap::Parser;

fn main() {
    let now = Instant::now();
    let parsed = cli::Cli::try_parse();
    let cmd = match parsed {
        Ok(c) => c.cmd,
        // SPEC-SCALE.md 3: an unusable `batch` invocation exits 1 with nothing on stdout.
        Err(e) if e.use_stderr() && std::env::args_os().nth(1).is_some_and(|a| a == "batch") => {
            let _ = e.print();
            std::process::exit(1);
        }
        Err(e) => e.exit(),
    };
    let code = match cmd {
        cli::Cmd::Serve { kind } => cli::serve_main(kind),
        cli::Cmd::Run { task, workspace, runs, started_ns } => {
            // Count wall time from the launcher's start when it handed one over.
            let t0 = started_ns
                .and_then(|s| {
                    let unix_now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_nanos();
                    let ago = u64::try_from(unix_now.checked_sub(s)?).ok()?;
                    (ago < 60_000_000_000).then(|| now.checked_sub(Duration::from_nanos(ago))).flatten()
                })
                .unwrap_or(now);
            cli::runtime().block_on(agent::run(t0, &task, &workspace, &runs))
        }
        cli::Cmd::Batch { manifest, concurrency } => batch::batch_main(&manifest, concurrency),
    };
    std::process::exit(code);
}
