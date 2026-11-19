//! Small helpers shared by both MCP servers. All protocol work is done by rmcp.

use std::sync::Arc;

use rmcp::model::{
    CallToolResponse, CallToolResult, ContentBlock, Implementation, ServerCapabilities, ServerConfig, Tool,
};
use serde_json::{Map, Value};

/// Result of a tool: JSON object text on success, an error message (without the `error: ` prefix).
pub type ToolOut = Result<String, String>;

pub fn ser<T: serde::Serialize>(v: &T) -> ToolOut {
    serde_json::to_string(v).map_err(|e| format!("cannot encode result: {e}"))
}

pub fn tool(name: &'static str, description: &'static str, schema: Value) -> Tool {
    let obj = match schema {
        Value::Object(m) => m,
        _ => unreachable!(),
    };
    Tool::new(name, description, Arc::new(obj))
}

pub fn server_info(name: &str) -> ServerConfig {
    ServerConfig::new(ServerCapabilities::builder().enable_tools().build())
        .with_server_info(Implementation::new(name, env!("CARGO_PKG_VERSION")))
}

pub fn to_response(out: ToolOut) -> CallToolResponse {
    match out {
        Ok(text) => {
            // `isError` is optional and defaults to false; leaving it out keeps every reply
            // 16 bytes shorter.
            let mut r = CallToolResult::success(vec![ContentBlock::text(text)]);
            r.is_error = None;
            r.into()
        }
        Err(e) => CallToolResult::error(vec![ContentBlock::text(format!("error: {e}"))]).into(),
    }
}

pub fn req_str<'a>(args: &'a Map<String, Value>, key: &str) -> Result<&'a str, String> {
    match args.get(key) {
        Some(Value::String(s)) => Ok(s),
        Some(_) => Err(format!("{key} must be a string")),
        None => Err(format!("missing required argument: {key}")),
    }
}

pub fn opt_str<'a>(args: &'a Map<String, Value>, key: &str) -> Result<Option<&'a str>, String> {
    match args.get(key) {
        Some(Value::String(s)) => Ok(Some(s)),
        Some(Value::Null) | None => Ok(None),
        Some(_) => Err(format!("{key} must be a string")),
    }
}

pub fn opt_int(args: &Map<String, Value>, key: &str) -> Result<Option<i64>, String> {
    match args.get(key) {
        Some(Value::Null) | None => Ok(None),
        Some(Value::Number(n)) => {
            if let Some(i) = n.as_i64() {
                Ok(Some(i))
            } else if let Some(f) = n.as_f64().filter(|f| f.fract() == 0.0 && f.abs() < 9e15) {
                Ok(Some(f as i64))
            } else {
                Err(format!("{key} must be an integer"))
            }
        }
        Some(_) => Err(format!("{key} must be an integer")),
    }
}

/// A future that turns a panic while polling the inner future into `Err(message)`, so one
/// agent's (or one tool call's) crash ends only that agent (or call). Needs `panic = "unwind"`.
pub struct CatchUnwind<'a, T> {
    inner: std::pin::Pin<Box<dyn Future<Output = T> + Send + 'a>>,
}

impl<'a, T> CatchUnwind<'a, T> {
    pub fn new(f: impl Future<Output = T> + Send + 'a) -> Self {
        Self { inner: Box::pin(f) }
    }
}

impl<T> Future for CatchUnwind<'_, T> {
    type Output = Result<T, String>;
    fn poll(mut self: std::pin::Pin<&mut Self>, cx: &mut std::task::Context<'_>) -> std::task::Poll<Self::Output> {
        use std::task::Poll;
        let inner = self.inner.as_mut();
        match std::panic::catch_unwind(std::panic::AssertUnwindSafe(move || inner.poll(cx))) {
            Ok(Poll::Pending) => Poll::Pending,
            Ok(Poll::Ready(v)) => Poll::Ready(Ok(v)),
            Err(p) => Poll::Ready(Err(panic_message(&p))),
        }
    }
}

pub fn panic_message(p: &Box<dyn std::any::Any + Send>) -> String {
    if let Some(s) = p.downcast_ref::<&str>() {
        s.to_string()
    } else if let Some(s) = p.downcast_ref::<String>() {
        s.clone()
    } else {
        "panic".into()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn catch_unwind_turns_a_panic_into_an_error() {
        let rt = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
        let ok = rt.block_on(CatchUnwind::new(async { 7 }));
        assert_eq!(ok, Ok(7));
        let bad: Result<(), String> = rt.block_on(CatchUnwind::new(async {
            tokio::task::yield_now().await;
            panic!("boom {}", 1);
        }));
        assert_eq!(bad, Err("boom 1".to_string()));
    }
}
