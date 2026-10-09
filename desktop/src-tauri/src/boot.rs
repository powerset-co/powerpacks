//! Launch: the splash shows each step until the window can load the local Powerpacks page.
//!
//! Flow:
//!   install or refresh the bundled code in ~/powerpacks (source.rs)
//!   -> `uv sync` with the bundled uv (downloads Python and packages on first launch)
//!   -> `python -m packs.shared.web.server serve`, as this app's child (an earlier page on the
//!      port is stopped first)
//!   -> setup not finished: start `bin/onboard` and show /install; otherwise show the Agent.

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

use crate::children::{self, Children};
use crate::onboard::{self, Setup};
use crate::paths;
use crate::source::{self, Plan};

pub const HOST: &str = "127.0.0.1";
pub const PORT: u16 = 8765;
const BOOT_EVENT: &str = "boot://state";
/// The health identity every Powerpacks page answers with (server.py PRIMITIVE).
const PAGE_PRIMITIVE: &str = "reconcile_review_web";
const SERVER_LOG: &str = ".powerpacks/install/server.log";
const HEALTH_POLL: Duration = Duration::from_millis(200);
const HEALTH_TIMEOUT: Duration = Duration::from_secs(1);
const SERVER_TIMEOUT: Duration = Duration::from_secs(20);
const KEPT_LINES: usize = 40;
const HOME_PAGE: &str = "/agent";
const SETUP_PAGE: &str = "/install";

#[derive(Clone, Copy, Serialize, PartialEq, Eq, Debug)]
#[serde(rename_all = "lowercase")]
pub enum Phase {
    Starting,
    Ready,
    Failed,
}

/// What the splash shows; sent whole on every change.
#[derive(Clone, Serialize, Debug)]
pub struct BootState {
    pub phase: Phase,
    pub message: String,
    pub detail: String,
    pub lines: Vec<String>,
    pub root: Option<PathBuf>,
}

