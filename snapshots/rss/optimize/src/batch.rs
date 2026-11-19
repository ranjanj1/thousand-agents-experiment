//! `tinycoder batch --manifest <file.json> --concurrency <N>` (SPEC-SCALE.md): many agents in
//! one process. Each agent runs the same loop as `run` (agent::run_agent) with its own `files`
//! and `shell` servers, served in-process by rmcp over an in-memory pipe. All agents share one
//! HTTP client (one connection pool) and one multi-threaded Tokio runtime.

use std::collections::HashMap;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Instant;

use serde_json::Value;
use tokio::sync::Semaphore;
use tokio::task::JoinSet;

use crate::agent::{self, Job, Servers, Shared};
use crate::files::FilesLimits;
use crate::shell;

const MAX_AGENTS: usize = 2000;

struct Entry {
    run_id: String,
    task: PathBuf,
    workspace: PathBuf,
    runs: PathBuf,
}

fn load_manifest(path: &Path) -> Result<Vec<Entry>, String> {
    let raw = std::fs::read(path).map_err(|e| format!("cannot read manifest {}: {e}", path.display()))?;
    let v: Value = serde_json::from_slice(&raw).map_err(|e| format!("manifest is not valid JSON: {e}"))?;
    let list = v
        .as_object()
        .and_then(|o| o.get("agents"))
        .and_then(Value::as_array)
        .ok_or("manifest must be an object with an \"agents\" array")?;
    if list.is_empty() || list.len() > MAX_AGENTS {
        return Err(format!("manifest has {} agents; it must have between 1 and {MAX_AGENTS}", list.len()));
    }
    let mut out = Vec::with_capacity(list.len());
    for (i, a) in list.iter().enumerate() {
        let o = a.as_object().ok_or_else(|| format!("agents[{i}] must be an object"))?;
        let field = |k: &str| -> Result<String, String> {
            match o.get(k) {
                Some(Value::String(s)) if !s.is_empty() => Ok(s.clone()),
                Some(Value::String(_)) => Err(format!("agents[{i}].{k} must not be empty")),
                _ => Err(format!("agents[{i}].{k} must be a string")),
            }
        };
        let run_id = field("run_id")?;
        if run_id.contains(['/', '\0']) || run_id == "." || run_id == ".." {
            return Err(format!("agents[{i}].run_id {run_id:?} cannot be used as a file name"));
        }
        out.push(Entry {
            run_id,
            task: field("task")?.into(),
            workspace: field("workspace")?.into(),
            runs: field("runs")?.into(),
        });
    }
    Ok(out)
}

#[repr(C)]
struct RLimit {
    cur: u64,
    max: u64,
}

unsafe extern "C" {
    fn getrlimit(resource: i32, rlim: *mut RLimit) -> i32;
    fn setrlimit(resource: i32, rlim: *const RLimit) -> i32;
}

#[cfg(target_os = "linux")]
const RLIMIT_NPROC: i32 = 6;
#[cfg(target_os = "linux")]
const RLIMIT_NOFILE: i32 = 7;
#[cfg(not(target_os = "linux"))]
const RLIMIT_NPROC: i32 = 7;
#[cfg(not(target_os = "linux"))]
const RLIMIT_NOFILE: i32 = 8;

/// Raise a soft limit towards its hard limit (never above it). macOS refuses values above
/// `kern.maxfilesperproc` for descriptors even when the hard limit is "unlimited", so smaller
/// values are tried in turn.
fn raise_soft_limit(resource: i32) {
    let mut cur = RLimit { cur: 0, max: 0 };
    if unsafe { getrlimit(resource, &mut cur) } != 0 || cur.cur >= cur.max {
        return;
    }
    for want in [cur.max, 1 << 20, 1 << 18, 1 << 16, 24_576, 10_240, 4_096] {
        if want <= cur.cur || want > cur.max {
            continue;
        }
        if unsafe { setrlimit(resource, &RLimit { cur: want, max: cur.max }) } == 0 {
            return;
        }
    }
}

