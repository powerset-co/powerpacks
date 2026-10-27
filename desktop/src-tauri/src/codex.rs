//! The Codex agent: one `codex app-server` child speaking JSON-RPC (JSONL, no "jsonrpc" field)
//! over stdio. Sign-in is Codex's own ChatGPT browser flow: `account/login/start` returns the
//! sign-in URL; Codex listens on localhost for ChatGPT's redirect and `account/login/completed`
//! arrives when it lands. Unlike device-code sign-in, no account setting has to allow it.
//!
//! Requests the page may send are allowlisted in `PAGE_METHODS`. Server notifications go to the
//! page as `codex://notification`; server requests (approvals) as `codex://request`, answered
//! with `codex_respond`.

use std::collections::HashMap;
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};
use tauri::{AppHandle, Emitter};
use tokio::sync::oneshot;

use crate::children;
use crate::paths;

mod questions;
use questions::QuestionEvents;

/// Where this app keeps Codex's sign-in, config and chat history, under the data folder.
pub const CODEX_HOME: &str = ".powerpacks/desktop/codex";
pub const CREDENTIALS_IN_FILE: &str = "cli_auth_credentials_store=file";
/// How this app, and only this app, starts Codex: what to stop from an earlier run.
const APP_SERVER_PATTERN: &str = "codex -c cli_auth_credentials_store=file app-server";
const CLIENT_NAME: &str = "powerpacks_desktop";
const CLIENT_TITLE: &str = "Powerpacks";
const REQUEST_TIMEOUT: Duration = Duration::from_secs(120);
const NOTIFICATION_EVENT: &str = "codex://notification";
const REQUEST_EVENT: &str = "codex://request";
const EXIT_EVENT: &str = "codex://exit";
/// The app-server methods the page may call directly; threads start and open through
/// `start_thread` and `open_thread`, which set the folder, sandbox and instructions.
pub const PAGE_METHODS: &[&str] = &[
    "account/read",
    "account/login/cancel",
    "account/logout",
    "turn/start",
    "turn/interrupt",
    "thread/archive",
];
const DEVELOPER_INSTRUCTIONS: &str = "You are the assistant inside the Powerpacks desktop app. \
You search the user's network, answer questions about people, companies and dossiers, and run \
the Powerpacks workflows the user asks for (contact imports, Deep Context processing, logbooks, \
the search index) with the Powerpacks skills. Powerpacks setup and the Powerset sign-in run from \
the app's own pages; if one is needed, say which page to open. Never ask the user to open a URL \
in a browser: the app already shows every Powerpacks page. After a search, say how many were \
found and stop: the results pane beside the chat shows the ranked list, so never print a table \
of results in the chat.";
const THREAD_LIST_LIMIT: u32 = 50;

type Reply = Result<Value, String>;
struct Waiter {
    reply: oneshot::Sender<Reply>,
    question_thread: bool,
}
type Pending = Arc<Mutex<HashMap<i64, Waiter>>>;

struct Connection {
    child: Child,
    stdin: Arc<Mutex<ChildStdin>>,
    questions: Arc<QuestionEvents>,
    pending: Pending,
    next_id: AtomicI64,
    alive: Arc<AtomicBool>,
}

