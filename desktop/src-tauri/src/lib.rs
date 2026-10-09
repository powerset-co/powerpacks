//! The Powerpacks desktop app: the local Powerpacks page in a native window, with Codex as the
//! in-app agent. Everything it needs ships inside it (uv, Codex, the Powerpacks source).
//!
//! The window opens on the bundled splash (`desktop/splash`); `boot` installs and starts
//! Powerpacks and navigates the window to it; `onboard` runs setup; `codex` runs the agent.
//! Links that leave the local page open in the system browser, where Google, ChatGPT and
//! LinkedIn allow sign-in.

mod boot;
mod children;
mod codex;
mod linkedin;
mod onboard;
mod paths;
mod signin;
mod source;
mod update;

use std::sync::Arc;

use serde_json::Value;
use tauri::webview::NewWindowResponse;
use tauri::{AppHandle, Manager, State, Url, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_opener::OpenerExt;

use boot::{Boot, BootState};
use codex::Codex;

const MAIN_WINDOW: &str = "main";
const SPLASH_PAGE: &str = "index.html";
const WINDOW_SIZE: (f64, f64) = (1360.0, 860.0);
const WINDOW_MIN_SIZE: (f64, f64) = (960.0, 640.0);

#[tauri::command]
fn boot_state(boot: State<'_, Arc<Boot>>) -> BootState {
    boot.snapshot()
}

#[tauri::command]
fn boot_retry(app: AppHandle) {
    boot::launch(app);
}

#[tauri::command]
async fn codex_status(
    app: AppHandle,
    boot: State<'_, Arc<Boot>>,
    codex: State<'_, Codex>,
) -> Result<Value, String> {
    codex.status(&app, boot.root().as_deref()).await
}

/// Starts ChatGPT sign-in; the page shows the returned `authUrl` in the sign-in pane.
#[tauri::command]
async fn codex_login(
    app: AppHandle,
    boot: State<'_, Arc<Boot>>,
    codex: State<'_, Codex>,
) -> Result<Value, String> {
    codex.login(&app, boot.root().as_deref()).await
}

/// Shows a provider's sign-in page inside the app's sign-in modal; `finish` is the callback
/// URL it ends on, `bounds` the modal body in CSS pixels.
#[tauri::command]
fn signin_open(
    app: AppHandle,
    url: String,
    finish: String,
    bounds: signin::Bounds,
) -> Result<(), String> {
    signin::open(&app, &url, &finish, bounds)
}

#[tauri::command]
fn signin_place(app: AppHandle, bounds: signin::Bounds) -> Result<(), String> {
    signin::place(&app, bounds)
}

/// Reads the LinkedIn connections list in the sign-in view; see linkedin.rs.
#[tauri::command]
async fn linkedin_read(
    app: AppHandle,
    boot: State<'_, Arc<Boot>>,
) -> Result<linkedin::Progress, String> {
    let root = boot::require_root(&boot)?;
    tauri::async_runtime::spawn_blocking(move || linkedin::read(&app, &root))
        .await
        .map_err(|error| error.to_string())?
}

/// Bring the window forward, after a sign-in that had to happen in the browser.
#[tauri::command]
fn app_focus(app: AppHandle) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

/// Open a web page in the system browser (Google sign-in, which refuses embedded views).
#[tauri::command]
fn open_external(app: AppHandle, url: String) -> Result<(), String> {
    let parsed: Url = url.parse().map_err(|error| format!("Bad URL: {error}"))?;
    if parsed.scheme() != "https" {
        return Err("Only https pages open outside the app.".into());
    }
    app.opener()
        .open_url(parsed.as_str(), None::<&str>)
        .map_err(|error| error.to_string())
}

/// The latest Powerpacks release against this app's version, for the side nav's update pane.
#[tauri::command]
async fn update_check(app: AppHandle) -> Result<update::Update, String> {
    update::check(&app.package_info().version.to_string()).await
}

#[tauri::command]
fn signin_close(app: AppHandle) {
    signin::close(&app)
}

#[tauri::command]
async fn codex_start_thread(
    app: AppHandle,
    boot: State<'_, Arc<Boot>>,
    codex: State<'_, Codex>,
) -> Result<Value, String> {
    codex.start_thread(&app, &boot::require_root(&boot)?).await
}

#[tauri::command]
async fn codex_open_thread(
    app: AppHandle,
    boot: State<'_, Arc<Boot>>,
    codex: State<'_, Codex>,
    thread_id: String,
) -> Result<Value, String> {
    codex
        .open_thread(&app, &boot::require_root(&boot)?, &thread_id)
        .await
}

#[tauri::command]
async fn codex_threads(
    app: AppHandle,
    boot: State<'_, Arc<Boot>>,
    codex: State<'_, Codex>,
) -> Result<Value, String> {
    codex.threads(&app, &boot::require_root(&boot)?).await
}

/// Test builds show a debug menu (CI sets POWERPACKS_DEBUG_MENU; debug builds always do).
const DEBUG_MENU: bool = cfg!(debug_assertions) || option_env!("POWERPACKS_DEBUG_MENU").is_some();

#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
struct DebugState {
    enabled: bool,
    setup_skipped: bool,
}

#[tauri::command]
fn debug_state(boot: State<'_, Arc<Boot>>) -> DebugState {
    let setup_skipped = boot.root().is_some_and(|root| onboard::is_skipped(&root));
    DebugState {
        enabled: DEBUG_MENU,
        setup_skipped,
    }
}

/// Debug menu: skip setup (launch opens Chat), or stop skipping and resume it now.
#[tauri::command]
fn debug_skip_setup(boot: State<'_, Arc<Boot>>, skip: bool) -> Result<(), String> {
    if !DEBUG_MENU {
        return Err("The debug menu is off in this build.".into());
    }
    let root = boot::require_root(&boot)?;
    onboard::set_skipped(&root, skip)?;
    if skip {
        return Ok(());
    }
    onboard::start(&root, onboard::Answer::default())
}

/// Debug menu: forget the app's data and run setup again from the start.
#[tauri::command]
fn debug_reset_data(
    app: AppHandle,
    boot: State<'_, Arc<Boot>>,
    codex: State<'_, Codex>,
) -> Result<(), String> {
    if !DEBUG_MENU {
        return Err("The debug menu is off in this build.".into());
    }
    let root = boot::require_root(&boot)?;
    onboard::stop_running(&root);
    codex.shutdown();
    boot::reset(&app, &boot)
}

/// The command-line install the setup page can import, as a path to show.
#[tauri::command]
fn setup_import_source(boot: State<'_, Arc<Boot>>) -> Option<String> {
    let root = boot.root()?;
    source::cli_install(&root).map(|path| path.display().to_string())
}

/// Import the command-line install's data; true when setup is already done with it.
#[tauri::command]
fn setup_import(boot: State<'_, Arc<Boot>>) -> Result<bool, String> {
    let root = boot::require_root(&boot)?;
    let cli = source::cli_install(&root).ok_or("No Powerpacks install to import.")?;
    source::import_cli_data(&root, &cli)?;
    Ok(onboard::setup(&root) == onboard::Setup::Done)
}

/// Whether macOS lets this app read Messages (Full Disk Access), checked afresh each call.
#[tauri::command]
async fn permission_messages(boot: State<'_, Arc<Boot>>) -> Result<bool, String> {
    let root = boot::require_root(&boot)?;
    Ok(
        tauri::async_runtime::spawn_blocking(move || onboard::messages_readable(&root))
            .await
            .unwrap_or(false),
    )
}

/// Resumes setup from the install page with what the user answered there.
#[tauri::command]
fn onboard_continue(boot: State<'_, Arc<Boot>>, answer: onboard::Answer) -> Result<(), String> {
    onboard::start(&boot::require_root(&boot)?, answer)
}

#[tauri::command]
async fn codex_call(
    app: AppHandle,
    boot: State<'_, Arc<Boot>>,
    codex: State<'_, Codex>,
    method: String,
    params: Value,
) -> Result<Value, String> {
    if !codex::PAGE_METHODS.contains(&method.as_str()) {
        return Err(format!("{method} is not available to the page."));
    }
    codex
        .call(&app, boot.root().as_deref(), &method, params)
        .await
}

/// Answers a Codex server request (an approval) by its JSON-RPC id.
#[tauri::command]
async fn codex_respond(
    app: AppHandle,
    codex: State<'_, Codex>,
    id: Value,
    result: Value,
) -> Result<(), String> {
    codex.respond(&app, id, result).await
}

/// The local page and the bundled splash stay in the window; everything else opens outside.
fn is_local(url: &Url) -> bool {
    match url.scheme() {
        "tauri" | "about" | "data" | "blob" => true,
        "http" | "https" => {
            url.host_str() == Some("tauri.localhost")
                || (matches!(url.host_str(), Some(boot::HOST) | Some("localhost"))
                    && url.port() == Some(boot::PORT))
        }
        _ => false,
    }
}

fn open_outside(app: &AppHandle, url: &Url) {
    if matches!(url.scheme(), "http" | "https" | "mailto") {
        let _ = app.opener().open_url(url.as_str(), None::<&str>);
    }
}

fn main_window(app: &AppHandle) -> tauri::Result<()> {
    let (navigation_app, popup_app) = (app.clone(), app.clone());
    let builder = WebviewWindowBuilder::new(app, MAIN_WINDOW, WebviewUrl::App(SPLASH_PAGE.into()))
        .title("Powerpacks")
        .inner_size(WINDOW_SIZE.0, WINDOW_SIZE.1)
        .min_inner_size(WINDOW_MIN_SIZE.0, WINDOW_MIN_SIZE.1)
        .background_color(tauri::window::Color(0x1a, 0x16, 0x14, 0xff))
        .on_navigation(move |url| {
            let local = is_local(url);
            if !local {
                open_outside(&navigation_app, url);
            }
            local
        })
        .on_new_window(move |url, _features| {
            open_outside(&popup_app, &url);
            NewWindowResponse::Deny
        });
    #[cfg(target_os = "macos")]
    let builder = builder
        .title_bar_style(tauri::TitleBarStyle::Overlay)
        .hidden_title(true);
    builder.build()?;
    Ok(())
}

/// "Setup is still running": quit anyway, or keep it running.
fn confirm_quit(app: &AppHandle) -> bool {
    use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
    app.dialog()
        .message("Setup is still importing your contacts. If you quit now it pauses, and picks up from the same step the next time you open Powerpacks.")
        .title("Quit while setup is running?")
        .kind(MessageDialogKind::Warning)
        .buttons(MessageDialogButtons::OkCancelCustom("Quit".into(), "Keep running".into()))
        .blocking_show()
}

/// A termination signal (Terminal, `kill`, the system shutting down) quits the app the normal
/// way, so the exit handler below still stops the server, Codex and setup.
fn quit_on_signal(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        #[cfg(unix)]
        {
            use tokio::signal::unix::{signal, SignalKind};
            let (Ok(mut term), Ok(mut int)) = (
                signal(SignalKind::terminate()),
                signal(SignalKind::interrupt()),
            ) else {
                return;
            };
            tokio::select! { _ = term.recv() => {}, _ = int.recv() => {} }
        }
        #[cfg(not(unix))]
        {
            let _ = tokio::signal::ctrl_c().await;
        }
        app.exit(0);
    });
}

