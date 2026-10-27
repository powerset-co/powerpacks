use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{AppHandle, Emitter, Manager, State, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

#[cfg(target_os = "macos")]
mod mac;

#[derive(Default)]
pub struct Permission(Mutex<Option<tauri::async_runtime::JoinHandle<()>>>);

const HELPER: (f64, f64) = (560.0, 144.0);

fn bundle(executable: &Path) -> Result<PathBuf, String> {
    executable
        .ancestors()
        .find(|path| path.extension().is_some_and(|ext| ext == "app"))
        .map(Path::to_path_buf)
        .ok_or_else(|| "Drag requires a bundled Powerpacks.app, not cargo run.".into())
}

#[tauri::command]
pub async fn permission_begin(
    app: AppHandle,
    permission: State<'_, Permission>,
) -> Result<(), String> {
    bundle(&std::env::current_exe().map_err(|error| error.to_string())?)?;
    if app.get_webview_window("permission").is_some() {
        return Ok(());
    }
    let window = app
        .get_webview_window(crate::MAIN_WINDOW)
        .ok_or("Powerpacks window is closed.")?;
    let mut url = window.url().map_err(|error| error.to_string())?;
    url.set_path("/permission");
    url.set_query(None);
    url.set_fragment(None);
    let helper = WebviewWindowBuilder::new(&app, "permission", WebviewUrl::External(url))
        .title("Messages permission")
        .inner_size(HELPER.0, HELPER.1)
        .decorations(false)
        .transparent(true)
        .always_on_top(true)
        .visible_on_all_workspaces(true)
        .skip_taskbar(true)
        .resizable(false)
        .visible(false)
        .focused(false)
        .accept_first_mouse(true)
        .disable_drag_drop_handler()
        .build()
        .map_err(|error| error.to_string())?;
    #[cfg(target_os = "macos")]
    helper
        .with_webview(|webview| unsafe {
            let window = &*webview.ns_window().cast::<objc2_app_kit::NSWindow>();
            window.setTabbingMode(objc2_app_kit::NSWindowTabbingMode::Disallowed);
            window.setExcludedFromWindowsMenu(true);
        })
        .map_err(|error| error.to_string())?;
    *permission.0.lock().unwrap() = Some(tauri::async_runtime::spawn(async move {
        loop {
            #[cfg(target_os = "macos")]
            if helper
                .with_webview(|webview| unsafe {
                    mac::follow_settings(webview.ns_window().cast());
                })
                .is_err()
            {
                break;
            }
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
    }));
    Ok(())
}

#[tauri::command]
pub async fn permission_end(
    app: AppHandle,
    permission: State<'_, Permission>,
) -> Result<(), String> {
    if let Some(task) = permission.0.lock().unwrap().take() {
        task.abort();
    }
    if let Some(helper) = app.get_webview_window("permission") {
        helper.destroy().map_err(|error| error.to_string())?;
    }
    crate::show_main(&app);
    app.emit_to(crate::MAIN_WINDOW, "permission-ended", ())
        .map_err(|error| error.to_string())?;
    Ok(())
}

#[tauri::command]
pub async fn permission_drag(window: WebviewWindow) -> Result<(), String> {
    let path = bundle(&std::env::current_exe().map_err(|error| error.to_string())?)?;
    #[cfg(target_os = "macos")]
    {
        let (send, receive) = tokio::sync::oneshot::channel();
        window
            .with_webview(move |webview| {
                let result = unsafe { mac::drag(webview.ns_window().cast(), &path) };
                let _ = send.send(result);
            })
            .map_err(|error| error.to_string())?;
        receive.await.map_err(|error| error.to_string())?
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (window, path);
        Err("Full Disk Access is a macOS permission.".into())
    }
}

#[cfg(test)]
mod tests {
    use super::bundle;
    use std::path::Path;

    #[test]
    fn guiding_permissions_never_scripts_settings_or_resizes_the_main_window() {
        let source = include_str!("permission.rs");
        let begin = source
            .split("pub async fn permission_begin")
            .nth(1)
            .unwrap()
            .split("pub async fn permission_end")
            .next()
            .unwrap();
        assert!(
            !begin.contains("osascript"),
            "Settings tracking must not require Automation"
        );
        assert!(
            !begin.contains("set_min_size"),
            "Guidance must leave the main window alone"
        );
    }

    /// The helper is a second window on the page server's URL: its capability must let it call
    /// the three permission commands, as the main window's does. Tauri resolves a call against
    /// the page's origin alone (the IPC request's Origin header, no path), so the origin is
    /// what the capability has to match.
    #[test]
    fn the_helper_window_may_call_the_permission_commands() {
        let mut context = crate::context();
        let authority = context.runtime_authority_mut();
        let helper = tauri::ipc::Origin::Remote {
            url: tauri::Url::parse("http://127.0.0.1:8765/").unwrap(),
        };
        let main = tauri::ipc::Origin::Remote {
            url: tauri::Url::parse("http://127.0.0.1:8765/").unwrap(),
        };
        for command in ["permission_begin", "permission_end", "permission_drag"] {
            let allowed = authority.resolve_access(command, "permission", "permission", &helper);
            assert!(
                allowed.is_some_and(|resolved| !resolved.is_empty()),
                "{command} from the helper"
            );
        }
        for command in ["permission_begin", "permission_end"] {
            let allowed = authority.resolve_access(command, "main", "main", &main);
            assert!(
                allowed.is_some_and(|resolved| !resolved.is_empty()),
                "{command} from the page"
            );
        }
    }

    #[test]
    fn drags_the_running_bundle_not_an_installed_copy() {
        let path = Path::new("/tmp/Test Build/Powerpacks.app/Contents/MacOS/powerpacks-desktop");
        assert_eq!(
            bundle(path).unwrap(),
            Path::new("/tmp/Test Build/Powerpacks.app")
        );
        assert!(bundle(Path::new("/tmp/target/debug/powerpacks-desktop")).is_err());
    }
}
