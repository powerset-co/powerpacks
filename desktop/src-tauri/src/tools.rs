//! Where the command-line tools Gmail and WhatsApp use live: wacli at `~/.powerpacks/bin/wacli`
//! (the pinned install wacli/binary.py checks) and the Google Cloud CLI at
//! `~/.powerpacks/google-cloud-sdk`. The app downloads both at launch (boot.rs runs the preflight's
//! `tools`), and puts both folders on every child's PATH (paths.rs). msgvault is a sidecar.

use std::path::{Path, PathBuf};

/// `~/.powerpacks/bin`: wacli's pinned install folder (wacli/binary.py `WACLI_BIN_DIR`).
pub fn bin_dir(home: &Path) -> PathBuf {
    home.join(".powerpacks/bin")
}

/// `~/.powerpacks/google-cloud-sdk`: the preflight's Google Cloud CLI folder.
pub fn gcloud_dir(home: &Path) -> PathBuf {
    home.join(".powerpacks/google-cloud-sdk")
}
