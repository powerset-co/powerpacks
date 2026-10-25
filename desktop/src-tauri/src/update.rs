//! Signed in-app updates from the configured Tauri updater feed.

use serde::Serialize;
use std::sync::atomic::{AtomicU64, Ordering};
use tauri::{AppHandle, Emitter};
use tauri_plugin_updater::UpdaterExt;

const UPDATE_PROGRESS_EVENT: &str = "update:progress";

#[derive(Serialize)]
pub struct Update {
    pub current: String,
    pub latest: String,
    pub available: bool,
}

#[derive(Clone, Serialize)]
struct Progress {
    downloaded: u64,
    total: Option<u64>,
}

/// This build, with no update on offer: what a test build answers, since the release would
/// replace it (and lose what it is testing).
pub fn current(app: &AppHandle) -> Update {
    let current = app.package_info().version.to_string();
    Update {
        latest: current.clone(),
        available: false,
        current,
    }
}

pub async fn check(app: &AppHandle) -> Result<Update, String> {
    let current = app.package_info().version.to_string();
    let update = app
        .updater()
        .map_err(|error| error.to_string())?
        .check()
        .await
        .map_err(|error| error.to_string())?;
    Ok(Update {
        latest: update
            .as_ref()
            .map_or_else(|| current.clone(), |update| update.version.clone()),
        available: update.is_some(),
        current,
    })
}

pub async fn install(app: &AppHandle) -> Result<(), String> {
    let update = app
        .updater()
        .map_err(|error| error.to_string())?
        .check()
        .await
        .map_err(|error| error.to_string())?
        .ok_or("No update available.")?;
    let downloaded = AtomicU64::new(0);
    update
        .download_and_install(
            |bytes, total| {
                let downloaded =
                    downloaded.fetch_add(bytes as u64, Ordering::Relaxed) + bytes as u64;
                let _ = app.emit(UPDATE_PROGRESS_EVENT, Progress { downloaded, total });
            },
            || {
                let downloaded = downloaded.load(Ordering::Relaxed);
                let _ = app.emit(
                    UPDATE_PROGRESS_EVENT,
                    Progress {
                        downloaded,
                        total: Some(downloaded),
                    },
                );
            },
        )
        .await
        .map_err(|error| error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn check_payload_has_no_release_url() {
        let update = Update {
            current: "1.0.0".into(),
            latest: "1.1.0".into(),
            available: true,
        };
        assert_eq!(
            serde_json::to_value(update).unwrap(),
            json!({"current": "1.0.0", "latest": "1.1.0", "available": true})
        );
    }

    #[test]
    fn progress_payload_supports_known_and_unknown_totals() {
        for total in [Some(200), None] {
            assert_eq!(
                serde_json::to_value(Progress {
                    downloaded: 100,
                    total,
                })
                .unwrap(),
                json!({"downloaded": 100, "total": total})
            );
        }
    }

    #[test]
    fn finished_download_payload_reports_received_bytes_as_total() {
        let downloaded = 100;
        assert_eq!(
            serde_json::to_value(Progress {
                downloaded,
                total: Some(downloaded),
            })
            .unwrap(),
            json!({"downloaded": 100, "total": 100})
        );
    }
}
