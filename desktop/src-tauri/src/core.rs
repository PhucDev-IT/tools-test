//! CoreManager: owns the Python Agent Core sidecar and its JSON Lines channel.
//!
//! * Exactly one Core child per app session (start/stop serialised by `lifecycle`).
//! * Requests are matched to responses by `request_id`; every request has a timeout and
//!   pending requests fail immediately with CORE_EXITED if the Core dies.
//! * Unexpected exit => phase `crashed`, automatic restart (max 3 within 5 minutes).
//! * Stop = `system.shutdown` -> wait for exit -> kill only as a last resort.
//! * The Core also exits by itself when stdin closes or this process disappears
//!   (`--parent-pid`), so no orphan survives a GUI crash.

use std::collections::{HashMap, VecDeque};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::Serialize;
use serde_json::{json, Value};
use tauri::{AppHandle, Emitter};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;
use tokio::sync::{oneshot, Notify};

pub const SIDECAR: &str = "sarae-agent-core";
pub const EVENT_CHANNEL: &str = "core://event";
pub const STATE_CHANNEL: &str = "core://state";
const MAX_CRASH_RESTARTS: usize = 3;
const CRASH_WINDOW: Duration = Duration::from_secs(300);

#[derive(Clone, Serialize, Default, Debug)]
pub struct CoreState {
    /// stopped | starting | ready | stopping | crashed
    pub phase: String,
    pub pid: Option<u32>,
    pub generation: u64,
    pub version: Option<String>,
    pub last_error: Option<Value>,
    pub exit_code: Option<i32>,
    pub restarts: u32,
}

pub struct CoreManager {
    child: Mutex<Option<CommandChild>>,
    pending: Mutex<HashMap<String, oneshot::Sender<Value>>>,
    state: Mutex<CoreState>,
    stderr_tail: Mutex<VecDeque<String>>,
    crash_times: Mutex<Vec<Instant>>,
    counter: AtomicU64,
    generation: AtomicU64,
    exiting: AtomicBool,
    exited: Notify,
    lifecycle: tokio::sync::Mutex<()>,
}

fn error(code: &str, message: impl Into<String>) -> Value {
    json!({ "code": code, "message": message.into() })
}

fn valid_method(method: &str) -> bool {
    !method.is_empty()
        && method.len() <= 64
        && method.chars().all(|c| c.is_ascii_lowercase() || c == '.' || c == '_')
        && method.contains('.')
}

impl CoreManager {
    pub fn new() -> Self {
        Self {
            child: Mutex::new(None),
            pending: Mutex::new(HashMap::new()),
            state: Mutex::new(CoreState { phase: "stopped".into(), ..Default::default() }),
            stderr_tail: Mutex::new(VecDeque::with_capacity(200)),
            crash_times: Mutex::new(Vec::new()),
            counter: AtomicU64::new(0),
            generation: AtomicU64::new(0),
            exiting: AtomicBool::new(false),
            exited: Notify::new(),
            lifecycle: tokio::sync::Mutex::new(()),
        }
    }

    pub fn state(&self) -> CoreState {
        self.state.lock().unwrap().clone()
    }

    pub fn stderr_tail(&self) -> Vec<String> {
        self.stderr_tail.lock().unwrap().iter().cloned().collect()
    }

    pub fn is_running(&self) -> bool {
        self.child.lock().unwrap().is_some()
    }

    /// Returns true the first time it is called (app exit in progress: no more restarts).
    pub fn begin_exit(&self) -> bool {
        !self.exiting.swap(true, Ordering::SeqCst)
    }

    fn update_state(&self, app: &AppHandle, f: impl FnOnce(&mut CoreState)) {
        let snapshot = {
            let mut state = self.state.lock().unwrap();
            f(&mut state);
            state.clone()
        };
        let _ = app.emit(STATE_CHANNEL, snapshot);
    }

    pub async fn start(self: &Arc<Self>, app: &AppHandle) -> Result<CoreState, String> {
        let _guard = self.lifecycle.lock().await;
        self.spawn_locked(app)
    }

