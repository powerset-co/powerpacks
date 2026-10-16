//! Signed in-app updates from the configured Tauri updater feed.

use serde::Serialize;
use tauri::{AppHandle, Emitter};
use tauri_plugin_updater::UpdaterExt;

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
    let mut downloaded = 0;
    update
        .download_and_install(
            |bytes, total| {
                downloaded += bytes as u64;
                let _ = app.emit("update:progress", Progress { downloaded, total });
            },
            || {},
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
}
