//! Sign-in inside the app: a second web view placed over the main page, inside the sign-in
//! modal the page draws (web/src/components/shared/SignInModal.tsx). The page owns the layout:
//! it sends the card body's rectangle on open and whenever it moves, and the view fills it.
//! The view's session (cookies, logins) persists in the app's data folder.
//!
//! The page opens it with a URL and the callback prefix the sign-in ends on; reaching that
//! prefix emits `signin://finished` and the callback still loads, so the local server that
//! owns it (the Powerset login, Codex) receives the code.

use std::path::PathBuf;

use serde::Deserialize;
use tauri::webview::WebviewBuilder;
use tauri::{AppHandle, Emitter, LogicalPosition, LogicalSize, Manager, Url, WebviewUrl};

pub const LABEL: &str = "signin";
pub const FINISHED_EVENT: &str = "signin://finished";
const DATA_DIR: &str = "signin";

/// Where the view goes, in the page's CSS pixels (the window's logical pixels).
#[derive(Clone, Copy, Debug, Deserialize)]
pub struct Bounds {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}

impl Bounds {
    fn position(self) -> LogicalPosition<f64> {
        LogicalPosition::new(self.x, self.y)
    }

    fn size(self) -> LogicalSize<f64> {
        LogicalSize::new(self.width.max(1.0), self.height.max(1.0))
    }
}

fn data_dir(app: &AppHandle) -> Option<PathBuf> {
    app.path().app_data_dir().ok().map(|dir| dir.join(DATA_DIR))
}

/// Show `url` at `bounds`, replacing any sign-in already open.
pub fn open(app: &AppHandle, url: &str, finish: &str, bounds: Bounds) -> Result<(), String> {
    let url: Url = url
        .parse()
        .map_err(|error| format!("Bad sign-in URL: {error}"))?;
    close(app);
    let window = app.get_window("main").ok_or("The main window is gone.")?;
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
        .add_child(builder, bounds.position(), bounds.size())
        .map_err(|error| error.to_string())?;
    Ok(())
}

/// Move the open view; the page calls this as the modal resizes.
pub fn place(app: &AppHandle, bounds: Bounds) -> Result<(), String> {
    let Some(pane) = app.get_webview(LABEL) else {
        return Ok(());
    };
    pane.set_position(bounds.position())
        .map_err(|error| error.to_string())?;
    pane.set_size(bounds.size())
        .map_err(|error| error.to_string())
}

pub fn close(app: &AppHandle) {
    if let Some(pane) = app.get_webview(LABEL) {
        let _ = pane.close();
    }
}
