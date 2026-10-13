//! Recipient opt-in. The native loop keeps working when the user leaves the Searches page.
use std::{fs, path::Path, sync::Arc, time::Duration};

use tauri::{AppHandle, Manager};

use crate::{boot::Boot, codex::Codex};

const FILE: &str = ".powerpacks/desktop/auto-reply.json";

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct FeatureFlags {
    #[serde(rename = "sets")]
    _sets: bool,
    auto_reply: bool,
}

pub fn enabled(root: &Path) -> Result<bool, String> {
    match fs::read(root.join(FILE)) {
        Ok(bytes) => serde_json::from_slice(&bytes).map_err(|error| error.to_string()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error.to_string()),
    }
}

pub fn save(root: &Path, enabled: bool) -> Result<bool, String> {
    fs::write(root.join(FILE), if enabled { "true" } else { "false" })
        .map_err(|error| error.to_string())?;
    Ok(enabled)
}

fn allowed(root: &Path) -> bool {
    enabled(root) == Ok(true)
        && fs::read(root.join(".powerpacks/desktop/features.json"))
            .ok()
            .and_then(|bytes| serde_json::from_slice::<FeatureFlags>(&bytes).ok())
            .is_some_and(|flags| flags.auto_reply)
}

pub fn launch(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        loop {
            tokio::time::sleep(Duration::from_secs(5)).await;
            let Some(root) = app.state::<Arc<Boot>>().root() else {
                continue;
            };
            if !allowed(&root) {
                continue;
            }
            if let Err(error) = app.state::<Codex>().auto_reply(&app, &root).await {
                eprintln!("Auto-reply: {error}");
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn off_by_default_and_persists_the_choice() {
        let root =
            std::env::temp_dir().join(format!("powerpacks-auto-reply-{}", std::process::id()));
        fs::create_dir_all(root.join(".powerpacks/desktop")).unwrap();
        assert!(!enabled(&root).unwrap());
        assert!(save(&root, true).unwrap());
        assert!(enabled(&root).unwrap());
        assert!(!save(&root, false).unwrap());
        assert!(!enabled(&root).unwrap());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn loop_requires_the_feature_and_the_user_choice() {
        let root = std::env::temp_dir().join(format!(
            "powerpacks-auto-reply-feature-{}",
            std::process::id()
        ));
        fs::create_dir_all(root.join(".powerpacks/desktop")).unwrap();
        let features = root.join(".powerpacks/desktop/features.json");
        assert!(!allowed(&root));
        save(&root, true).unwrap();
        assert!(!allowed(&root));
        fs::write(&features, r#"{"sets":true,"auto_reply":false}"#).unwrap();
        assert!(!allowed(&root));
        fs::write(&features, r#"{"sets":true,"auto_reply":true}"#).unwrap();
        assert!(allowed(&root));
        save(&root, false).unwrap();
        assert!(!allowed(&root));
        save(&root, true).unwrap();
        for text in [
            "invalid",
            r#"{"sets":true,"auto_reply":"true"}"#,
            r#"{"auto_reply":true}"#,
        ] {
            fs::write(&features, text).unwrap();
            assert!(!allowed(&root));
        }
        fs::remove_dir_all(root).unwrap();
    }
}
