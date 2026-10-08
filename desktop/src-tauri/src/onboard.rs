//! The setup workflow (`bin/onboard`: account, imports, Deep Context, index), run by the app
//! instead of an agent. The install page shows its progress from
//! `.powerpacks/install/manifest.json`; re-running `bin/onboard` resumes from its saved choices,
//! and `--approve-spend <step>` approves the spend it stopped for. Its own lock keeps one run.

use std::fs::{self, OpenOptions};
use std::path::Path;
use std::process::{Command, Stdio};

const MANIFEST: &str = ".powerpacks/install/manifest.json";
const LOG: &str = ".powerpacks/install/desktop-onboard.log";
const SPEND_STEPS: [&str; 4] = ["synthesize", "cluster", "enrich", "index"];
/// What bootstrap records after installing; the app did the same work before setup starts.
const INSTALLED_EVENTS: [&str; 2] = ["install.dependencies_ready", "install.skills_ready"];

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
    match (field("status"), field("step")) {
        ("completed", "ready") => Setup::Done,
        ("running" | "completed", _) => Setup::Interrupted,
        _ => Setup::Paused,
    }
}

/// Record the app's install on the setup page, as bootstrap does, before the first setup run.
pub fn record_install(root: &Path, python: &Path) -> Result<(), String> {
    let retry = root.join("bin/onboard");
    for event in INSTALLED_EVENTS {
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
            .arg(&retry)
            .current_dir(root)
            .stdout(Stdio::null())
            .status()
            .map_err(|error| error.to_string())?;
        if !status.success() {
            return Err(format!("Could not record setup progress ({event})."));
        }
    }
    Ok(())
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
pub fn start(root: &Path, answer: Answer) -> Result<(), String> {
    let mut steps = Vec::new();
    if let Some(url) = answer.linkedin_url {
        if !url.contains("linkedin.com/in/") {
            return Err("Paste your LinkedIn profile URL, like linkedin.com/in/your-name.".into());
        }
        let mut owner = Command::new(root.join("bin/deep-context-v2"));
        owner.args(["owner", "--linkedin-url", &url]);
        if let Some(email) = account_email(root) {
            owner.args(["--email", &email]);
        }
        steps.push(owner);
    }
    let mut onboard = Command::new(root.join("bin/onboard"));
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