    fn spawn_locked(self: &Arc<Self>, app: &AppHandle) -> Result<CoreState, String> {
        if self.is_running() {
            return Ok(self.state());
        }
        if self.exiting.load(Ordering::SeqCst) {
            return Err("Ứng dụng đang thoát".into());
        }
        let parent_pid = std::process::id().to_string();
        let command = app
            .shell()
            .sidecar(SIDECAR)
            .map_err(|e| format!("Không tìm thấy Agent Core: {e}"))?
            .args(["--ipc", "--parent-pid", parent_pid.as_str()]);
        let (mut rx, child) = command.spawn().map_err(|e| format!("Không khởi chạy được Agent Core: {e}"))?;
        let generation = self.generation.fetch_add(1, Ordering::SeqCst) + 1;
        let pid = child.pid();
        *self.child.lock().unwrap() = Some(child);
        self.update_state(app, |s| {
            s.phase = "starting".into();
            s.pid = Some(pid);
            s.generation = generation;
            s.exit_code = None;
            s.last_error = None;
            s.version = None;
        });

        let manager = Arc::clone(self);
        let handle = app.clone();
        tauri::async_runtime::spawn(async move {
            let mut buffer: Vec<u8> = Vec::new();
            while let Some(event) = rx.recv().await {
                match event {
                    CommandEvent::Stdout(bytes) => {
                        // The shell plugin delivers line-sized chunks; re-split defensively.
                        buffer.extend_from_slice(&bytes);
                        if !buffer.ends_with(b"\n") && !looks_complete(&buffer) {
                            continue;
                        }
                        let data = std::mem::take(&mut buffer);
                        for line in data.split(|b| *b == b'\n') {
                            manager.handle_line(&handle, generation, line);
                        }
                    }
                    CommandEvent::Stderr(bytes) => {
                        let text = String::from_utf8_lossy(&bytes).trim_end().to_string();
                        if !text.is_empty() {
                            let mut tail = manager.stderr_tail.lock().unwrap();
                            if tail.len() >= 200 {
                                tail.pop_front();
                            }
                            tail.push_back(text);
                        }
                    }
                    CommandEvent::Error(message) => {
                        manager.update_state(&handle, |s| s.last_error = Some(error("CORE_IO_ERROR", message)));
                    }
                    CommandEvent::Terminated(payload) => {
                        manager.on_terminated(&handle, generation, payload.code);
                        break;
                    }
                    _ => {}
                }
            }
        });
        Ok(self.state())
    }

    fn handle_line(&self, app: &AppHandle, generation: u64, line: &[u8]) {
        let trimmed = line.trim_ascii();
        if trimmed.is_empty() {
            return;
        }
        let frame: Value = match serde_json::from_slice(trimmed) {
            Ok(value) => value,
            Err(_) => {
                // Not a protocol frame (should never happen: Core isolates stdout). Keep for diagnostics.
                let mut tail = self.stderr_tail.lock().unwrap();
                tail.push_back(format!("[stdout] {}", String::from_utf8_lossy(trimmed)));
                return;
            }
        };
        match frame.get("type").and_then(Value::as_str) {
            Some("response") => {
                if let Some(id) = frame.get("request_id").and_then(Value::as_str) {
                    if let Some(sender) = self.pending.lock().unwrap().remove(id) {
                        let _ = sender.send(frame);
                    }
                }
            }
            Some("event") => {
                let name = frame.get("event").and_then(Value::as_str).unwrap_or("");
                if name == "core.ready" && self.state().generation == generation {
                    let version = frame.pointer("/data/version").and_then(Value::as_str).map(str::to_string);
                    self.update_state(app, |s| {
                        s.phase = "ready".into();
                        s.version = version;
                    });
                } else if name == "core.error" {
                    let data = frame.get("data").cloned();
                    self.update_state(app, |s| s.last_error = data);
                }
                let _ = app.emit(EVENT_CHANNEL, frame);
            }
            _ => {}
        }
    }

    fn on_terminated(self: &Arc<Self>, app: &AppHandle, generation: u64, code: Option<i32>) {
        if self.state().generation != generation {
            return;
        }
        *self.child.lock().unwrap() = None;
        for (_, sender) in self.pending.lock().unwrap().drain() {
            let _ = sender.send(json!({ "ok": false, "error": error("CORE_EXITED", "Agent Core đã dừng") }));
        }
        let expected = self.state().phase == "stopping" || self.exiting.load(Ordering::SeqCst);
        self.update_state(app, |s| {
            s.phase = if expected { "stopped".into() } else { "crashed".into() };
            s.pid = None;
            s.exit_code = code;
            if !expected && s.last_error.is_none() {
                s.last_error = Some(error("CORE_CRASHED", format!("Agent Core thoát bất thường (mã {:?})", code)));
            }
        });
        self.exited.notify_waiters();
        if expected {
            return;
        }
        // Crash: restart automatically unless it keeps crashing (then the user decides).
        let allowed = {
            let mut times = self.crash_times.lock().unwrap();
            times.retain(|t| t.elapsed() < CRASH_WINDOW);
            times.push(Instant::now());
            times.len() <= MAX_CRASH_RESTARTS
        };
        let already_running = self
            .state()
            .last_error
            .as_ref()
            .and_then(|e| e.get("code"))
            .and_then(Value::as_str)
            == Some("CORE_ALREADY_RUNNING");
        if allowed && !already_running {
            let manager = Arc::clone(self);
            let handle = app.clone();
            tauri::async_runtime::spawn(async move {
                tokio::time::sleep(Duration::from_secs(2)).await;
                let _guard = manager.lifecycle.lock().await;
                if manager.state().phase == "crashed" && manager.spawn_locked(&handle).is_ok() {
                    manager.update_state(&handle, |s| s.restarts += 1);
                }
            });
        }
    }