impl Connection {
    fn spawn(app: &AppHandle, binary: &Path, cwd: Option<&Path>) -> Result<Self, String> {
        let mut command = Command::new(binary);
        command
            .args(["-c", CREDENTIALS_IN_FILE, "app-server"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        if let Some(cwd) = cwd {
            // The app's own Codex home: its sign-in and chats, apart from any `codex` on the
            // machine (whose keychain entry would prompt for every build of this app).
            // Codex refuses a home that does not exist.
            let home = cwd.join(CODEX_HOME);
            std::fs::create_dir_all(&home)
                .map_err(|error| format!("Could not create {}: {error}", home.display()))?;
            command.current_dir(cwd).env("CODEX_HOME", home);
        }
        // An app-server from a run that did not exit cleanly still holds the chats' writer locks.
        children::stop_matching(APP_SERVER_PATTERN);
        let mut child = command
            .spawn()
            .map_err(|error| format!("Could not start Codex: {error}"))?;
        let stdin = Arc::new(Mutex::new(child.stdin.take().ok_or("Codex has no stdin")?));
        let questions = Arc::new(QuestionEvents::default());
        let stdout = child.stdout.take().ok_or("Codex has no stdout")?;
        let pending: Pending = Arc::default();
        let alive = Arc::new(AtomicBool::new(true));
        let (reader_pending, reader_alive, app) = (pending.clone(), alive.clone(), app.clone());
        let (reader_questions, reader_stdin) = (questions.clone(), stdin.clone());
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if let Ok(message) = serde_json::from_str::<Value>(&line) {
                    route(
                        &app,
                        &reader_pending,
                        &reader_questions,
                        &reader_stdin,
                        message,
                    );
                }
            }
            reader_alive.store(false, Ordering::SeqCst);
            for (_, waiter) in reader_pending.lock().expect("pending").drain() {
                let _ = waiter.reply.send(Err("Codex stopped.".into()));
            }
            reader_questions.stopped();
            let _ = app.emit(EXIT_EVENT, ());
        });
        Ok(Self {
            child,
            stdin,
            questions,
            pending,
            next_id: AtomicI64::new(1),
            alive,
        })
    }

    fn write(&self, message: &Value) -> Result<(), String> {
        let mut stdin = self.stdin.lock().expect("codex stdin");
        writeln!(stdin, "{message}")
            .and_then(|()| stdin.flush())
            .map_err(|error| format!("Codex stopped: {error}"))
    }

    async fn request(&self, method: &str, params: Value) -> Reply {
        self.request_inner(method, params, false).await
    }

    async fn request_inner(&self, method: &str, params: Value, question_thread: bool) -> Reply {
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let (sender, receiver) = oneshot::channel();
        self.pending.lock().expect("pending").insert(
            id,
            Waiter {
                reply: sender,
                question_thread,
            },
        );
        if let Err(error) = self.write(&json!({ "id": id, "method": method, "params": params })) {
            self.pending.lock().expect("pending").remove(&id);
            return Err(error);
        }
        match tokio::time::timeout(REQUEST_TIMEOUT, receiver).await {
            Ok(Ok(reply)) => reply,
            Ok(Err(_)) => Err("Codex stopped.".into()),
            Err(_) => {
                self.pending.lock().expect("pending").remove(&id);
                Err(format!("Codex did not answer {method}."))
            }
        }
    }
}

impl Drop for Connection {
    fn drop(&mut self) {
        let _ = self.child.kill();
    }
}

/// A response settles its request; anything with a method goes to the page.
fn route(
    app: &AppHandle,
    pending: &Pending,
    questions: &QuestionEvents,
    stdin: &Mutex<ChildStdin>,
    message: Value,
) {
    if questions.route(&message, |reply| {
        let mut stdin = stdin.lock().expect("codex stdin");
        let _ = writeln!(stdin, "{reply}").and_then(|()| stdin.flush());
    }) {
        return;
    }
    let method = message
        .get("method")
        .and_then(Value::as_str)
        .map(str::to_owned);
    let id = message.get("id").cloned();
    match (method, id) {
        (Some(method), Some(id)) => {
            let params = message.get("params").cloned().unwrap_or(Value::Null);
            let _ = app.emit(
                REQUEST_EVENT,
                json!({ "id": id, "method": method, "params": params }),
            );
        }
        (Some(method), None) => {
            let params = message.get("params").cloned().unwrap_or(Value::Null);
            let _ = app.emit(
                NOTIFICATION_EVENT,
                json!({ "method": method, "params": params }),
            );
        }
        (None, Some(id)) => {
            let Some(waiter) = id
                .as_i64()
                .and_then(|id| pending.lock().expect("pending").remove(&id))
            else {
                return;
            };
            let reply = match message.get("error") {
                Some(error) => Err(error
                    .get("message")
                    .and_then(Value::as_str)
                    .unwrap_or("Codex error")
                    .to_owned()),
                None => Ok(message.get("result").cloned().unwrap_or(Value::Null)),
            };
            if waiter.question_thread {
                if let Ok(reply) = &reply {
                    if let Some(thread_id) = reply.pointer("/thread/id").and_then(Value::as_str) {
                        questions.register(thread_id);
                    }
                }
            }
            let _ = waiter.reply.send(reply);
        }
        (None, None) => {}
    }
}

