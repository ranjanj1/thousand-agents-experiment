//! The agent loop (SPEC.md section 4), shared by `run` (one agent, servers as stdio child
//! processes) and `batch` (many agents in one process, servers in-process; SPEC-SCALE.md).

use std::collections::HashMap;
use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use rmcp::model::CallToolRequestParams;
use rmcp::service::RunningService;
use rmcp::transport::TokioChildProcess;
use rmcp::{RoleClient, ServerHandler, ServiceExt};
use serde::{Deserialize, Serialize};
use serde_json::value::RawValue;
use serde_json::{Map, Value, json};

use crate::files::{FilesLimits, FilesServer};
use crate::mcp_util::CatchUnwind;
use crate::shell::{self, ShellServer};

const MODEL: &str = "claude-sonnet-5-5";
const API_VERSION: &str = "2026-10-21";
const MAX_TOKENS: u32 = 16000;
const MAX_STEPS: u32 = 20;
const MAX_WRITE_BYTES: usize = 100_000;
const RETRY_STATUS: &[u16] = &[429, 500, 502, 503, 529];
const RETRY_WAITS: &[u64] = &[1, 2, 4];
const EMBEDDED_SYSTEM: &str = include_str!("../shared/system.txt");
/// Buffer of the in-process byte pipe between an agent and one of its servers.
const INPROC_PIPE_BYTES: usize = 64 * 1024;

type Client = RunningService<RoleClient, ()>;

#[derive(Serialize)]
pub struct Final {
    pub status: &'static str,
    pub reason: Option<String>,
    pub steps: u32,
    pub tool_calls: u32,
    pub blocked_calls: u32,
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub wall_ms: f64,
    pub llm_ms: f64,
    pub tool_ms: f64,
    pub final_text: String,
}

impl Final {
    pub fn exit_code(&self) -> i32 {
        match self.status {
            "done" => 0,
            "step_limit" => 2,
            _ => 1,
        }
    }
}

#[derive(Serialize)]
struct ToolEvent<'a> {
    #[serde(rename = "type")]
    kind: &'static str,
    step: u32,
    tool: &'a str,
    blocked: bool,
    ok: bool,
    duration_ms: f64,
    args_bytes: usize,
    result_bytes: usize,
}

#[derive(Serialize)]
struct LlmEvent<'a> {
    #[serde(rename = "type")]
    kind: &'static str,
    step: u32,
    duration_ms: f64,
    input_tokens: u64,
    output_tokens: u64,
    cache_read_input_tokens: u64,
    stop_reason: &'a str,
}

fn ms(d: Duration) -> f64 {
    (d.as_secs_f64() * 1e6).round() / 1e3
}

struct RunLog {
    file: Option<File>,
    tag: String,
}

impl RunLog {
    fn line(&mut self, s: &str) {
        if let Some(f) = self.file.as_mut() {
            let mut buf = String::with_capacity(s.len() + 1);
            buf.push_str(s);
            buf.push('\n');
            if let Err(e) = f.write_all(buf.as_bytes()) {
                eprintln!("tinycoder{}: cannot write runs file: {e}", self.tag);
            }
        }
    }
    fn event<T: Serialize>(&mut self, ev: &T) {
        self.line(&serde_json::to_string(ev).unwrap_or_default());
    }
}

/// Find `shared/system.txt` next to the program (walking up from the binary), then from the
/// working directory; fall back to the copy embedded at build time.
pub fn system_prompt() -> String {
    let mut starts: Vec<PathBuf> = Vec::new();
    if let Ok(exe) = std::env::current_exe() {
        starts.push(std::fs::canonicalize(&exe).unwrap_or(exe));
    }
    if let Ok(cwd) = std::env::current_dir() {
        starts.push(cwd.join("x"));
    }
    for s in starts {
        let mut dir = s.parent();
        while let Some(d) = dir {
            if let Ok(t) = std::fs::read_to_string(d.join("shared").join("system.txt")) {
                return t;
            }
            dir = d.parent();
        }
    }
    EMBEDDED_SYSTEM.to_string()
}

