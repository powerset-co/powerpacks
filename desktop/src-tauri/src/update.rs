//! The app's update check: the latest Powerpacks release on GitHub against the version this
//! app was built as (tauri.conf.json), for the side nav's update pane. Release Please tags the
//! releases `powerpacks-v3.22.2`; the number after the last `v` is what compares.

use std::time::Duration;

use serde::{Deserialize, Serialize};

const DEFAULT_RELEASES_URL: &str =
    "https://api.github.com/repos/powerset-co/powerpacks/releases/latest";
/// Baked at build time, for a fork or a staging feed.
const RELEASES_URL: &str = match option_env!("POWERPACKS_UPDATE_URL") {
    Some(url) => url,
    None => DEFAULT_RELEASES_URL,
};
const TIMEOUT: Duration = Duration::from_secs(10);

#[derive(Deserialize)]
struct Release {
    tag_name: String,
    html_url: String,
}

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct Update {
    pub current: String,
    pub latest: String,
    /// The release page, to open in the browser.
    pub url: String,
    pub available: bool,
}

/// The latest release against `current`; a network problem is the error's text, never a panic.
pub async fn check(current: &str) -> Result<Update, String> {
    let client = reqwest::Client::builder()
        .timeout(TIMEOUT)
        .user_agent(format!("powerpacks-desktop/{current}"))
        .build()
        .map_err(|error| error.to_string())?;
    let release: Release = client
        .get(RELEASES_URL)
        .header("Accept", "application/vnd.github+json")
        .send()
        .await
        .map_err(|error| format!("Couldn't reach the release feed: {error}"))?
        .error_for_status()
        .map_err(|error| format!("The release feed answered {error}"))?
        .json()
        .await
        .map_err(|error| format!("The release feed answered oddly: {error}"))?;
    let latest = version_of(&release.tag_name).to_owned();
    Ok(Update {
        available: newer(&latest, current),
        current: current.to_owned(),
        latest,
        url: release.html_url,
    })
}

/// `powerpacks-v3.22.2` and `v3.22.2` are both version 3.22.2.
fn version_of(tag: &str) -> &str {
    tag.rsplit('v').next().unwrap_or(tag)
}

/// Dotted numbers compare numerically; anything else counts as newer when it differs.
fn newer(latest: &str, current: &str) -> bool {
    match (parts(latest), parts(current)) {
        (Some(latest), Some(current)) => latest > current,
        _ => latest != current,
    }
}

fn parts(version: &str) -> Option<Vec<u64>> {
    version.split('.').map(|part| part.parse().ok()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tags_strip_the_component_and_the_v() {
        assert_eq!(version_of("powerpacks-v3.22.2"), "3.22.2");
        assert_eq!(version_of("v3.23.0"), "3.23.0");
        assert_eq!(version_of("3.23.0"), "3.23.0");
    }

    #[test]
    fn versions_compare_numerically() {
        assert!(newer("3.23.0", "3.22.2"));
        assert!(newer("3.22.10", "3.22.9"));
        assert!(!newer("3.22.2", "3.22.2"));
        assert!(!newer("3.22.2", "3.23.0"));
        assert!(newer("2026.1", "3.22.2"));
        assert!(!newer("beta", "beta"));
        assert!(newer("beta", "3.22.2"));
    }
}