impl Default for BootState {
    fn default() -> Self {
        Self {
            phase: Phase::Starting,
            message: "Starting Powerpacks".into(),
            detail: String::new(),
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

    fn step(&self, app: &AppHandle, message: &str, detail: &str) {
        self.update(app, |state| {
            state.message = message.into();
            state.detail = detail.into();
            state.lines.clear();
        });
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

/// Start (or retry after a failure) on a worker thread.
pub fn launch(app: AppHandle) {
    thread::spawn(move || {
        let boot = app.state::<Arc<Boot>>().inner().clone();
        boot.update(&app, |state| *state = BootState::default());
        if let Err(message) = run(&app, &boot) {
            boot.update(&app, |state| {
                state.phase = Phase::Failed;
                state.message = message;
            });
        }
    });
}

fn run(app: &AppHandle, boot: &Boot) -> Result<(), String> {
    let root = paths::root()?;
    boot.update(app, |state| state.root = Some(root.clone()));
    let version = app
        .path()
        .resolve(source::VERSION_RESOURCE, BaseDirectory::Resource)
        .ok()
        .and_then(|path| std::fs::read_to_string(path).ok())
        .map(|text| text.trim().to_owned())
        .filter(|text| !text.is_empty())
        .unwrap_or_else(|| app.package_info().version.to_string());
    if let Plan::Install = source::plan(&root, &version)? {
        boot.step(app, "Installing Powerpacks", "");
        let archive = app
            .path()
            .resolve(source::ARCHIVE_RESOURCE, BaseDirectory::Resource)
            .map_err(|error| format!("The app is missing Powerpacks: {error}"))?;
        source::install(&archive, &root, &version)?;
    }
    source::ensure_env(&root)?;

    boot.step(
        app,
        "Setting up Python",
        "The first launch downloads about 200 MB. Later launches skip this.",
    );
    let uv = paths::bundled("uv")?;
    let mut sync = Command::new(uv);
    sync.args(["sync", "--frozen", "--no-dev", "--project"])
        .arg(&root)
        .current_dir(&root);
    stream(app, boot, sync, "Python setup")?;

    boot.step(app, "Opening Powerpacks", "");
    serve(app, &root)?;

    let setup = onboard::setup(&root);
    if setup == Setup::New {
        onboard::record_install(&root, &paths::project_python(&root))?;
    }
    if matches!(setup, Setup::New | Setup::Interrupted) {
        onboard::start(&root, onboard::Answer::default())?;
    }
    show_page(
        app,
        boot,
        if setup == Setup::Done {
            HOME_PAGE
        } else {
            SETUP_PAGE
        },
    )
}

/// The page's /healthz identity when a Powerpacks page holds the port.
fn page_health() -> Option<serde_json::Value> {
    let address = format!("{HOST}:{PORT}").parse().ok()?;
    let mut stream = TcpStream::connect_timeout(&address, HEALTH_TIMEOUT).ok()?;
    stream.set_read_timeout(Some(HEALTH_TIMEOUT)).ok()?;
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

/// Run the page server as this app's own child, replacing any page an earlier run left on the
/// port, so the window always talks to the code this app installed and nothing outlives it.
fn serve(app: &AppHandle, root: &Path) -> Result<(), String> {
    if let Some(stale) = page_health() {
        children::stop_pid(
            stale
                .get("pid")
                .and_then(serde_json::Value::as_u64)
                .unwrap_or(0) as u32,
        );
        let gone = Instant::now() + SERVER_TIMEOUT;
        while page_health().is_some() && Instant::now() < gone {
            thread::sleep(HEALTH_POLL);
        }
    }
    let log_path = root.join(SERVER_LOG);
    std::fs::create_dir_all(log_path.parent().expect("log dir"))
        .map_err(|error| error.to_string())?;
    let log = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(|error| error.to_string())?;
    let errors = log.try_clone().map_err(|error| error.to_string())?;
    let child = Command::new(paths::project_python(root))
        .args([
            "-m",
            "packs.shared.web.server",
            "serve",
            "--port",
            &PORT.to_string(),
        ])
        .current_dir(root)
        .stdin(Stdio::null())
        .stdout(log)
        .stderr(errors)
        .spawn()
        .map_err(|error| format!("Could not run Python in {}: {error}", root.display()))?;
    app.state::<Children>().adopt(child);
    let deadline = Instant::now() + SERVER_TIMEOUT;
    while Instant::now() < deadline {
        if page_health()
            .and_then(|identity| {
                identity
                    .get("repo_root")
                    .map(|value| value == &serde_json::json!(root))
            })
            .unwrap_or(false)
        {
            return Ok(());
        }
        thread::sleep(HEALTH_POLL);
    }
    Err(format!(
        "The Powerpacks page did not start. See {}.",
        log_path.display()
    ))
}

/// Run `command`, showing its output on the splash; fail with its last line.
fn stream(app: &AppHandle, boot: &Boot, mut command: Command, what: &str) -> Result<(), String> {
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("{what} could not start: {error}"))?;
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
    let threads: Vec<_> = readers
        .into_iter()
        .flatten()
        .map(|reader| {
            let (app, boot) = (app.clone(), app.state::<Arc<Boot>>().inner().clone());
            thread::spawn(move || {
                for line in BufReader::new(reader).lines().map_while(Result::ok) {
                    boot.line(&app, line);
                }
            })
        })
        .collect();
    let status = child.wait().map_err(|error| error.to_string())?;
    for reader in threads {
        let _ = reader.join();
    }
    if status.success() {
        return Ok(());
    }
    let last = boot.snapshot().lines.last().cloned().unwrap_or_default();
    Err(format!("{what} failed: {last}"))
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

/// The root the app runs Powerpacks from, once launch has resolved it.
pub fn require_root(boot: &Boot) -> Result<PathBuf, String> {
    boot.root()
        .filter(|root| Path::new(root).join("bin/onboard").is_file())
        .ok_or_else(|| "Powerpacks is not installed yet.".into())
}
