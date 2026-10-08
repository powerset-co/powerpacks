//! Launch: show the splash, then load the local Powerpacks page into the window.
//!
//! Flow:
//!   checkout with a project interpreter -> `python -m packs.shared.web.server start` (reuses a
//!     running page) -> navigate the window to it.
//!   otherwise -> run the bundled `bootstrap --harness codex --powerset`, which clones or reuses
//!     the checkout and starts the page; the window moves to `/install` as soon as the page answers
//!     and the install page shows the rest.
//! The page server outlives the app by design (packs/shared/web/server.py `start`).

use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::path::BaseDirectory;
use tauri::{AppHandle, Emitter, Manager};

use crate::paths;

pub const HOST: &str = "127.0.0.1";
pub const PORT: u16 = 8765;
/// The health identity every Powerpacks page answers with (server.py PRIMITIVE).
const PAGE_PRIMITIVE: &str = "reconcile_review_web";
const BOOT_EVENT: &str = "boot://state";
const BOOTSTRAP_RESOURCE: &str = "bootstrap";
const BOOTSTRAP_ARGS: [&str; 3] = ["--harness", "codex", "--powerset"];
const HEALTH_POLL: Duration = Duration::from_millis(400);
const HEALTH_TIMEOUT: Duration = Duration::from_secs(1);
const KEPT_LINES: usize = 40;

#[derive(Clone, Copy, Serialize, PartialEq, Eq, Debug)]
#[serde(rename_all = "lowercase")]
pub enum Phase {
    Starting,
    Installing,
    Ready,
    Failed,
}

/// What the splash shows; sent whole on every change.
#[derive(Clone, Serialize, Debug)]
pub struct BootState {
    pub phase: Phase,
    pub message: String,
    pub lines: Vec<String>,
    pub root: Option<PathBuf>,
}

impl Default for BootState {
    fn default() -> Self {
        Self {
            phase: Phase::Starting,
            message: "Starting Powerpacks".into(),
            lines: Vec::new(),
            root: None,
        }
    }
}

#[derive(Default)]
pub struct Boot {
    state: Mutex<BootState>,
}

impl Boot {
    pub fn snapshot(&self) -> BootState {
        self.state.lock().expect("boot state").clone()
    }

    pub fn root(&self) -> Option<PathBuf> {
        self.snapshot().root
    }

    fn update(&self, app: &AppHandle, change: impl FnOnce(&mut BootState)) {
        let state = {
            let mut state = self.state.lock().expect("boot state");
            change(&mut state);
            state.clone()
        };
        let _ = app.emit(BOOT_EVENT, state);
    }

    fn line(&self, app: &AppHandle, line: String) {
        self.update(app, |state| {
            state.lines.push(line);
            let excess = state.lines.len().saturating_sub(KEPT_LINES);
            state.lines.drain(..excess);
        });
    }
}

pub fn page_url(path: &str) -> String {
    format!("http://{HOST}:{PORT}{path}")
}

/// Start (or restart after a failure) on a worker thread.
pub fn launch(app: AppHandle) {
    thread::spawn(move || {
        let boot = app.state::<Arc<Boot>>().inner().clone();
        boot.update(&app, |state| *state = BootState::default());
        let root = paths::checkout();
        boot.update(&app, |state| state.root = root.clone());
        let ready = root
            .as_deref()
            .and_then(|root| paths::project_python(root).map(|python| (root, python)));
        let outcome = match ready {
            Some((root, python)) => start_page(&app, &boot, root, &python),
            None => install(&app, &boot),
        };
        if let Err(message) = outcome {
            boot.update(&app, |state| {
                state.phase = Phase::Failed;
                state.message = message;
            });
        }
    });
}