pub fn run() {
    paths::adopt_path();
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(Arc::new(Boot::default()))
        .manage(Codex::default())
        .manage(children::Children::default())
        .invoke_handler(tauri::generate_handler![
            boot_state,
            boot_retry,
            codex_status,
            codex_login,
            codex_start_thread,
            codex_open_thread,
            codex_threads,
            codex_call,
            codex_respond,
            signin_open,
            signin_place,
            signin_close,
            linkedin_read,
            app_focus,
            open_external,
            update_check,
            onboard_continue,
            permission_messages,
            debug_state,
            debug_skip_setup,
            debug_reset_data,
            setup_import_source,
            setup_import,
        ])
        .setup(|app| {
            quit_on_signal(app.handle().clone());
            main_window(app.handle())?;
            boot::launch(app.handle().clone());
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Powerpacks failed to start")
        .run(|app, event| {
            // Quitting mid-setup pauses the import until the next launch; ask first.
            if let tauri::RunEvent::ExitRequested { api, code, .. } = &event {
                let mid_setup = code.is_none()
                    && app
                        .state::<Arc<Boot>>()
                        .root()
                        .is_some_and(|root| onboard::is_running(&root));
                if mid_setup && !confirm_quit(app) {
                    api.prevent_exit();
                }
                return;
            }
            // Nothing of Powerpacks outlives the window: the page server, Codex, and setup.
            if let tauri::RunEvent::Exit = event {
                app.state::<Codex>().shutdown();
                if let Some(root) = app.state::<Arc<Boot>>().root() {
                    onboard::stop_running(&root);
                }
                app.state::<children::Children>().stop_all();
            }
        });
}
