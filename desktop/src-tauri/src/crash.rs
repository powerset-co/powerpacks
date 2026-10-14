//! When Powerpacks breaks, a Codex run looks into it (packs/shared/skills/crash-repair.md): it
//! repairs the local setup, changes code only when Powerpacks cannot start or a core page cannot
//! load, and ends with a report the app files with Powerset (send_feedback, category `app`).
//!
//! What counts as a crash: the page server not starting (boot.rs retries it once after the
//! repair), a page that throws (the web error boundary, through `report_crash`), and a panic in
//! the app (noted by the hook below, looked into at the next launch). Each distinct crash is looked
//! into once per version: its report under .powerpacks/desktop/crashes/ marks it done.

use std::collections::hash_map::DefaultHasher;
use std::fs;
use std::hash::{Hash, Hasher};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use crate::codex::{CODEX_HOME, CREDENTIALS_IN_FILE};
use crate::paths;

const DIR: &str = ".powerpacks/desktop/crashes";
/// The panic hook's note, looked into at the next launch.
const PANIC_NOTE: &str = "panic.txt";
const SERVER_LOG: &str = ".powerpacks/install/server.log";
const GUIDE: &str = "packs/shared/skills/crash-repair.md";
const SEND_FEEDBACK: &str = "packs/powerset/primitives/send_feedback/send_feedback.py";
const LOG_LINES: usize = 80;
const REPAIR_TIMEOUT: Duration = Duration::from_secs(15 * 60);
const POLL: Duration = Duration::from_millis(500);

pub struct Crash {
    /// `server`, `page` or `app`.
    pub kind: &'static str,
    /// The error's own line: what tells one crash from another.
    pub summary: String,
    pub detail: String,
}

impl Crash {
    /// The page server did not start: its log's last error line, and the log's tail.
    pub fn server(root: &Path) -> Self {
        let log = fs::read_to_string(root.join(SERVER_LOG)).unwrap_or_default();
        let lines: Vec<&str> = log.lines().collect();
        let summary = lines
            .iter()
            .rev()
            .find(|line| is_error(line))
            .map_or("The page server did not start.", |line| line)
            .to_owned();
        let detail = lines[lines.len().saturating_sub(LOG_LINES)..].join("\n");
        Self {
            kind: "server",
            summary,
            detail,
        }
    }
}

/// A Python traceback's last line: `SomeError: ...` or `SomeException: ...`.
fn is_error(line: &str) -> bool {
    let name = line.split(':').next().unwrap_or_default();
    !line.starts_with(char::is_whitespace)
        && line.contains(':')
        && (name.ends_with("Error") || name.ends_with("Exception"))
}

/// Look into `crash` with Codex, unless this version already did, and file its report. Blocks
/// until the run ends (at most 15 minutes); true when a run happened.
pub fn repair(root: &Path, version: &str, crash: &Crash) -> Result<bool, String> {
    let dir = root.join(DIR);
    fs::create_dir_all(&dir).map_err(|error| error.to_string())?;
    let mut hasher = DefaultHasher::new();
    (version, crash.kind, &crash.summary).hash(&mut hasher);
    let id = format!("{:016x}", hasher.finish());
    let report = dir.join(format!("{id}.md"));
    if report.exists() {
        return Ok(false);
    }
    let log_path = dir.join(format!("{id}.log"));
    let log = fs::File::create(&log_path).map_err(|error| error.to_string())?;
    let prompt = format!(
        "Powerpacks broke. Follow {GUIDE} in this folder.\n\nWhat broke: {}\nVersion: {version}\nError: {}\n\nDetails:\n{}\n",
        crash.kind, crash.summary, crash.detail
    );
    let home = root.join(CODEX_HOME);
    fs::create_dir_all(&home).map_err(|error| error.to_string())?;
    let mut child = Command::new(paths::bundled("codex")?)
        .args(["-c", CREDENTIALS_IN_FILE])
        .args(["-c", "sandbox_workspace_write.network_access=true"])
        .args(["exec", "--skip-git-repo-check", "--ephemeral"])
        .args(["--sandbox", "workspace-write", "--output-last-message"])
        .arg(&report)
        .arg(prompt)
        .current_dir(root)
        .env("CODEX_HOME", &home)
        .stdin(Stdio::null())
        .stdout(log.try_clone().map_err(|error| error.to_string())?)
        .stderr(log)
        .spawn()
        .map_err(|error| format!("Could not start Codex: {error}"))?;
    let deadline = Instant::now() + REPAIR_TIMEOUT;
    while child
        .try_wait()
        .map_err(|error| error.to_string())?
        .is_none()
    {
        if Instant::now() > deadline {
            let _ = child.kill();
            let _ = child.wait();
            break;
        }
        thread::sleep(POLL);
    }
    // Codex writes the report as it finishes; a run that stopped short leaves none.
    if !report.exists() {
        fs::write(
            &report,
            format!("The repair did not finish.\n\nError: {}\n", crash.summary),
        )
        .map_err(|error| error.to_string())?;
    }
    file(root, version, crash, &id, &report, &log_path);
    Ok(true)
}

