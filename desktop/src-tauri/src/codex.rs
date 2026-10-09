//! The Codex agent: one `codex app-server` child speaking JSON-RPC (JSONL, no "jsonrpc" field)
//! over stdio. Sign-in is Codex's own ChatGPT OAuth: `account/login/start` returns an auth URL
//! that opens in the system browser, and `account/login/completed` arrives when it lands.
//!
//! Requests the page may send are allowlisted in `PAGE_METHODS`. Server notifications go to the
//! page as `codex://notification`; server requests (approvals) as `codex://request`, answered
//! with `codex_respond`.

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::path::Path;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};
use tauri::{AppHandle, Emitter};
use tokio::sync::oneshot;

use crate::paths;

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
/// The only skills the in-app agent gets: people, company, contact and SQL search, which also
/// answer dossier lookups. Setup, imports, Deep Context and the index run from the app's pages.
const SKILL_ROOTS: [&str; 2] = ["packs/search/skills", "packs/contacts/skills"];
const DEVELOPER_INSTRUCTIONS: &str = "You are the assistant inside the Powerpacks desktop app. \
You search the user's network and answer questions about people, companies and dossiers with \
the Powerpacks search skills. Setup, sign-in, contact imports, Deep Context processing and the \
search index are run by the user from the app's own pages (Accounts, People, Searches); do not \
run those workflows yourself. If one is needed, say which page to open. Never ask the user to \
open a URL in a browser: the app already shows every Powerpacks page.";
const THREAD_LIST_LIMIT: u32 = 50;

type Reply = Result<Value, String>;
type Pending = Arc<Mutex<HashMap<i64, oneshot::Sender<Reply>>>>;

struct Connection {
    child: Child,
    stdin: Mutex<ChildStdin>,
    pending: Pending,
    next_id: AtomicI64,
    alive: Arc<AtomicBool>,
}

impl Connection {
    fn spawn(app: &AppHandle, binary: &Path, cwd: Option<&Path>) -> Result<Self, String> {
        let mut command = Command::new(binary);
        command
            .arg("app-server")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        if let Some(cwd) = cwd {
            command.current_dir(cwd);
        }
        let mut child = command
            .spawn()
            .map_err(|error| format!("Could not start Codex: {error}"))?;
        let stdin = child.stdin.take().ok_or("Codex has no stdin")?;
        let stdout = child.stdout.take().ok_or("Codex has no stdout")?;
        let pending: Pending = Arc::default();
        let alive = Arc::new(AtomicBool::new(true));
        let (reader_pending, reader_alive, app) = (pending.clone(), alive.clone(), app.clone());
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if let Ok(message) = serde_json::from_str::<Value>(&line) {
                    route(&app, &reader_pending, message);
                }
            }
            reader_alive.store(false, Ordering::SeqCst);
            for (_, waiter) in reader_pending.lock().expect("pending").drain() {
                let _ = waiter.send(Err("Codex stopped.".into()));
            }
            let _ = app.emit(EXIT_EVENT, ());
        });
        Ok(Self {
            child,
            stdin: Mutex::new(stdin),
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
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let (sender, receiver) = oneshot::channel();
        self.pending.lock().expect("pending").insert(id, sender);
        self.write(&json!({ "id": id, "method": method, "params": params }))?;
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
fn route(app: &AppHandle, pending: &Pending, message: Value) {
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
            let _ = waiter.send(reply);
        }
        (None, None) => {}
    }
}

/// The app's one Codex connection, started on first use and restarted if it exits.
#[derive(Default)]
pub struct Codex {
    connection: tokio::sync::Mutex<Option<Arc<Connection>>>,
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
        if let Some(cwd) = cwd {
            let roots: Vec<_> = SKILL_ROOTS.iter().map(|root| cwd.join(root)).collect();
            connection
                .request("skills/extraRoots/set", json!({ "extraRoots": roots }))
                .await?;
        }
        *slot = Some(connection.clone());
        Ok(connection)
    }

    /// Stop the Codex process, if one runs (its drop kills it).
    pub fn shutdown(&self) {
        if let Ok(mut slot) = self.connection.try_lock() {
            slot.take();
        }
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

    /// Start ChatGPT sign-in and return the login id; the caller opens `authUrl`.
    pub async fn login(&self, app: &AppHandle, cwd: Option<&Path>) -> Reply {
        self.call(
            app,
            cwd,
            "account/login/start",
            json!({ "type": "chatgpt" }),
        )
        .await
    }

    /// Writes stay in the Powerpacks folder; network is on, since every search calls Powerset.
    fn thread_settings(cwd: &Path) -> Value {
        json!({
            "cwd": cwd,
            "approvalPolicy": "on-request",
            "sandbox": "workspace-write",
            "config": { "sandbox_workspace_write": { "network_access": true } },
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

    /// This app's chats, newest first.
    pub async fn threads(&self, app: &AppHandle, cwd: &Path) -> Reply {
        let params = json!({ "cwd": cwd, "sourceKinds": ["appServer"], "limit": THREAD_LIST_LIMIT,
                             "sortKey": "updated_at", "archived": false });
        self.call(app, Some(cwd), "thread/list", params).await
    }
}
