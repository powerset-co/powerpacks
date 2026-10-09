//! The setup workflow (what `bin/onboard` wraps: account, imports, Deep Context, index), run by the app
//! instead of an agent. The install page shows its progress from
//! `.powerpacks/install/manifest.json`; re-running `bin/onboard` resumes from its saved choices,
//! and `--approve-spend <step>` approves the spend it stopped for. Its own lock keeps one run.

use std::fs::{self, OpenOptions};
use std::path::Path;
use std::process::{Command, Stdio};

use crate::paths;

const MANIFEST: &str = ".powerpacks/install/manifest.json";
/// Set from the debug menu: launch treats setup as done, so the other pages can be tried first.
const SKIP_MARKER: &str = ".powerpacks/desktop/skip-setup";
const LOG: &str = ".powerpacks/install/desktop-onboard.log";
/// The setup workflow and the owner-profile step, run with the project's Python (what
/// `bin/onboard` and `bin/deep-context-v2 owner` wrap) so no shell is needed.
const ONBOARD_MODULE: &str = "packs.powerset.primitives.install.onboard";
const OWNER_MODULE: &str = "packs.ingestion.primitives.deep_context_v2.owner";
const SPEND_STEPS: [&str; 4] = ["synthesize", "cluster", "enrich", "index"];
/// What bootstrap records after installing; the app did the same work before setup starts.
const INSTALLED_EVENTS: [&str; 2] = ["install.dependencies_ready", "install.skills_ready"];
/// The page shows a Start/Continue button for these (status_prose.py: action `resume`).
const READY_EVENT: &str = "setup.ready";
const PAUSED_EVENT: &str = "setup.paused";

#[derive(Debug, PartialEq, Eq)]
pub enum Setup {
    /// Nothing has run yet.
    New,
    /// A step is running or just finished; the process may have died with the last app run.
    Interrupted,
    /// Waiting on the user, or failed: the install page offers the next click.
    Paused,
    Done,
}

pub fn setup(root: &Path) -> Setup {
    if root.join(SKIP_MARKER).exists() {
        return Setup::Done;
    }
    let Ok(text) = fs::read_to_string(root.join(MANIFEST)) else {
        return Setup::New;
    };
    let manifest: serde_json::Value = serde_json::from_str(&text).unwrap_or_default();
    let field = |key: &str| {
        manifest
            .get(key)
            .and_then(serde_json::Value::as_str)
            .unwrap_or_default()
    };
    // A step can complete mid-setup (the install itself does); setup is done only at `ready`.
    if field("event") == READY_EVENT {
        return Setup::Paused;
    }
    match (field("status"), field("step")) {
        ("completed", "ready") => Setup::Done,
        ("running" | "completed", _) => Setup::Interrupted,
        _ => Setup::Paused,
    }
}

/// Skip setup on later launches, or stop skipping it.
pub fn set_skipped(root: &Path, skip: bool) -> Result<(), String> {
    let marker = root.join(SKIP_MARKER);
    let result = if skip {
        fs::create_dir_all(marker.parent().expect("marker has a parent"))
            .and_then(|()| fs::write(&marker, ""))
    } else if marker.exists() {
        fs::remove_file(&marker)
    } else {
        Ok(())
    };
    result.map_err(|error| error.to_string())
}

pub fn is_skipped(root: &Path) -> bool {
    root.join(SKIP_MARKER).exists()
}

/// Record the app's install on the setup page, as bootstrap does, then that setup waits for the
/// user's click.
pub fn record_install(root: &Path, python: &Path) -> Result<(), String> {
    for event in INSTALLED_EVENTS {
        write_event(root, python, event)?;
    }
    write_event(root, python, READY_EVENT)
}

/// A run that died with the last app run waits for the user's click too.
pub fn record_paused(root: &Path, python: &Path) -> Result<(), String> {
    write_event(root, python, PAUSED_EVENT)
}

fn write_event(root: &Path, python: &Path, event: &str) -> Result<(), String> {
    let status = Command::new(python)
        .args([
            "-m",
            "packs.powerset.primitives.install.status",
            "write",
            "--pid",
            "0",
            "--event",
            event,
        ])
        .arg("--root")
        .arg(root)
        .arg("--retry-command")
        .arg(root.join("bin/onboard"))
        .current_dir(root)
        .stdout(Stdio::null())
        .status()
        .map_err(|error| error.to_string())?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("Could not record setup progress ({event})."))
    }
}

/// What the user gave on the install page to get setup past a wait.
#[derive(Debug, Default, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Answer {
    /// A spend step the user approved.
    pub approve: Option<String>,
    /// The Gmail account to add.
    pub gmail_email: Option<String>,
    /// The user's own LinkedIn profile, when the import could not tell whose network it is.
    pub linkedin_url: Option<String>,
}