fn start_page(app: &AppHandle, boot: &Boot, root: &Path, python: &Path) -> Result<(), String> {
    boot.update(app, |state| state.message = "Opening your workspace".into());
    let output = Command::new(python)
        .args([
            "-m",
            "packs.shared.web.server",
            "start",
            "--port",
            &PORT.to_string(),
        ])
        .current_dir(root)
        .stdin(Stdio::null())
        .output()
        .map_err(|error| format!("Could not run Python in {}: {error}", root.display()))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        let reason = stderr
            .lines()
            .rev()
            .find(|line| !line.trim().is_empty())
            .unwrap_or("no error output");
        return Err(format!("The Powerpacks page did not start: {reason}"));
    }
    show_page(app, boot, "/")
}

fn install(app: &AppHandle, boot: &Boot) -> Result<(), String> {
    if !cfg!(target_os = "macos") {
        return Err("Installing Powerpacks needs macOS. Point POWERPACKS_REPO_ROOT at an installed checkout to use it here.".into());
    }
    let bootstrap = app
        .path()
        .resolve(BOOTSTRAP_RESOURCE, BaseDirectory::Resource)
        .map_err(|error| format!("The app is missing its installer: {error}"))?;
    boot.update(app, |state| {
        state.phase = Phase::Installing;
        state.message = "Installing Powerpacks".into();
    });
    let mut child = Command::new("bash")
        .arg(&bootstrap)
        .args(BOOTSTRAP_ARGS)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("Could not start the installer: {error}"))?;
    let readers = [
        child
            .stdout
            .take()
            .map(|out| Box::new(out) as Box<dyn Read + Send>),
        child
            .stderr
            .take()
            .map(|err| Box::new(err) as Box<dyn Read + Send>),
    ];
    for reader in readers.into_iter().flatten() {
        let (app, boot) = (app.clone(), app.state::<Arc<Boot>>().inner().clone());
        thread::spawn(move || {
            for line in BufReader::new(reader).lines().map_while(Result::ok) {
                boot.line(&app, line);
            }
        });
    }
    // The page comes up partway through bootstrap; show it as soon as it answers.
    loop {
        if page_health().is_some() {
            boot.update(app, |state| state.root = paths::checkout());
            return show_page(app, boot, "/install");
        }
        if let Some(status) = child.try_wait().map_err(|error| error.to_string())? {
            thread::sleep(HEALTH_POLL); // let the readers flush the outcome line
            let last = boot.snapshot().lines.last().cloned().unwrap_or_default();
            return Err(if last.is_empty() {
                format!("The installer stopped ({status}).")
            } else {
                last
            });
        }
        thread::sleep(HEALTH_POLL);
    }
}

fn show_page(app: &AppHandle, boot: &Boot, path: &str) -> Result<(), String> {
    let url = page_url(path)
        .parse()
        .map_err(|error| format!("Bad page URL: {error}"))?;
    let window = app
        .get_webview_window("main")
        .ok_or("The main window is gone.")?;
    window.navigate(url).map_err(|error| error.to_string())?;
    boot.update(app, |state| {
        state.phase = Phase::Ready;
        state.message = "Ready".into();
    });
    Ok(())
}

/// The page's /healthz identity when a Powerpacks page holds the port.
fn page_health() -> Option<serde_json::Value> {
    let deadline = Instant::now() + HEALTH_TIMEOUT;
    let mut stream =
        TcpStream::connect_timeout(&format!("{HOST}:{PORT}").parse().ok()?, HEALTH_TIMEOUT).ok()?;
    stream
        .set_read_timeout(Some(
            deadline
                .saturating_duration_since(Instant::now())
                .max(HEALTH_POLL),
        ))
        .ok()?;
    write!(
        stream,
        "GET /healthz HTTP/1.0\r\nHost: {HOST}:{PORT}\r\n\r\n"
    )
    .ok()?;
    let mut response = String::new();
    stream.read_to_string(&mut response).ok()?;
    let body = response.split_once("\r\n\r\n")?.1;
    let identity: serde_json::Value = serde_json::from_str(body).ok()?;
    (identity.get("primitive")?.as_str()? == PAGE_PRIMITIVE).then_some(identity)
}
