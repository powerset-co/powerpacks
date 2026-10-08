//! Where things live on this machine: the Powerpacks checkout and the binaries the app runs.
//!
//! A macOS app started from Finder gets a bare PATH, so the user's login-shell PATH is read
//! once at launch and exported to every child (the page server, bootstrap, Codex).

use std::path::{Path, PathBuf};
use std::process::Command;

/// Where bootstrap clones a fresh install, and the other checkout it reuses (bin/bootstrap).
const DEFAULT_CHECKOUT: &str = "powerpacks";
const WORKSPACE_CHECKOUT: &str = "workspace/powerpacks";
/// bootstrap's override for the checkout it reuses; the app honours the same variable.
const ROOT_OVERRIDE: &str = "POWERPACKS_REPO_ROOT";
const PATH_MARKER: &str = "__POWERPACKS_PATH__";

/// The checkout bootstrap would reuse: $POWERPACKS_REPO_ROOT, ~/powerpacks, ~/workspace/powerpacks.
pub fn checkout() -> Option<PathBuf> {
    let home = dirs::home_dir()?;
    let candidates = [
        std::env::var_os(ROOT_OVERRIDE).map(PathBuf::from),
        Some(home.join(DEFAULT_CHECKOUT)),
        Some(home.join(WORKSPACE_CHECKOUT)),
    ];
    candidates
        .into_iter()
        .flatten()
        .find(|root| is_checkout(root))
}

fn is_checkout(root: &Path) -> bool {
    root.join("bin/bootstrap").is_file() && root.join("packs").is_dir()
}

/// The checkout's project interpreter, once installation has created it.
pub fn project_python(root: &Path) -> Option<PathBuf> {
    let python = if cfg!(windows) {
        root.join(".venv").join("Scripts").join("python.exe")
    } else {
        root.join(".venv").join("bin").join("python")
    };
    python.is_file().then_some(python)
}

/// Export the login shell's PATH, plus the usual install folders, to this process.
pub fn adopt_login_path() {
    let mut entries: Vec<PathBuf> = login_shell_path()
        .or_else(|| std::env::var("PATH").ok())
        .map(|path| std::env::split_paths(&path).collect())
        .unwrap_or_default();
    if let Some(home) = dirs::home_dir() {
        for extra in [home.join(".local/bin"), home.join(".cargo/bin")] {
            entries.push(extra);
        }
    }
    for extra in ["/opt/homebrew/bin", "/usr/local/bin"] {
        entries.push(PathBuf::from(extra));
    }
    let mut seen = std::collections::HashSet::new();
    entries.retain(|entry| seen.insert(entry.clone()));
    if let Ok(joined) = std::env::join_paths(entries) {
        std::env::set_var("PATH", joined);
    }
}

#[cfg(unix)]
fn login_shell_path() -> Option<String> {
    let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/zsh".into());
    let script = format!("printf '{PATH_MARKER}%s{PATH_MARKER}' \"$PATH\"");
    let output = Command::new(shell).args(["-ilc", &script]).output().ok()?;
    let text = String::from_utf8_lossy(&output.stdout);
    let path = text.split(PATH_MARKER).nth(1)?;
    (!path.is_empty()).then(|| path.to_string())
}

#[cfg(not(unix))]
fn login_shell_path() -> Option<String> {
    let _ = PATH_MARKER;
    None
}

/// The first executable named `name` on PATH.
pub fn which(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    let names: Vec<String> = if cfg!(windows) {
        vec![
            format!("{name}.exe"),
            format!("{name}.cmd"),
            name.to_string(),
        ]
    } else {
        vec![name.to_string()]
    };
    std::env::split_paths(&path)
        .flat_map(|dir| names.iter().map(move |file| dir.join(file)))
        .find(|candidate| candidate.is_file())
}

/// The Codex CLI: on PATH, else the copy inside the Codex desktop app.
pub fn codex() -> Option<PathBuf> {
    const CODEX_APP_CLI: &str = "/Applications/Codex.app/Contents/Resources/codex";
    which("codex").or_else(|| {
        let bundled = PathBuf::from(CODEX_APP_CLI);
        bundled.is_file().then_some(bundled)
    })
}
