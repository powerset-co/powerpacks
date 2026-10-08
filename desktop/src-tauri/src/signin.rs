//! Sign-in inside the app: a second web view docked in the main window, below the top bar and
//! the page's "Signing in to …" strip, showing the provider's own sign-in page. Its session
//! (cookies, logins) persists in the app's data folder, so LinkedIn and the rest stay signed in.
//!
//! The page opens it with a URL and the callback prefix the sign-in ends on; reaching that
//! prefix emits `signin://finished` and the callback still loads, so the local server that
//! owns it (the Powerset login, Codex) receives the code.

use std::path::PathBuf;

use tauri::webview::WebviewBuilder;
use tauri::{
    AppHandle, Emitter, LogicalPosition, LogicalSize, Manager, Url, WebviewUrl, Window, WindowEvent,
};

pub const LABEL: &str = "signin";
pub const FINISHED_EVENT: &str = "signin://finished";
const DATA_DIR: &str = "signin";
/// The main page's top bar and the sign-in strip above the pane (web/src: TopBar, SignInBar).
const TOP_PX: f64 = 52.0 + 44.0;

/// The pane's place: the window below the top bar and strip, in logical pixels.
fn frame(window: &Window) -> tauri::Result<(LogicalPosition<f64>, LogicalSize<f64>)> {
    let size = window
        .inner_size()?
        .to_logical::<f64>(window.scale_factor()?);
    Ok((
        LogicalPosition::new(0.0, TOP_PX),
        LogicalSize::new(size.width, (size.height - TOP_PX).max(0.0)),
    ))
}

fn data_dir(app: &AppHandle) -> Option<PathBuf> {
    app.path().app_data_dir().ok().map(|dir| dir.join(DATA_DIR))
}

/// Show `url` in the pane, replacing any sign-in already open.
pub fn open(app: &AppHandle, url: &str, finish: &str) -> Result<(), String> {
    let url: Url = url
        .parse()
        .map_err(|error| format!("Bad sign-in URL: {error}"))?;
    close(app);
    let window = app.get_window("main").ok_or("The main window is gone.")?;
    let (position, size) = frame(&window).map_err(|error| error.to_string())?;
    let (done, finish) = (app.clone(), finish.to_owned());
    let mut builder =
        WebviewBuilder::new(LABEL, WebviewUrl::External(url)).on_navigation(move |url| {
            if url.as_str().starts_with(&finish) {
                let _ = done.emit(FINISHED_EVENT, url.as_str());
            }
            true
        });
    if let Some(dir) = data_dir(app) {
        builder = builder.data_directory(dir);
    }
    window
        .add_child(builder, position, size)
        .map_err(|error| error.to_string())?;
    Ok(())
}

pub fn close(app: &AppHandle) {
    if let Some(pane) = app.get_webview(LABEL) {
        let _ = pane.close();
    }
}

/// Keep the pane filling the window as it resizes.
pub fn follow_window(window: &Window) {
    let app = window.app_handle().clone();
    window.on_window_event(move |event| {
        if !matches!(
            event,
            WindowEvent::Resized(_) | WindowEvent::ScaleFactorChanged { .. }
        ) {
            return;
        }
        let Some(pane) = app.get_webview(LABEL) else {
            return;
        };
        let Some(window) = app.get_window("main") else {
            return;
        };
        if let Ok((position, size)) = frame(&window) {
            let _ = pane.set_position(position);
            let _ = pane.set_size(size);
        }
    });
}
