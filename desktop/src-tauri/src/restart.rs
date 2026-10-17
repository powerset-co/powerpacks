//! Consume the support runner's restart request while the app is running.
use std::{fs, io, path::Path, sync::Arc, time::Duration};

use tauri::{AppHandle, Manager};

use crate::boot::Boot;

const FILE: &str = ".powerpacks/desktop/restart-requested";
const POLL: Duration = Duration::from_secs(2);

fn consume(root: &Path, relaunch: impl FnOnce()) -> io::Result<()> {
    match fs::remove_file(root.join(FILE)) {
        Ok(()) => relaunch(),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    Ok(())
}

pub fn launch(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        loop {
            tokio::time::sleep(POLL).await;
            let Some(root) = app.state::<Arc<Boot>>().root() else {
                continue;
            };
            if let Err(error) = consume(&root, || app.request_restart()) {
                eprintln!("Support restart: {error}");
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn consumes_the_file_before_relaunching_once() {
        let root = std::env::temp_dir().join(format!("powerpacks-restart-{}", std::process::id()));
        fs::create_dir_all(root.join(".powerpacks/desktop")).unwrap();
        let request = root.join(FILE);
        consume(&root, || panic!("no request")).unwrap();
        let staged = root.join(".powerpacks/support/restart-requested");
        fs::create_dir_all(staged.parent().unwrap()).unwrap();
        fs::write(&staged, "").unwrap();
        consume(&root, || panic!("report not sent yet")).unwrap();
        assert!(staged.exists());
        fs::rename(&staged, &request).unwrap();
        let mut restarted = false;
        consume(&root, || {
            assert!(!request.exists());
            restarted = true;
        })
        .unwrap();
        assert!(restarted);
        consume(&root, || panic!("already consumed")).unwrap();
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn does_not_relaunch_when_the_request_cannot_be_removed() {
        let root =
            std::env::temp_dir().join(format!("powerpacks-restart-error-{}", std::process::id()));
        fs::create_dir_all(root.join(FILE)).unwrap();
        assert!(consume(&root, || panic!("request not consumed")).is_err());
        fs::remove_dir_all(root).unwrap();
    }
}