/// SPEC.md 4.4 pre-call hook. Returns the reason when the call must be blocked.
fn hook_check(name: &str, input: &Value, known: &HashMap<String, usize>) -> Option<String> {
    if !known.contains_key(name) {
        return Some(format!("unknown tool: {name}"));
    }
    let args = input.as_object();
    if let Some(Value::String(p)) = args.and_then(|a| a.get("path")) {
        if p.starts_with('/') || p.starts_with('\\') || Path::new(p).is_absolute() {
            return Some(format!("absolute path not allowed: {p}"));
        }
        if p.split(['/', '\\']).any(|part| part == "..") {
            return Some(format!("path with '..' not allowed: {p}"));
        }
    }
    if name == "write_file" {
        if let Some(Value::String(c)) = args.and_then(|a| a.get("content")) {
            if c.len() > MAX_WRITE_BYTES {
                return Some(format!("write_file content is {} bytes, over the 100000 byte limit", c.len()));
            }
        }
    }
    if name == "run_command" {
        let argv = match shell::parse_argv(args.and_then(|a| a.get("argv"))) {
            Ok(a) => a,
            Err(e) => return Some(e),
        };
        if let Err(e) = shell::command_allowed(&argv) {
            return Some(e);
        }
    }
    None
}

/// How an agent reaches its two MCP servers.
#[derive(Clone, Copy)]
pub enum Servers {
    /// `tinycoder serve files|shell` child processes on stdio (SPEC.md section 1).
    Stdio,
    /// Server handlers in this process, each on its own rmcp async-rw transport over an
    /// in-memory pipe (SPEC-SCALE.md section 2), with the given `files` server limits.
    InProcess(FilesLimits),
}

/// The servers run as the lean `tinycoder` binary next to this one (no TLS stack, so it starts
/// faster); if it is missing, this binary serves them itself.
fn server_exe() -> std::io::Result<PathBuf> {
    let exe = std::env::current_exe()?;
    let exe = std::fs::canonicalize(&exe).unwrap_or(exe);
    let lean = exe.with_file_name("tinycoder");
    Ok(if lean != exe && lean.is_file() { lean } else { exe })
}

async fn start_child_server(args: Vec<String>) -> anyhow::Result<Client> {
    let mut cmd = tokio::process::Command::new(server_exe()?);
    cmd.args(&args);
    let transport = TokioChildProcess::new(cmd)?;
    let client = ().serve(transport).await.map_err(|e| anyhow::anyhow!("MCP server {}: {e}", args[1]))?;
    Ok(client)
}

/// Serve `server` on one end of an in-memory pipe and connect a client to the other end. Both
/// sides are rmcp services; rmcp does all framing and protocol work.
async fn start_inproc_server<S: ServerHandler>(name: &str, server: anyhow::Result<S>) -> anyhow::Result<Client> {
    let server = server.map_err(|e| anyhow::anyhow!("MCP server {name}: {e:#}"))?;
    let (client_io, server_io) = tokio::io::duplex(INPROC_PIPE_BYTES);
    let label = name.to_string();
    tokio::spawn(async move {
        match server.serve(server_io).await {
            Ok(running) => {
                let _ = running.waiting().await;
            }
            Err(e) => eprintln!("tinycoder: MCP server {label}: {e}"),
        }
    });
    ().serve(client_io).await.map_err(|e| anyhow::anyhow!("MCP server {name}: {e}"))
}

pub struct Api {
    http: reqwest::Client,
    url: String,
    key: String,
}

impl Api {
    pub fn new() -> anyhow::Result<Self> {
        let base = std::env::var("ANTHROPIC_BASE_URL")
            .ok()
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "https://api.anthropic.com".into());
        let url = format!("{}/v1/messages", base.trim_end_matches('/'));
        let key = std::env::var("ANTHROPIC_API_KEY").unwrap_or_default();
        let http = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(30))
            .timeout(Duration::from_secs(600))
            .build()?;
        Ok(Self { http, url, key })
    }

    /// One model call with up to 3 retries (SPEC.md 4.1). Err is the reason for status `error`.
    async fn send(&self, body: Vec<u8>, tag: &str) -> Result<Vec<u8>, String> {
        let mut attempt = 0usize;
        loop {
            let res = self
                .http
                .post(&self.url)
                .header("x-api-key", &self.key)
                .header("anthropic-version", API_VERSION)
                .header("content-type", "application/json")
                .body(body.clone())
                .send()
                .await;
            let retry_why = match res {
                Ok(r) => {
                    let status = r.status().as_u16();
                    match r.bytes().await {
                        Ok(bytes) if status == 200 => return Ok(bytes.to_vec()),
                        Ok(bytes) => {
                            let text = String::from_utf8_lossy(&bytes);
                            let text: String = text.chars().take(500).collect();
                            let why = format!("HTTP {status}: {text}");
                            if !RETRY_STATUS.contains(&status) {
                                return Err(why);
                            }
                            why
                        }
                        Err(e) => format!("reading response failed: {e}"),
                    }
                }
                Err(e) => format!("connection error: {e}"),
            };
            if attempt >= RETRY_WAITS.len() {
                return Err(format!("{retry_why} (after {} retries)", RETRY_WAITS.len()));
            }
            eprintln!("tinycoder{tag}: API call failed ({retry_why}); retrying in {} s", RETRY_WAITS[attempt]);
            tokio::time::sleep(Duration::from_secs(RETRY_WAITS[attempt])).await;
            attempt += 1;
        }
    }
}