/// Resume `bin/onboard` in the background with the user's answer; it outlives the app like the
/// page server does.
/// Whether setup is mid-step right now: the manifest says running, and its process is alive.
pub fn is_running(root: &Path) -> bool {
    let Ok(text) = fs::read_to_string(root.join(MANIFEST)) else {
        return false;
    };
    let manifest: serde_json::Value = serde_json::from_str(&text).unwrap_or_default();
    let running = manifest.get("status").and_then(serde_json::Value::as_str) == Some("running");
    let pid = manifest
        .get("installer_pid")
        .and_then(serde_json::Value::as_u64)
        .unwrap_or(0);
    running && pid != 0 && crate::children::is_alive(pid as u32)
}

/// Stop the setup process recorded in the manifest, if any: before a new run (its lock would
/// refuse one, and a macOS permission granted meanwhile only reaches a new process), and when
/// the app exits. Setup resumes from its saved step on the next launch.
pub fn stop_running(root: &Path) {
    let Ok(text) = fs::read_to_string(root.join(MANIFEST)) else {
        return;
    };
    let manifest: serde_json::Value = serde_json::from_str(&text).unwrap_or_default();
    let pid = manifest
        .get("installer_pid")
        .and_then(serde_json::Value::as_u64)
        .unwrap_or(0);
    if pid == 0 || pid == std::process::id() as u64 {
        return;
    }
    #[cfg(unix)]
    {
        let _ = Command::new("kill")
            .args(["-TERM", &pid.to_string()])
            .status();
    }
}

pub fn start(root: &Path, answer: Answer) -> Result<(), String> {
    stop_running(root);
    let mut steps = Vec::new();
    if let Some(url) = answer.linkedin_url {
        if !url.contains("linkedin.com/in/") {
            return Err("Paste your LinkedIn profile URL, like linkedin.com/in/your-name.".into());
        }
        let mut owner = Command::new(paths::project_python(root));
        owner.args(["-m", OWNER_MODULE, "--linkedin-url", &url]);
        if let Some(email) = account_email(root) {
            owner.args(["--email", &email]);
        }
        steps.push(owner);
    }
    let mut onboard = Command::new(paths::project_python(root));
    onboard.args(["-m", ONBOARD_MODULE, "--root"]).arg(root);
    if let Some(step) = answer.approve {
        if !SPEND_STEPS.contains(&step.as_str()) {
            return Err(format!("{step} is not a step that needs approval."));
        }
        onboard.args(["--approve-spend", &step]);
    }
    if let Some(email) = answer.gmail_email {
        if !email.contains('@') {
            return Err("Enter a Gmail address.".into());
        }
        onboard.args(["--gmail-email", email.trim()]);
    }
    steps.push(onboard);

    let log = root.join(LOG);
    fs::create_dir_all(log.parent().expect("log has a parent"))
        .map_err(|error| error.to_string())?;
    let mut children = Vec::new();
    for mut step in steps {
        let output = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log)
            .map_err(|error| error.to_string())?;
        let errors = output.try_clone().map_err(|error| error.to_string())?;
        step.current_dir(root)
            .stdin(Stdio::null())
            .stdout(output)
            .stderr(errors);
        children.push(step);
    }
    // In order, on a worker thread: the owner profile, if any, then setup.
    std::thread::spawn(move || {
        for mut step in children {
            let ok = step.status().is_ok_and(|status| status.success());
            if !ok {
                break;
            }
        }
    });
    Ok(())
}

fn account_email(root: &Path) -> Option<String> {
    let text = fs::read_to_string(root.join(MANIFEST)).ok()?;
    let manifest: serde_json::Value = serde_json::from_str(&text).ok()?;
    manifest.get("account_email")?.as_str().map(str::to_owned)
}

/// Whether this app may read Messages (Full Disk Access). Each check runs in a fresh child
/// process: macOS answers a process once, so only a new one sees a permission granted since.
pub fn messages_readable(root: &Path) -> bool {
    const PROBE: &str = "import sqlite3, sys\nfrom pathlib import Path\ntry:\n    db = Path.home() / 'Library' / 'Messages' / 'chat.db'\n    sys.exit(0 if db.is_file() and sqlite3.connect(f'file:{db}?mode=ro', uri=True).execute('select count(*) from sqlite_master').fetchone() else 1)\nexcept Exception:\n    sys.exit(1)\n";
    Command::new(paths::project_python(root))
        .args(["-c", PROBE])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
}
