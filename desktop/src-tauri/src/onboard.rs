//! The setup workflow (what `bin/onboard` wraps: account, imports, Deep Context, index), run by the app
//! instead of an agent. The install page shows its progress from
//! `.powerpacks/install/manifest.json`; re-running `bin/onboard` resumes from its saved choices,
//! Its own lock keeps one run.

use std::fs::{self, OpenOptions};
use std::path::Path;
use std::process::{Command, Stdio};

use crate::paths;
use crate::source;

const MANIFEST: &str = ".powerpacks/install/manifest.json";
/// Set from the debug menu: launch treats setup as done, so the other pages can be tried first.
const SKIP_MARKER: &str = ".powerpacks/desktop/skip-setup";
const LOG: &str = ".powerpacks/install/desktop-onboard.log";
/// Read by the Python workflow and page server (`POWERPACKS_DESKTOP`): no browsers, app-voiced words.
pub const DESKTOP_FLAG: &str = "POWERPACKS_DESKTOP";
/// The setup workflow and the owner-profile step, run with the project's Python (what
/// `bin/onboard` and `bin/deep-context-v2 owner` wrap) so no shell is needed.
const ONBOARD_MODULE: &str = "packs.powerset.primitives.install.onboard";
/// The onboarding's LinkedIn step: read the connections, import and index them, check search
/// (what `bin/linkedin-setup` wraps). It takes no sources or profile link.
const LINKEDIN_SETUP_MODULE: &str = "packs.powerset.primitives.install.linkedin_setup";
/// What bootstrap records after installing; the app did the same work before setup starts.
const INSTALLED_EVENTS: [&str; 2] = ["install.dependencies_ready", "install.skills_ready"];
/// The page shows a Start/Continue button for these (status_prose.py: action `resume`).
const READY_EVENT: &str = "setup.ready";

#[derive(Debug, PartialEq, Eq)]
pub enum Setup {
    /// Nothing has run yet.
    New,
    /// Started and not done: running, waiting on the user, or failed.
    Paused,
    Done,
}

