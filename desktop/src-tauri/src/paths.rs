//! Where things live: the Powerpacks folder, and the PATH every child process gets.
//!
//! A macOS app started from Finder gets a bare PATH, so the login shell's PATH is read once at
//! launch. The app's own folder goes first: it holds the bundled `uv`, `codex` and `rg`, which
//! Powerpacks calls by name (`uv run --project . ...`).

use std::path::PathBuf;
use std::process::Command;

/// Where bootstrap installs Powerpacks; the app uses the same folder, and the same override.
const DEFAULT_ROOT: &str = "powerpacks";
const ROOT_OVERRIDE: &str = "POWERPACKS_REPO_ROOT";
const PATH_MARKER: &str = "__POWERPACKS_PATH__";

pub fn root() -> Result<PathBuf, String> {
    if let Some(root) = std::env::var_os(ROOT_OVERRIDE) {
        return Ok(PathBuf::from(root));
    }
    dirs::home_dir()
        .map(|home| home.join(DEFAULT_ROOT))
        .ok_or_else(|| "No home folder.".into())
}

pub fn project_python(root: &std::path::Path) -> PathBuf {
    root.join(".venv/bin/python")
}

/// Bundled binaries first, then the login shell's PATH, then the usual install folders.
pub fn adopt_path() {
    let mut entries: Vec<PathBuf> = Vec::new();
    if let Some(app_dir) = std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(PathBuf::from))
    {
        entries.push(app_dir);
    }
    let inherited = login_shell_path()
        .or_else(|| std::env::var("PATH").ok())
        .unwrap_or_default();
    entries.extend(std::env::split_paths(&inherited));
    if let Some(home) = dirs::home_dir() {
        entries.push(home.join(".local/bin"));
    }
    entries.extend(["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"].map(PathBuf::from));
    let mut seen = std::collections::HashSet::new();
    entries.retain(|entry| seen.insert(entry.clone()));
    if let Ok(joined) = std::env::join_paths(entries) {
        std::env::set_var("PATH", joined);
    }
}

fn login_shell_path() -> Option<String> {
    let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/zsh".into());
    let script = format!("printf '{PATH_MARKER}%s{PATH_MARKER}' \"$PATH\"");
    let output = Command::new(shell)
        .args(["-ilc", &script])
        .stdin(std::process::Stdio::null())
        .output()
        .ok()?;
    let text = String::from_utf8_lossy(&output.stdout);
    let path = text.split(PATH_MARKER).nth(1)?;
    (!path.is_empty()).then(|| path.to_string())
}

/// The first executable named `name` on PATH (the bundled one, after `adopt_path`).
pub fn which(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    std::env::split_paths(&path)
        .map(|dir| dir.join(name))
        .find(|candidate| candidate.is_file())
}