/// The app's one Codex connection, started on first use and restarted if it exits.
#[derive(Default)]
pub struct Codex {
    connection: tokio::sync::Mutex<Option<Arc<Connection>>>,
    answering: Mutex<std::collections::HashSet<String>>,
}

impl Codex {
    async fn connect(
        &self,
        app: &AppHandle,
        cwd: Option<&Path>,
    ) -> Result<Arc<Connection>, String> {
        let mut slot = self.connection.lock().await;
        if let Some(live) = slot
            .as_ref()
            .filter(|live| live.alive.load(Ordering::SeqCst))
        {
            return Ok(live.clone());
        }
        let binary = paths::bundled("codex")?;
        let connection = Arc::new(Connection::spawn(app, &binary, cwd)?);
        let client = json!({ "name": CLIENT_NAME, "title": CLIENT_TITLE, "version": app.package_info().version.to_string() });
        connection
            .request(
                "initialize",
                json!({ "clientInfo": client, "capabilities": null }),
            )
            .await?;
        connection.write(&json!({ "method": "initialized" }))?;
        *slot = Some(connection.clone());
        Ok(connection)
    }

    /// Stop the Codex process, if one runs. A request in flight holds the connection, so the
    /// process is stopped by its command line rather than through the lock.
    pub fn shutdown(&self) {
        if let Ok(mut slot) = self.connection.try_lock() {
            slot.take();
        }
        children::stop_matching(APP_SERVER_PATTERN);
    }

    pub async fn call(
        &self,
        app: &AppHandle,
        cwd: Option<&Path>,
        method: &str,
        params: Value,
    ) -> Reply {
        self.connect(app, cwd).await?.request(method, params).await
    }

    pub async fn respond(&self, app: &AppHandle, id: Value, result: Value) -> Result<(), String> {
        self.connect(app, None)
            .await?
            .write(&json!({ "id": id, "result": result }))
    }

    /// The signed-in account, or `installed: false` when no Codex CLI is on this machine.
    pub async fn status(&self, app: &AppHandle, cwd: Option<&Path>) -> Reply {
        let Ok(binary) = paths::bundled("codex") else {
            return Ok(json!({ "installed": false }));
        };
        let account = self.call(app, cwd, "account/read", json!({})).await?;
        Ok(
            json!({ "installed": true, "binary": binary, "account": account.get("account"),
                   "requiresOpenaiAuth": account.get("requiresOpenaiAuth") }),
        )
    }

    /// Start ChatGPT sign-in: `{loginId, authUrl}`. The page opens the URL in the system
    /// browser; `account/login/completed` ends it.
    pub async fn login(&self, app: &AppHandle, cwd: Option<&Path>) -> Reply {
        self.call(
            app,
            cwd,
            "account/login/start",
            json!({ "type": "chatgpt" }),
        )
        .await
    }

    /// Writes stay in the Powerpacks folder plus uv's cache (every `bin/*` command runs through
    /// `uv run`, which dies without it); network is on, since every search calls Powerset.
    fn thread_settings(cwd: &Path) -> Value {
        let uv_cache = dirs::home_dir().unwrap_or_default().join(".cache/uv");
        json!({
            "cwd": cwd,
            "approvalPolicy": "on-request",
            "sandbox": "workspace-write",
            "config": { "sandbox_workspace_write": { "network_access": true, "writable_roots": [uv_cache] } },
            "developerInstructions": DEVELOPER_INSTRUCTIONS,
        })
    }

    pub async fn start_thread(&self, app: &AppHandle, cwd: &Path) -> Reply {
        self.call(app, Some(cwd), "thread/start", Self::thread_settings(cwd))
            .await
    }