    pub async fn request(&self, method: &str, params: Value, timeout: Duration) -> Result<Value, Value> {
        if !valid_method(method) {
            return Err(error("METHOD_NOT_FOUND", "Tên method không hợp lệ"));
        }
        if self.state().phase != "ready" {
            return Err(error("CORE_NOT_READY", "Agent Core chưa sẵn sàng"));
        }
        let id = format!("g{}-{}", self.state().generation, self.counter.fetch_add(1, Ordering::SeqCst) + 1);
        let frame = json!({ "v": 1, "type": "request", "request_id": id, "method": method, "params": params });
        let mut line = serde_json::to_vec(&frame).map_err(|e| error("INVALID_PARAMS", e.to_string()))?;
        line.push(b'\n');
        let (tx, rx) = oneshot::channel();
        self.pending.lock().unwrap().insert(id.clone(), tx);
        {
            let mut child = self.child.lock().unwrap();
            let Some(child) = child.as_mut() else {
                self.pending.lock().unwrap().remove(&id);
                return Err(error("CORE_NOT_READY", "Agent Core chưa chạy"));
            };
            if let Err(e) = child.write(&line) {
                self.pending.lock().unwrap().remove(&id);
                return Err(error("CORE_IO_ERROR", e.to_string()));
            }
        }
        match tokio::time::timeout(timeout, rx).await {
            Ok(Ok(frame)) => {
                if frame.get("ok").and_then(Value::as_bool) == Some(true) {
                    Ok(frame.get("result").cloned().unwrap_or(Value::Null))
                } else {
                    Err(frame.get("error").cloned().unwrap_or_else(|| error("UNKNOWN", "Lỗi không xác định")))
                }
            }
            Ok(Err(_)) => Err(error("CORE_EXITED", "Agent Core đã dừng")),
            Err(_) => {
                self.pending.lock().unwrap().remove(&id);
                Err(error("CORE_TIMEOUT", format!("{method} không phản hồi sau {} giây", timeout.as_secs())))
            }
        }
    }

    pub async fn stop(&self, app: &AppHandle) -> CoreState {
        let _guard = self.lifecycle.lock().await;
        if !self.is_running() {
            self.update_state(app, |s| s.phase = "stopped".into());
            return self.state();
        }
        let was_ready = self.state().phase == "ready";
        self.update_state(app, |s| s.phase = "stopping".into());
        if was_ready {
            // Core stops its Cloud worker, monitor and OBS session, then exits on its own.
            let frame = json!({ "v": 1, "type": "request", "request_id": "shutdown", "method": "system.shutdown", "params": {} });
            if let Some(child) = self.child.lock().unwrap().as_mut() {
                let mut line = serde_json::to_vec(&frame).unwrap_or_default();
                line.push(b'\n');
                let _ = child.write(&line);
            }
        }
        let wait = async {
            loop {
                let notified = self.exited.notified();
                if !self.is_running() {
                    break;
                }
                notified.await;
            }
        };
        if tokio::time::timeout(Duration::from_secs(20), wait).await.is_err() {
            if let Some(child) = self.child.lock().unwrap().take() {
                let _ = child.kill();
            }
            self.update_state(app, |s| {
                s.phase = "stopped".into();
                s.pid = None;
                s.last_error = Some(error("CORE_KILLED", "Agent Core không tự dừng trong 20 giây, đã buộc dừng"));
            });
        }
        self.state()
    }
}

/// A chunk that already parses as one JSON value is a whole frame even without '\n'.
fn looks_complete(buffer: &[u8]) -> bool {
    serde_json::from_slice::<Value>(buffer.trim_ascii()).is_ok()
}
