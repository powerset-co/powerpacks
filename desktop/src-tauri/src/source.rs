//! The Powerpacks code the app ships (`resources/powerpacks.tar.gz`, from
//! scripts/bundle-runtime.sh), installed as the checkout every Powerpacks tool expects: code,
//! `.env` and `.powerpacks/` in one folder.
//!
//! A git checkout (only reached through POWERPACKS_REPO_ROOT) belongs to its owner and is used
//! as it is. The app's own folder is refreshed when the app's version changes: the archive's
//! top-level entries are replaced and `.env`, `.powerpacks/` and `.venv/` (never in the
//! archive) stay.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

pub const ARCHIVE_RESOURCE: &str = "powerpacks.tar.gz";
const MARKER: &str = ".powerpacks/desktop/source-version";
const ENV_TEMPLATE: &str = "packs/powerset/templates/env.powerset.example";

pub enum Plan {
    /// The installed code is current, or a git checkout owns it.
    Ready,
    /// Install (or refresh) the bundled code.
    Install,
}

/// What to do with `root` for this app version; an unrelated non-empty folder is an error.
pub fn plan(root: &Path, version: &str) -> Result<Plan, String> {
    if root.join(".git").exists() {
        return Ok(Plan::Ready);
    }
    if let Ok(installed) = fs::read_to_string(root.join(MARKER)) {
        return Ok(if installed.trim() == version {
            Plan::Ready
        } else {
            Plan::Install
        });
    }
    let empty = fs::read_dir(root)
        .map(|mut entries| entries.next().is_none())
        .unwrap_or(true);
    if empty || root.join("bin/onboard").is_file() {
        return Ok(Plan::Install);
    }
    Err(format!(
        "{} already holds other files. Move them, or set POWERPACKS_REPO_ROOT to another folder.",
        root.display()
    ))
}

pub fn install(archive: &Path, root: &Path, version: &str) -> Result<(), String> {
    fs::create_dir_all(root)
        .map_err(|error| format!("Could not create {}: {error}", root.display()))?;
    for entry in top_level_entries(archive)? {
        let path = root.join(&entry);
        let Ok(metadata) = fs::symlink_metadata(&path) else {
            continue;
        };
        let removed = if metadata.is_dir() {
            fs::remove_dir_all(&path)
        } else {
            fs::remove_file(&path)
        };
        removed.map_err(|error| format!("Could not replace {}: {error}", path.display()))?;
    }
    tar(&[
        OsArg::from("-xzf"),
        archive.into(),
        "-C".into(),
        root.into(),
    ])?;
    let marker = root.join(MARKER);
    fs::create_dir_all(marker.parent().expect("marker has a parent"))
        .map_err(|error| error.to_string())?;
    fs::write(&marker, version).map_err(|error| error.to_string())
}

/// `.env` from the Powerset template, as bootstrap writes it, readable only by the user.
pub fn ensure_env(root: &Path) -> Result<(), String> {
    let env = root.join(".env");
    if env.exists() {
        return Ok(());
    }
    fs::copy(root.join(ENV_TEMPLATE), &env)
        .map_err(|error| format!("Could not write .env: {error}"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&env, fs::Permissions::from_mode(0o600))
            .map_err(|error| error.to_string())?;
    }
    Ok(())
}

type OsArg = std::ffi::OsString;

fn tar(args: &[OsArg]) -> Result<String, String> {
    let output = Command::new("tar")
        .args(args)
        .output()
        .map_err(|error| format!("Could not run tar: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "Unpacking Powerpacks failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

fn top_level_entries(archive: &Path) -> Result<Vec<PathBuf>, String> {
    let listing = tar(&["-tzf".into(), archive.into()])?;
    let mut entries: Vec<PathBuf> = listing
        .lines()
        .filter_map(|line| {
            line.split('/')
                .find(|part| !part.is_empty() && *part != ".")
        })
        .map(PathBuf::from)
        .collect();
    entries.sort();
    entries.dedup();
    Ok(entries)
}