    /// Resume a past chat with its turns, so the page can show its history and continue it.
    pub async fn open_thread(&self, app: &AppHandle, cwd: &Path, thread_id: &str) -> Reply {
        let mut params = Self::thread_settings(cwd);
        params["threadId"] = json!(thread_id);
        self.call(app, Some(cwd), "thread/resume", params).await
    }

    /// This app's chats, newest first. Codex's `updatedAt` moves whenever a chat is opened (a resume
    /// appends a settings line to its file), so each chat also carries `lastMessageAt`: the time of
    /// its last message, which the page sorts by.
    pub async fn threads(&self, app: &AppHandle, cwd: &Path) -> Reply {
        // No source filter: Codex records app-server chats as `vscode`; the folder already scopes them.
        let params = json!({ "cwd": cwd, "limit": THREAD_LIST_LIMIT,
                             "sortKey": "updated_at", "archived": false });
        let mut reply = self.call(app, Some(cwd), "thread/list", params).await?;
        let sessions = cwd.join(CODEX_HOME).join("sessions");
        if let Some(threads) = reply.get_mut("data").and_then(Value::as_array_mut) {
            for thread in threads {
                let Some(id) = thread.get("id").and_then(Value::as_str).map(str::to_owned) else {
                    continue;
                };
                if let Some(at) =
                    rollout_file(&sessions, &id).and_then(|file| last_message_at(&file))
                {
                    thread["lastMessageAt"] = json!(at);
                }
            }
        }
        Ok(reply)
    }
}

/// The chat's rollout file under `sessions/YYYY/MM/DD/`: the one whose name ends with its id.
fn rollout_file(sessions: &Path, thread_id: &str) -> Option<PathBuf> {
    let suffix = format!("-{thread_id}.jsonl");
    let mut pending = vec![sessions.to_path_buf()];
    while let Some(dir) = pending.pop() {
        for entry in fs::read_dir(&dir).ok()?.flatten() {
            let path = entry.path();
            if path.is_dir() {
                pending.push(path);
            } else if path
                .file_name()
                .is_some_and(|name| name.to_string_lossy().ends_with(&suffix))
            {
                return Some(path);
            }
        }
    }
    None
}

/// The timestamp of the file's last message (a `response_item` of type `message`, the user's or
/// the agent's), as Codex wrote it (RFC 3339).
fn last_message_at(file: &Path) -> Option<String> {
    let reader = BufReader::new(fs::File::open(file).ok()?);
    let mut last: Option<String> = None;
    for line in reader.lines().map_while(Result::ok) {
        if !line.contains("\"response_item\"") {
            continue;
        }
        let Ok(row) = serde_json::from_str::<Value>(&line) else {
            continue;
        };
        let is_message = row.get("type").and_then(Value::as_str) == Some("response_item")
            && row.pointer("/payload/type").and_then(Value::as_str) == Some("message");
        if is_message {
            if let Some(at) = row.get("timestamp").and_then(Value::as_str) {
                last = Some(at.to_owned());
            }
        }
    }
    last
}

#[cfg(test)]
mod rollout_tests {
    use super::*;

    #[test]
    fn the_last_message_wins_over_later_settings_lines() {
        let root =
            std::env::temp_dir().join(format!("powerpacks-rollout-test-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let day = root.join("2026").join("10").join("16");
        fs::create_dir_all(&day).expect("day");
        let file = day.join("rollout-2026-10-16T02-02-52-abc123.jsonl");
        fs::write(
            &file,
            concat!(
                "{\"timestamp\":\"2026-10-16T09:02:52Z\",\"type\":\"session_meta\",\"payload\":{}}\n",
                "{\"timestamp\":\"2026-10-16T09:05:25Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"assistant\"}}\n",
                "{\"timestamp\":\"2026-10-17T07:34:08Z\",\"type\":\"event_msg\",\"payload\":{\"type\":\"thread_settings_applied\"}}\n",
            ),
        )
        .expect("write");
        let found = rollout_file(&root, "abc123").expect("file");
        assert_eq!(found, file);
        assert_eq!(
            last_message_at(&found).as_deref(),
            Some("2026-10-16T09:05:25Z")
        );
        assert!(rollout_file(&root, "nope").is_none());
        let _ = fs::remove_dir_all(&root);
    }
}