fn print_line(run_id: &str, final_json: &str) {
    let line = format!("{{\"run_id\":{},{}\n", Value::String(run_id.to_string()), &final_json[1..]);
    let mut so = std::io::stdout().lock();
    let _ = so.write_all(line.as_bytes());
    let _ = so.flush();
}

async fn one(shared: Arc<Shared>, a: Entry) {
    let t0 = Instant::now();
    let tag = format!(" [{}]", a.run_id);
    let job = Job { run_id: &a.run_id, task_dir: &a.task, workspace: &a.workspace, runs: &a.runs };
    let out = agent::run_agent(t0, job, &shared, Servers::InProcess(FilesLimits::BATCH), &tag).await;
    print_line(&a.run_id, &out.json);
}

/// Last resort when an agent task died without reporting (run_agent already catches panics).
fn report_lost(run_id: &str, runs: &Path, why: &str) {
    let fin = agent::Final {
        status: "error",
        reason: Some(format!("agent crashed: {why}")),
        steps: 0,
        tool_calls: 0,
        blocked_calls: 0,
        input_tokens: 0,
        output_tokens: 0,
        wall_ms: 0.0,
        llm_ms: 0.0,
        tool_ms: 0.0,
        final_text: String::new(),
    };
    let json = serde_json::to_string(&fin).unwrap_or_default();
    if let Ok(mut f) =
        std::fs::OpenOptions::new().create(true).append(true).open(runs.join(format!("{run_id}.jsonl")))
    {
        let _ = writeln!(f, "{{\"type\":\"final\",{}", &json[1..]);
    }
    print_line(run_id, &json);
}

async fn run_all(agents: Vec<Entry>, concurrency: usize) {
    let shared = Arc::new(Shared::new());
    let sem = Arc::new(Semaphore::new(concurrency));
    let mut set = JoinSet::new();
    let mut ids: HashMap<tokio::task::Id, (String, PathBuf)> = HashMap::new();
    let reap = |r: Result<(tokio::task::Id, ()), tokio::task::JoinError>,
                ids: &mut HashMap<tokio::task::Id, (String, PathBuf)>| match r {
        Ok((id, ())) => {
            ids.remove(&id);
        }
        Err(e) => {
            if let Some((rid, runs)) = ids.remove(&e.id()) {
                report_lost(&rid, &runs, &e.to_string());
            }
        }
    };
    // Start in manifest order; each start waits for a free slot.
    for a in agents {
        let permit = sem.clone().acquire_owned().await.expect("semaphore is never closed");
        while let Some(r) = set.try_join_next_with_id() {
            reap(r, &mut ids);
        }
        let key = (a.run_id.clone(), a.runs.clone());
        let shared = shared.clone();
        let handle = set.spawn(async move {
            one(shared, a).await;
            drop(permit);
        });
        ids.insert(handle.id(), key);
    }
    while let Some(r) = set.join_next_with_id().await {
        reap(r, &mut ids);
    }
}

/// Commands (from all agents) that may start and run side by side: `TINYCODER_PROCESS_SLOTS`, or
/// by default one per CPU (test commands are mostly CPU-bound; see NOTES.md and shell.rs).
fn process_slots() -> usize {
    std::env::var("TINYCODER_PROCESS_SLOTS")
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .filter(|&n| n > 0)
        .unwrap_or_else(|| std::thread::available_parallelism().map(|n| n.get()).unwrap_or(4))
}

/// Returns the exit code: 0 once every agent has finished and printed its line, 1 (with no
/// agent started) when the manifest cannot be read or is invalid.
pub fn batch_main(manifest: &Path, concurrency: u32) -> i32 {
    let agents = match load_manifest(manifest) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("tinycoder batch: {e}");
            return 1;
        }
    };
    raise_soft_limit(RLIMIT_NOFILE);
    raise_soft_limit(RLIMIT_NPROC);
    let slots = process_slots();
    shell::limit_processes(slots, 4 * slots);
    let rt = match tokio::runtime::Builder::new_multi_thread().enable_all().build() {
        Ok(rt) => rt,
        Err(e) => {
            eprintln!("tinycoder batch: cannot start the runtime: {e}");
            return 1;
        }
    };
    rt.block_on(run_all(agents, concurrency as usize));
    0
}
