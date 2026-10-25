use std::path::{Path, PathBuf};
use std::sync::Mutex;

use tauri::{
    AppHandle, Emitter, LogicalPosition, LogicalSize, Manager, PhysicalPosition, PhysicalSize,
    State, WebviewUrl, WebviewWindow, WebviewWindowBuilder,
};

#[cfg(target_os = "macos")]
mod mac;

struct Frame {
    position: PhysicalPosition<i32>,
    size: PhysicalSize<u32>,
}

#[derive(Default)]
pub struct Permission(Mutex<Option<Frame>>);

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
    let window = app
        .get_webview_window(crate::MAIN_WINDOW)
        .ok_or("Powerpacks window is closed.")?;
    let monitor = window
        .primary_monitor()
        .map_err(|error| error.to_string())?
        .ok_or("No main screen.")?;
    let area = monitor.work_area();
    let scale = monitor.scale_factor();
    let position = area.position.to_logical::<f64>(scale);
    let size = area.size.to_logical::<f64>(scale);
    {
        let mut saved = permission.0.lock().unwrap();
        if saved.is_none() {
            *saved = Some(Frame {
                position: window.outer_position().map_err(|error| error.to_string())?,
                size: window.inner_size().map_err(|error| error.to_string())?,
            });
        }
    }
    window
        .set_min_size(None::<LogicalSize<f64>>)
        .map_err(|error| error.to_string())?;
    window
        .set_position(position)
        .map_err(|error| error.to_string())?;
    let titlebar = window
        .outer_size()
        .map_err(|error| error.to_string())?
        .height
        - window
            .inner_size()
            .map_err(|error| error.to_string())?
            .height;
    window
        .set_size(tauri::Size::Logical(LogicalSize::new(
            size.width / 2.0,
            size.height - f64::from(titlebar) / scale,
        )))
        .map_err(|error| error.to_string())?;
    if app.get_webview_window("permission").is_none() {
        let mut url = window.url().map_err(|error| error.to_string())?;
        url.set_path("/permission");
        url.set_query(None);
        url.set_fragment(None);
        let helper = WebviewWindowBuilder::new(&app, "permission", WebviewUrl::External(url))
            .title("Messages permission")
            .inner_size(360.0, 224.0)
            .position(
                position.x + size.width * 0.75 - 180.0,
                position.y + size.height - 236.0,
            )
            .decorations(false)
            .transparent(true)
            .always_on_top(true)
            .visible_on_all_workspaces(true)
            .skip_taskbar(true)
            .resizable(false)
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
    }
    let right = LogicalPosition::new(position.x + size.width / 2.0, position.y);
    tauri::async_runtime::spawn_blocking(move || {
        let script = format!(r#"
tell application "System Events"
    repeat 30 times
        if exists process "System Settings" then
            tell process "System Settings"
                if exists window 1 then
                    set position of window 1 to {{{x}, {y}}}
                    set size of window 1 to {{{width}, {height}}}
                    return
                end if
            end tell
        end if
        delay 0.1
    end repeat
    error "System Settings did not open."
end tell
"#, x = right.x.round(), y = right.y.round(), width = (size.width / 2.0).round(), height = size.height.round());
        let output = std::process::Command::new("osascript").args(["-e", &script]).output()
            .map_err(|error| error.to_string())?;
        if output.status.success() { Ok(()) } else {
            Err(format!("Could not position System Settings. Allow Powerpacks in Accessibility and Automation, or move Settings to the right yourself. {}", String::from_utf8_lossy(&output.stderr).trim()))
        }
    }).await.map_err(|error| error.to_string())?
}

#[tauri::command]
pub async fn permission_end(
    app: AppHandle,
    permission: State<'_, Permission>,
) -> Result<(), String> {
    let frame = permission.0.lock().unwrap().take();
    if let Some(frame) = frame {
        if let Some(helper) = app.get_webview_window("permission") {
            helper.destroy().map_err(|error| error.to_string())?;
        }
        let window = app
            .get_webview_window(crate::MAIN_WINDOW)
            .ok_or("Powerpacks window is closed.")?;
        window
            .set_size(frame.size)
            .map_err(|error| error.to_string())?;
        window
            .set_position(frame.position)
            .map_err(|error| error.to_string())?;
        window
            .set_min_size(Some(LogicalSize::new(
                crate::WINDOW_MIN_SIZE.0,
                crate::WINDOW_MIN_SIZE.1,
            )))
            .map_err(|error| error.to_string())?;
        crate::show_main(&app);
        app.emit_to(crate::MAIN_WINDOW, "permission-ended", ())
            .map_err(|error| error.to_string())?;
    }
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
    fn drags_the_running_bundle_not_an_installed_copy() {
        let path = Path::new("/tmp/Test Build/Powerpacks.app/Contents/MacOS/powerpacks-desktop");
        assert_eq!(
            bundle(path).unwrap(),
            Path::new("/tmp/Test Build/Powerpacks.app")
        );
        assert!(bundle(Path::new("/tmp/target/debug/powerpacks-desktop")).is_err());
    }
}