pub fn setup(root: &Path) -> Setup {
    if root.join(SKIP_MARKER).exists() || source::imported(root) {
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
    let retry_command = root.join("bin/onboard").to_string_lossy().into_owned();
    for event in INSTALLED_EVENTS {
        write_event(root, python, event, &retry_command)?;
    }
    write_event(root, python, READY_EVENT, &retry_command)
}

fn write_event(root: &Path, python: &Path, event: &str, retry_command: &str) -> Result<(), String> {
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
        .arg(retry_command)
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
    /// The user's own LinkedIn profile, when setup cannot tell whose network it is.
    pub linkedin_url: Option<String>,
    /// The run's sources, when the screen decides them: LinkedIn alone (the onboarding's LinkedIn
    /// step) runs the LinkedIn setup; the Sources step runs every source through `bin/onboard`.
    /// Left out (Try again), the run the record names runs again, and `bin/onboard` keeps the
    /// saved choice.
    pub sources: Option<Vec<String>>,
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
/// the app exits. The next run starts from the person's click.
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

/// The command the record says reruns setup: `bin/linkedin-setup`, or `bin/onboard …`.
fn recorded_retry_command(root: &Path) -> String {
    let text = fs::read_to_string(root.join(MANIFEST)).unwrap_or_default();
    let manifest: serde_json::Value = serde_json::from_str(&text).unwrap_or_default();
    manifest
        .get("retry_command")
        .and_then(serde_json::Value::as_str)
        .unwrap_or_default()
        .to_string()
}

pub fn start(root: &Path, answer: Answer) -> Result<(), String> {
    let mut onboard = Command::new(paths::project_python(root));
    let linkedin_setup = match &answer.sources {
        Some(sources) => *sources == ["linkedin"],
        None => recorded_retry_command(root)
            .split_whitespace()
            .next()
            .is_some_and(|program| program.ends_with("bin/linkedin-setup")),
    };
    if linkedin_setup {
        onboard
            .args(["-m", LINKEDIN_SETUP_MODULE, "--root"])
            .arg(root);
    } else {
        onboard.args(["-m", ONBOARD_MODULE, "--root"]).arg(root);
        if let Some(url) = answer.linkedin_url {
            if !url.contains("linkedin.com/in/") {
                return Err(
                    "Paste your LinkedIn profile URL, like linkedin.com/in/your-name.".into(),
                );
            }
            // Setup's owner step looks the profile up once its credentials are in (pipeline.py).
            onboard.args(["--linkedin-url", url.trim()]);
        }
        for source in answer.sources.unwrap_or_default() {
            onboard.args(["--source", &source]);
        }
    }

    let log = root.join(LOG);
    fs::create_dir_all(log.parent().expect("log has a parent"))
        .map_err(|error| error.to_string())?;
    let output = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log)
        .map_err(|error| error.to_string())?;
    let errors = output.try_clone().map_err(|error| error.to_string())?;
    // The workflow's desktop behaviour: no browsers, hand sign-ins and waits back to the page.
    onboard
        .env(DESKTOP_FLAG, "1")
        .current_dir(root)
        .stdin(Stdio::null())
        .stdout(output)
        .stderr(errors);
    stop_running(root);
    let mut child = onboard
        .spawn()
        .map_err(|error| format!("Could not start setup: {error}"))?;
    std::thread::spawn(move || {
        let _ = child.wait();
    });
    Ok(())
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    #[test]
    fn imported_data_is_a_finished_setup_without_any_record() {
        let temporary =
            std::env::temp_dir().join(format!("powerpacks-setup-state-{}", std::process::id()));
        let own = temporary.join("own");
        fs::create_dir_all(own.join(".powerpacks")).unwrap();
        assert_eq!(setup(&own), Setup::New);
        let imported = temporary.join("imported");
        fs::create_dir_all(&imported).unwrap();
        std::os::unix::fs::symlink(own.join(".powerpacks"), imported.join(".powerpacks")).unwrap();
        assert_eq!(setup(&imported), Setup::Done);
        fs::remove_dir_all(temporary).unwrap();
    }

    #[test]
    fn a_profile_link_goes_to_setup_as_its_owner_choice() {
        use std::os::unix::fs::PermissionsExt;
        let root =
            std::env::temp_dir().join(format!("powerpacks-owner-link-{}", std::process::id()));
        let python = paths::project_python(&root);
        fs::create_dir_all(python.parent().unwrap()).unwrap();
        fs::write(&python, "#!/bin/sh\necho \"$@\" > setup-args\n").unwrap();
        fs::set_permissions(&python, fs::Permissions::from_mode(0o755)).unwrap();
        let answer = |url: &str| Answer {
            linkedin_url: Some(url.into()),
            sources: None,
        };
        assert!(start(&root, answer("https://example.com/jordan")).is_err());
        assert!(!root.join("setup-args").exists());
        start(&root, answer("https://linkedin.com/in/jordan-example")).unwrap();
        let args = (0..50)
            .find_map(|_| {
                std::thread::sleep(std::time::Duration::from_millis(50));
                fs::read_to_string(root.join("setup-args")).ok()
            })
            .expect("setup started");
        assert!(args.contains("--linkedin-url https://linkedin.com/in/jordan-example"));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn try_again_runs_the_recorded_run_and_sources_pick_the_run() {
        use std::os::unix::fs::PermissionsExt;
        let root =
            std::env::temp_dir().join(format!("powerpacks-linkedin-setup-{}", std::process::id()));
        let python = paths::project_python(&root);
        fs::create_dir_all(python.parent().unwrap()).unwrap();
        fs::create_dir_all(root.join(MANIFEST).parent().unwrap()).unwrap();
        fs::write(&python, "#!/bin/sh\necho \"$@\" > setup-args\n").unwrap();
        fs::set_permissions(&python, fs::Permissions::from_mode(0o755)).unwrap();
        let run = |retry_command: &str, sources: Option<&[&str]>| {
            let _ = fs::remove_file(root.join("setup-args"));
            let record = serde_json::json!({ "status": "failed", "retry_command": retry_command });
            fs::write(root.join(MANIFEST), record.to_string()).unwrap();
            let answer = Answer {
                linkedin_url: None,
                sources: sources
                    .map(|sources| sources.iter().map(|source| source.to_string()).collect()),
            };
            start(&root, answer).unwrap();
            (0..50)
                .find_map(|_| {
                    std::thread::sleep(std::time::Duration::from_millis(50));
                    fs::read_to_string(root.join("setup-args")).ok()
                })
                .expect("setup started")
        };
        let linkedin_setup = format!("-m {LINKEDIN_SETUP_MODULE} --root {}", root.display());
        let onboard = format!("-m {ONBOARD_MODULE} --root {}", root.display());
        // Try again (no sources) on a LinkedIn setup record runs the LinkedIn setup again.
        assert_eq!(run("bin/linkedin-setup", None).trim(), linkedin_setup);
        // Try again on an import record runs `bin/onboard` with its saved choice.
        let import = "/app/bin/onboard --source linkedin --source gmail --port 4321";
        assert_eq!(run(import, None).trim(), onboard);
        // The LinkedIn step's Continue runs the LinkedIn setup whatever the record says.
        assert_eq!(run(import, Some(&["linkedin"])).trim(), linkedin_setup);
        assert_eq!(
            run("bin/linkedin-setup", Some(&["linkedin", "gmail"])).trim(),
            format!("{onboard} --source linkedin --source gmail")
        );
        fs::remove_dir_all(root).unwrap();
    }
}