/// What every agent of one program shares: the HTTP client (and its connection pool) and the
/// request prefix holding the model, max_tokens and the system prompt.
pub struct Shared {
    api: Result<Api, String>,
    /// `{"model":…,"max_tokens":…,"system":…,"messages":[`
    head: String,
}

impl Shared {
    pub fn new() -> Self {
        let system = system_prompt();
        let head = format!(
            "{{\"model\":{},\"max_tokens\":{MAX_TOKENS},\"system\":{},\"messages\":[",
            Value::String(MODEL.into()),
            Value::String(system)
        );
        Self { api: Api::new().map_err(|e| format!("{e:#}")), head }
    }
}

/// One agent to run.
pub struct Job<'a> {
    pub run_id: &'a str,
    pub task_dir: &'a Path,
    pub workspace: &'a Path,
    pub runs: &'a Path,
}

/// The finished agent: its final object, and that object serialized (one JSON object).
pub struct Outcome {
    pub fin: Final,
    pub json: String,
}

/// Run one agent to the end (SPEC.md section 4). Never panics outward: a panic inside the
/// agent ends it with status `error`. Writes the runs file, including its final line.
pub async fn run_agent(t0: Instant, job: Job<'_>, shared: &Shared, servers: Servers, tag: &str) -> Outcome {
    let file = match std::fs::create_dir_all(job.runs).and_then(|_| {
        OpenOptions::new().create(true).append(true).open(job.runs.join(format!("{}.jsonl", job.run_id)))
    }) {
        Ok(f) => Some(f),
        Err(e) => {
            eprintln!("tinycoder{tag}: cannot open runs file: {e}");
            None
        }
    };
    let mut log = RunLog { file, tag: tag.to_string() };

    let mut fin = Final {
        status: "error",
        reason: None,
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

    let mut clients: Vec<Client> = Vec::new();
    let res = CatchUnwind::new(agent_loop(&job, shared, servers, tag, &mut log, &mut fin, &mut clients)).await;
    match res {
        Ok(Ok(())) => {}
        Ok(Err(e)) => {
            fin.status = "error";
            fin.reason = Some(format!("{e:#}"));
        }
        Err(panic) => {
            eprintln!("tinycoder{tag}: agent crashed: {panic}");
            fin.status = "error";
            fin.reason = Some(format!("agent crashed: {panic}"));
        }
    }
    for c in clients {
        let _ = tokio::time::timeout(Duration::from_secs(5), c.cancel()).await;
    }

    fin.wall_ms = ms(t0.elapsed());
    let json = serde_json::to_string(&fin).unwrap_or_default();
    log.line(&format!("{{\"type\":\"final\",{}", &json[1..]));
    Outcome { fin, json }
}

/// `tinycoder run` (SPEC.md section 4): one agent, stdio servers, one JSON line on stdout.
pub async fn run(t0: Instant, task_dir: &Path, workspace: &Path, runs: &Path) -> i32 {
    let run_id = std::env::var("TINYCODER_RUN_ID").ok().filter(|s| !s.is_empty()).unwrap_or_else(|| {
        SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis()).unwrap_or(0).to_string()
    });
    let shared = Shared::new();
    let job = Job { run_id: &run_id, task_dir, workspace, runs };
    let out = run_agent(t0, job, &shared, Servers::Stdio, "").await;
    {
        let mut so = std::io::stdout().lock();
        let _ = writeln!(so, "{}", out.json);
        let _ = so.flush();
    }
    out.fin.exit_code()
}

