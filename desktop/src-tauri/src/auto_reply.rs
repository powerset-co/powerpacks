//! Recipient opt-in. The native loop keeps working when the user leaves the Searches page.
use std::{fs, path::Path, sync::Arc, time::Duration};

use tauri::{AppHandle, Manager};

use crate::{boot::Boot, codex::Codex};

const FILE: &str = ".powerpacks/desktop/auto-reply.json";

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

pub fn launch(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        loop {
            tokio::time::sleep(Duration::from_secs(5)).await;
            let Some(root) = app.state::<Arc<Boot>>().root() else {
                continue;
            };
            if enabled(&root) != Ok(true) {
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
}
