//! Where things live: the Powerpacks folder, the bundled binaries, and the PATH every child
//! process gets.
//!
//! A macOS app started from Finder gets a bare PATH, so the login shell's PATH is read once at
//! launch. The app's own folder goes first: it holds the bundled `uv`, `codex` and `rg`, which
//! Powerpacks calls by name (`uv run --project . ...`).

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

/// The app's own Powerpacks folder, beside the credentials in `~/.powerpacks`. A checkout at
/// `~/powerpacks` belongs to the command-line install and may be any version, so the app never
/// touches it; `POWERPACKS_REPO_ROOT` points the app at a checkout on purpose (development).
const DEFAULT_ROOT: &str = ".powerpacks/app";
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

/// The project's interpreter, where `uv sync` puts it on each platform.
pub fn project_python(root: &Path) -> PathBuf {
    if cfg!(windows) {
        root.join(".venv").join("Scripts").join("python.exe")
    } else {
        root.join(".venv").join("bin").join("python")
    }
}

fn executable_name(name: &str) -> String {
    if cfg!(windows) {
        format!("{name}.exe")
    } else {
        name.to_owned()
    }
}

/// The folder the app's executable and its bundled binaries sit in.
pub fn app_dir() -> Option<PathBuf> {
    std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(PathBuf::from))
}

/// A binary the app ships (uv, codex, rg): beside the executable, else the first on PATH. The
/// error names where it was expected, for a bundle that came out wrong.
pub fn bundled(name: &str) -> Result<PathBuf, String> {
    let file = executable_name(name);
    let beside = app_dir().map(|dir| dir.join(&file));
    if let Some(path) = beside.as_ref().filter(|path| path.is_file()) {
        return Ok(path.clone());
    }
    which(name).ok_or_else(|| {
        let expected = beside
            .map(|path| path.display().to_string())
            .unwrap_or_else(|| file.clone());
        format!("The app is missing {name} (expected at {expected}).")
    })
}

/// The first executable named `name` on PATH.
pub fn which(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    let file = executable_name(name);
    std::env::split_paths(&path)
        .map(|dir| dir.join(&file))
        .find(|candidate| candidate.is_file())
}

/// Bundled binaries first, then the login shell's PATH, then the usual install folders.
pub fn adopt_path() {
    let mut entries: Vec<PathBuf> = Vec::new();
    if let Some(app_dir) = app_dir() {
        entries.push(app_dir);
    }
    let inherited = login_shell_path()
        .or_else(|| std::env::var("PATH").ok())
        .unwrap_or_default();
    entries.extend(std::env::split_paths(&inherited));
    if let Some(home) = dirs::home_dir() {
        entries.push(home.join(".local/bin"));
    }
    if !cfg!(windows) {
        entries
            .extend(["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"].map(PathBuf::from));
    }
    let mut seen = std::collections::HashSet::new();
    entries.retain(|entry| seen.insert(entry.clone()));
    if let Ok(joined) = std::env::join_paths(entries) {
        std::env::set_var("PATH", joined);
    }
}

/// The login shell's PATH; a POSIX notion, so none on Windows.
fn login_shell_path() -> Option<String> {
    if cfg!(windows) {
        return None;
    }
    let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/zsh".into());
    let script = format!("printf '{PATH_MARKER}%s{PATH_MARKER}' \"$PATH\"");
    let output = Command::new(shell)
        .args(["-ilc", &script])
        .stdin(Stdio::null())
        .output()
        .ok()?;
    let text = String::from_utf8_lossy(&output.stdout);
    let path = text.split(PATH_MARKER).nth(1)?;
    (!path.is_empty()).then(|| path.to_string())
}