fn text_blocks(content: &[Value]) -> String {
    content
        .iter()
        .filter(|b| b.get("type").and_then(Value::as_str) == Some("text"))
        .filter_map(|b| b.get("text").and_then(Value::as_str))
        .collect::<Vec<_>>()
        .join("\n")
}

fn usage(u: &Option<Value>, k: &str) -> u64 {
    u.as_ref().and_then(|u| u.get(k)).and_then(Value::as_u64).unwrap_or(0)
}

/// The parts of a Messages API response the loop needs. `content` stays as the exact bytes
/// the API sent, so the assistant turn goes back into `messages` unchanged.
#[derive(Deserialize)]
struct Reply<'a> {
    #[serde(borrow, default)]
    content: Option<&'a RawValue>,
    #[serde(default)]
    stop_reason: Option<Value>,
    #[serde(default)]
    usage: Option<Value>,
}

async fn agent_loop(
    job: &Job<'_>,
    shared: &Shared,
    servers: Servers,
    tag: &str,
    log: &mut RunLog,
    fin: &mut Final,
    clients: &mut Vec<Client>,
) -> anyhow::Result<()> {
    let task_md = std::fs::read_to_string(job.task_dir.join("TASK.md"))
        .map_err(|e| anyhow::anyhow!("cannot read {}: {e}", job.task_dir.join("TASK.md").display()))?;
    let task_json = job.task_dir.join("task.json");
    let api = shared.api.as_ref().map_err(|e| anyhow::anyhow!("HTTP client: {e}"))?;

    let (files, shell) = match servers {
        Servers::Stdio => {
            let ws = job.workspace.to_string_lossy().into_owned();
            tokio::join!(
                start_child_server(vec!["serve".into(), "files".into(), "--workspace".into(), ws.clone()]),
                start_child_server(vec![
                    "serve".into(),
                    "shell".into(),
                    "--workspace".into(),
                    ws,
                    "--task".into(),
                    task_json.to_string_lossy().into_owned(),
                ]),
            )
        }
        Servers::InProcess(limits) => tokio::join!(
            start_inproc_server("files", FilesServer::with_limits(job.workspace, limits)),
            start_inproc_server("shell", ShellServer::new(job.workspace, &task_json)),
        ),
    };
    let (files, shell) = match (files, shell) {
        (Ok(f), Ok(s)) => (f, s),
        (f, s) => {
            let mut err = None;
            for r in [f, s] {
                match r {
                    Ok(c) => clients.push(c),
                    Err(e) => err = err.or(Some(e)),
                }
            }
            return Err(err.unwrap());
        }
    };
    clients.push(files);
    clients.push(shell);

    // tools: files first, then shell, each in server order, fields unchanged.
    let mut tools: Vec<Value> = Vec::new();
    let mut owner: HashMap<String, usize> = HashMap::new();
    for (i, c) in clients.iter().enumerate() {
        let list = c.list_all_tools().await.map_err(|e| anyhow::anyhow!("tools/list failed: {e}"))?;
        for t in list {
            let mut o = Map::new();
            o.insert("name".into(), Value::String(t.name.to_string()));
            if let Some(d) = &t.description {
                o.insert("description".into(), Value::String(d.to_string()));
            }
            o.insert("input_schema".into(), Value::Object((*t.input_schema).clone()));
            owner.entry(t.name.to_string()).or_insert(i);
            tools.push(Value::Object(o));
        }
    }
    // The request is `head + messages + tail`. `messages` is kept as serialized JSON (one
    // compact text, messages joined by commas): no re-encoding of the history at every step,
    // and much less memory than a tree of JSON values.
    let tail = format!("],\"tools\":{},\"output_config\":{{\"effort\":\"medium\"}}}}", Value::Array(tools));
    let mut messages = String::new();
    messages.push_str(&json!({"role": "user", "content": task_md}).to_string());

    for step in 1..=MAX_STEPS {
        fin.steps = step;
        let mut body = Vec::with_capacity(shared.head.len() + messages.len() + tail.len());
        body.extend_from_slice(shared.head.as_bytes());
        body.extend_from_slice(messages.as_bytes());
        body.extend_from_slice(tail.as_bytes());
        let t = Instant::now();
        let resp = api.send(body, tag).await;
        let dt = t.elapsed();
        fin.llm_ms += ms(dt);
        let raw = match resp {
            Ok(r) => r,
            Err(why) => {
                fin.status = "error";
                fin.reason = Some(why);
                return Ok(());
            }
        };
        let reply: Reply = match serde_json::from_slice(&raw) {
            Ok(r) => r,
            Err(e) => {
                fin.status = "error";
                fin.reason = Some(format!("bad API response: {e}"));
                return Ok(());
            }
        };
        let stop_reason = reply.stop_reason.as_ref().and_then(Value::as_str).unwrap_or("").to_string();
        let (it, ot) = (usage(&reply.usage, "input_tokens"), usage(&reply.usage, "output_tokens"));
        fin.input_tokens += it;
        fin.output_tokens += ot;
        log.event(&LlmEvent {
            kind: "llm",
            step,
            duration_ms: ms(dt),
            input_tokens: it,
            output_tokens: ot,
            cache_read_input_tokens: usage(&reply.usage, "cache_read_input_tokens"),
            stop_reason: &stop_reason,
        });
        let raw_content = reply.content.map(RawValue::get).filter(|s| s.trim_start().starts_with('['));
        let content: Vec<Value> = match raw_content {
            Some(s) => serde_json::from_str(s).map_err(|e| anyhow::anyhow!("bad API response content: {e}"))?,
            None => Vec::new(),
        };
        fin.final_text = text_blocks(&content);
        messages.push_str(",{\"role\":\"assistant\",\"content\":");
        messages.push_str(raw_content.unwrap_or("[]"));
        messages.push('}');
        drop(reply);
        drop(raw);

        match stop_reason.as_str() {
            "tool_use" => {}
            "end_turn" => {
                fin.status = "done";
                fin.reason = None;
                return Ok(());
            }
            other => {
                fin.status = "error";
                fin.reason = Some(if other.is_empty() { "missing stop_reason".into() } else { other.into() });
                return Ok(());
            }
        }

        let mut results: Vec<Value> = Vec::new();
        for block in content {
            if block.get("type").and_then(Value::as_str) != Some("tool_use") {
                continue;
            }
            let id = block.get("id").and_then(Value::as_str).unwrap_or("").to_string();
            let name = block.get("name").and_then(Value::as_str).unwrap_or("").to_string();
            let input = match block {
                Value::Object(mut o) => o.remove("input").unwrap_or_else(|| json!({})),
                _ => json!({}),
            };
            let args_bytes = serde_json::to_string(&input).map(|s| s.len()).unwrap_or(0);
            let t = Instant::now();
            fin.tool_calls += 1;
            let (text, is_error, blocked) = match hook_check(&name, &input, &owner) {
                Some(why) => {
                    fin.blocked_calls += 1;
                    (format!("blocked by hook: {why}"), true, true)
                }
                None => {
                    let client = &clients[owner[&name]];
                    let mut params = CallToolRequestParams::new(name.clone());
                    if let Value::Object(o) = input {
                        params = params.with_arguments(o);
                    }
                    match client.call_tool(params).await {
                        Ok(r) => {
                            let text = r
                                .content
                                .iter()
                                .filter_map(|c| c.as_text().map(|t| t.text.as_str()))
                                .collect::<Vec<_>>()
                                .join("\n");
                            (text, r.is_error.unwrap_or(false), false)
                        }
                        Err(e) => (format!("error: {e}"), true, false),
                    }
                }
            };
            let dt = t.elapsed();
            fin.tool_ms += ms(dt);
            log.event(&ToolEvent {
                kind: "tool",
                step,
                tool: &name,
                blocked,
                ok: !is_error,
                duration_ms: ms(dt),
                args_bytes,
                result_bytes: text.len(),
            });
            let mut r = json!({"type": "tool_result", "tool_use_id": id, "content": text});
            if is_error {
                r["is_error"] = Value::Bool(true);
            }
            results.push(r);
        }
        if results.is_empty() {
            fin.status = "error";
            fin.reason = Some("stop_reason tool_use without tool_use blocks".into());
            return Ok(());
        }
        messages.push(',');
        messages.push_str(&json!({"role": "user", "content": results}).to_string());
    }
    fin.status = "step_limit";
    fin.reason = Some(format!("no end_turn after {MAX_STEPS} steps"));
    Ok(())
}
