//! The command-line tools the app ships and places under `~/.powerpacks` at launch, exactly where
//! Powerpacks looks for them: wacli at `~/.powerpacks/bin/wacli` with its version stamp (the pinned
//! install wacli/binary.py checks), and the Google Cloud CLI at `~/.powerpacks/google-cloud-sdk`
//! (its `VERSION` file). msgvault is a sidecar beside the executable, on PATH. Each is placed once
//! per bundled version; a launch with both current does nothing.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use tauri::path::BaseDirectory;
use tauri::{AppHandle, Manager};

const WACLI_RESOURCE: &str = "wacli";
const WACLI_VERSION_RESOURCE: &str = "wacli.version";
const GCLOUD_RESOURCE: &str = "google-cloud-sdk.tar.gz";
const GCLOUD_VERSION_RESOURCE: &str = "google-cloud-sdk.version";

/// `~/.powerpacks/bin`: wacli's pinned install folder (wacli/binary.py `WACLI_BIN_DIR`).
pub fn bin_dir(home: &Path) -> PathBuf {
    home.join(".powerpacks/bin")
}

/// `~/.powerpacks/google-cloud-sdk`: where the preflight used to download the CLI.
pub fn gcloud_dir(home: &Path) -> PathBuf {
    home.join(".powerpacks/google-cloud-sdk")
}

/// True when either tool is missing or at another version than the app ships.
pub fn stale(app: &AppHandle, home: &Path) -> bool {
    current(
        &bin_dir(home).join(".wacli-version"),
        &version(app, WACLI_VERSION_RESOURCE),
    )
    .is_none()
        || current(
            &gcloud_dir(home).join("VERSION"),
            &version(app, GCLOUD_VERSION_RESOURCE),
        )
        .is_none()
}

/// Place both tools at the bundled version.
pub fn place(app: &AppHandle, home: &Path) -> Result<(), String> {
    let wacli_version = version(app, WACLI_VERSION_RESOURCE);
    let bin = bin_dir(home);
    if current(&bin.join(".wacli-version"), &wacli_version).is_none() {
        fs::create_dir_all(&bin)
            .map_err(|error| format!("Could not create {}: {error}", bin.display()))?;
        let target = bin.join("wacli");
        fs::copy(resource(app, WACLI_RESOURCE)?, &target)
            .map_err(|error| format!("Could not place wacli: {error}"))?;
        executable(&target)?;
        fs::write(bin.join(".wacli-version"), format!("{wacli_version}\n"))
            .map_err(|error| format!("Could not stamp wacli: {error}"))?;
    }
    let gcloud_version = version(app, GCLOUD_VERSION_RESOURCE);
    let sdk = gcloud_dir(home);
    if current(&sdk.join("VERSION"), &gcloud_version).is_none() {
        if sdk.exists() {
            fs::remove_dir_all(&sdk)
                .map_err(|error| format!("Could not replace {}: {error}", sdk.display()))?;
        }
        let parent = sdk.parent().ok_or("No folder for the Google Cloud CLI")?;
        fs::create_dir_all(parent)
            .map_err(|error| format!("Could not create {}: {error}", parent.display()))?;
        let output = Command::new("tar")
            .arg("-xzf")
            .arg(resource(app, GCLOUD_RESOURCE)?)
            .arg("-C")
            .arg(parent)
            .output()
            .map_err(|error| format!("Could not run tar: {error}"))?;
        if !output.status.success() {
            return Err(format!(
                "Unpacking the Google Cloud CLI failed: {}",
                String::from_utf8_lossy(&output.stderr).trim()
            ));
        }
    }
    Ok(())
}

/// The placed tool's version when it is the bundled one.
fn current(stamp: &Path, bundled: &str) -> Option<String> {
    let placed = fs::read_to_string(stamp).ok()?;
    (placed.trim() == bundled).then(|| placed.trim().to_owned())
}

fn resource(app: &AppHandle, name: &str) -> Result<PathBuf, String> {
    app.path()
        .resolve(name, BaseDirectory::Resource)
        .map_err(|error| format!("The app is missing {name}: {error}"))
}

/// The version a bundled resource carries; an app built without it has none.
fn version(app: &AppHandle, name: &str) -> String {
    resource(app, name)
        .ok()
        .and_then(|path| fs::read_to_string(path).ok())
        .map(|text| text.trim().to_owned())
        .unwrap_or_default()
}

#[cfg(unix)]
fn executable(path: &Path) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(0o755))
        .map_err(|error| format!("Could not make {} executable: {error}", path.display()))
}

#[cfg(not(unix))]
fn executable(_path: &Path) -> Result<(), String> {
    Ok(())
}