/// Send the report to Powerset with the app's Python; what it answers goes into the run's log.
fn file(root: &Path, version: &str, crash: &Crash, id: &str, report: &Path, log_path: &Path) {
    let comment = fs::read_to_string(report).unwrap_or_default();
    let metadata = serde_json::json!({ "kind": crash.kind, "version": version, "crash": id });
    let answer = Command::new(paths::project_python(root))
        .arg(SEND_FEEDBACK)
        .args(["--category", "app", "--comment"])
        .arg(comment)
        .arg("--metadata")
        .arg(metadata.to_string())
        .current_dir(root)
        .stdin(Stdio::null())
        .output();
    if let Ok(mut log) = fs::OpenOptions::new().append(true).open(log_path) {
        let _ = match answer {
            Ok(output) => log
                .write_all(b"\n--- report sent ---\n")
                .and_then(|()| log.write_all(&output.stdout))
                .and_then(|()| log.write_all(&output.stderr)),
            Err(error) => writeln!(log, "\n--- report not sent: {error}"),
        };
    }
}

/// Note any panic for the next launch to look into: the app may not live to do it now.
pub fn note_panics(root: PathBuf) {
    let previous = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        let dir = root.join(DIR);
        let _ = fs::create_dir_all(&dir);
        let backtrace = std::backtrace::Backtrace::force_capture();
        let _ = fs::write(dir.join(PANIC_NOTE), format!("{info}\n\n{backtrace}"));
        previous(info);
    }));
}

/// The last run's panic, looked into in the background now that the app is up.
pub fn after_launch(root: &Path, version: &str) {
    let note = root.join(DIR).join(PANIC_NOTE);
    let Ok(text) = fs::read_to_string(&note) else {
        return;
    };
    let _ = fs::remove_file(&note);
    let crash = Crash {
        kind: "app",
        summary: text.lines().take(2).collect::<Vec<_>>().join(" "),
        detail: text,
    };
    let (root, version) = (root.to_path_buf(), version.to_owned());
    thread::spawn(move || {
        let _ = repair(&root, &version, &crash);
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_server_crash_is_named_by_its_tracebacks_last_line() {
        let root = std::env::temp_dir().join(format!("crash-test-{}", std::process::id()));
        let log = root.join(SERVER_LOG);
        fs::create_dir_all(log.parent().unwrap()).unwrap();
        fs::write(
            &log,
            "serving\nTraceback (most recent call last):\n  File \"server.py\", line 3, in serve\n    raise RuntimeError(\"x\")\nRuntimeError: injected crash test\n",
        )
        .unwrap();
        let crash = Crash::server(&root);
        assert_eq!(crash.summary, "RuntimeError: injected crash test");
        assert!(crash.detail.contains("line 3, in serve"));
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn an_empty_log_still_names_the_crash() {
        let root = std::env::temp_dir().join(format!("crash-empty-{}", std::process::id()));
        assert_eq!(
            Crash::server(&root).summary,
            "The page server did not start."
        );
    }
}
