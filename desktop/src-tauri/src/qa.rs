//! QA hook for test builds: `desktop/qa` drives the real window from the terminal. It reads the
//! page with JavaScript, clicks and types with native mouse and key events (WebKit handles them
//! as a person's, so a menu under the drawer or an input that drops keystrokes shows), and takes
//! a picture of the page from WebKit itself (no Screen Recording permission needed).
//!
//! On only in a debug-menu build launched with `POWERPACKS_QA_PORT`; it listens on 127.0.0.1.
//! Links the page sends outside are recorded for the run instead of opening the browser.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::mpsc;
use std::sync::Mutex;
use std::time::Duration;

use serde_json::{json, Value};
use tauri::{AppHandle, Manager, Url};

const PORT_ENV: &str = "POWERPACKS_QA_PORT";
const WAIT: Duration = Duration::from_secs(20);

static OPENED: Mutex<Vec<String>> = Mutex::new(Vec::new());

fn port() -> Option<u16> {
    if !crate::DEBUG_MENU {
        return None;
    }
    std::env::var(PORT_ENV).ok()?.parse().ok()
}

/// Start listening when this is a QA run.
pub fn launch(app: AppHandle) {
    let Some(port) = port() else { return };
    let listener = match TcpListener::bind(("127.0.0.1", port)) {
        Ok(listener) => listener,
        Err(error) => {
            eprintln!("QA hook could not listen on {port}: {error}");
            return;
        }
    };
    std::thread::spawn(move || {
        for stream in listener.incoming().flatten() {
            let app = app.clone();
            std::thread::spawn(move || {
                let _ = answer(&app, stream);
            });
        }
    });
}

/// In a QA run a link that would open the browser is recorded instead; true when it was.
pub fn record_opened(url: &Url) -> bool {
    if port().is_none() {
        return false;
    }
    OPENED.lock().unwrap().push(url.to_string());
    true
}

fn answer(app: &AppHandle, stream: TcpStream) -> std::io::Result<()> {
    let mut reader = BufReader::new(stream.try_clone()?);
    let mut request_line = String::new();
    reader.read_line(&mut request_line)?;
    let path = request_line
        .split_whitespace()
        .nth(1)
        .unwrap_or("")
        .to_string();
    let mut length = 0;
    loop {
        let mut header = String::new();
        reader.read_line(&mut header)?;
        if header.trim().is_empty() {
            break;
        }
        if let Some((name, value)) = header.split_once(':') {
            if name.eq_ignore_ascii_case("content-length") {
                length = value.trim().parse().unwrap_or(0);
            }
        }
    }
    let mut body = vec![0; length];
    reader.read_exact(&mut body)?;
    let body: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
    let (status, kind, bytes) = match route(app, &path, &body) {
        Ok(Reply::Json(value)) => ("200 OK", "application/json", value.to_string().into_bytes()),
        Ok(Reply::Png(bytes)) => ("200 OK", "image/png", bytes),
        Err(error) => (
            "500 Internal Server Error",
            "application/json",
            json!({ "error": error }).to_string().into_bytes(),
        ),
    };
    let mut stream = stream;
    write!(
        stream,
        "HTTP/1.0 {status}\r\nContent-Type: {kind}\r\nContent-Length: {}\r\n\r\n",
        bytes.len()
    )?;
    stream.write_all(&bytes)
}

enum Reply {
    Json(Value),
    Png(Vec<u8>),
}

fn route(app: &AppHandle, path: &str, body: &Value) -> Result<Reply, String> {
    let window = app
        .get_webview_window(crate::MAIN_WINDOW)
        .ok_or("No window.")?;
    let text = |key: &str| body[key].as_str().unwrap_or("").to_string();
    match path {
        // A JavaScript expression; its value comes back as JSON.
        "/eval" => {
            let script = format!(
                "(() => {{ try {{ return {{ value: ({}) }}; }} catch (error) {{ return {{ error: String(error) }}; }} }})()",
                text("js")
            );
            let (send, receive) = mpsc::channel();
            window
                .eval_with_callback(script, move |result| {
                    let _ = send.send(result);
                })
                .map_err(|error| error.to_string())?;
            let result = receive
                .recv_timeout(WAIT)
                .map_err(|_| "The page did not answer.")?;
            serde_json::from_str(&result)
                .map(Reply::Json)
                .map_err(|error| error.to_string())
        }
        "/click" => {
            let (x, y) = (
                body["x"].as_f64().unwrap_or(0.0),
                body["y"].as_f64().unwrap_or(0.0),
            );
            on_main(&window, move |webview| unsafe { mac::click(webview, x, y) })
                .map(|()| Reply::Json(json!({})))
        }
        // The window comes forward; the page has focus once document.hasFocus() says so.
        "/focus" => {
            let (send, receive) = mpsc::channel();
            window
                .with_webview(move |webview| {
                    let window = mac::parts(&webview).0;
                    mac::bring_forward(window);
                    let _ = send.send(window.isKeyWindow());
                })
                .map_err(|error| error.to_string())?;
            let key = receive
                .recv_timeout(WAIT)
                .map_err(|_| "The window did not answer.")?;
            Ok(Reply::Json(json!({ "key": key })))
        }
        "/type" => {
            let (typed, key) = (text("text"), text("key"));
            on_main(&window, move |webview| unsafe {
                mac::type_keys(webview, &typed, &key)
            })
            .map(|()| Reply::Json(json!({})))
        }
        "/snapshot" => {
            let (send, receive) = mpsc::channel();
            window
                .with_webview(move |webview| unsafe { mac::snapshot(webview, send) })
                .map_err(|error| error.to_string())?;
            receive
                .recv_timeout(WAIT)
                .map_err(|_| "No picture came back.".to_string())?
                .map(Reply::Png)
        }
        "/opened" => Ok(Reply::Json(json!(std::mem::take(
            &mut *OPENED.lock().unwrap()
        )))),
        _ => Err(format!("No route {path}.")),
    }
}

/// Run on the main thread with the window's webview; wait for the result.
fn on_main(
    window: &tauri::WebviewWindow,
    work: impl FnOnce(tauri::webview::PlatformWebview) -> Result<(), String> + Send + 'static,
) -> Result<(), String> {
    let (send, receive) = mpsc::channel();
    window
        .with_webview(move |webview| {
            let _ = send.send(work(webview));
        })
        .map_err(|error| error.to_string())?;
    receive
        .recv_timeout(WAIT)
        .map_err(|_| "The window did not answer.")?
}

mod mac {
    use std::sync::mpsc::Sender;

    use block2::RcBlock;
    use objc2_app_kit::{
        NSApplication, NSBitmapImageFileType, NSBitmapImageRep, NSEvent, NSEventModifierFlags,
        NSEventType, NSImage, NSView, NSWindow,
    };
    use objc2_foundation::{
        MainThreadMarker, NSDictionary, NSError, NSPoint, NSProcessInfo, NSString,
    };
    use objc2_web_kit::WKWebView;
    use tauri::webview::PlatformWebview;

    pub fn parts(webview: &PlatformWebview) -> (&'static NSWindow, &'static WKWebView) {
        unsafe {
            (
                &*webview.ns_window().cast::<NSWindow>(),
                &*webview.inner().cast::<WKWebView>(),
            )
        }
    }

    pub fn bring_forward(window: &NSWindow) {
        if let Some(main) = MainThreadMarker::new() {
            #[allow(deprecated)]
            NSApplication::sharedApplication(main).activateIgnoringOtherApps(true);
        }
        window.makeKeyAndOrderFront(None);
    }

    fn now() -> f64 {
        NSProcessInfo::processInfo().systemUptime()
    }

    /// A left click at a point of the page, in CSS pixels from its top left.
    pub unsafe fn click(webview: PlatformWebview, x: f64, y: f64) -> Result<(), String> {
        let (window, view) = parts(&webview);
        let view: &NSView = view;
        let local = if view.isFlipped() {
            NSPoint::new(x, y)
        } else {
            NSPoint::new(x, view.frame().size.height - y)
        };
        let point = view.convertPoint_toView(local, None);
        for kind in [
            NSEventType::MouseMoved,
            NSEventType::LeftMouseDown,
            NSEventType::LeftMouseUp,
        ] {
            let event = NSEvent::mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure(
                kind,
                point,
                NSEventModifierFlags::empty(),
                now(),
                window.windowNumber(),
                None,
                0,
                1,
                if kind == NSEventType::LeftMouseUp { 0.0 } else { 1.0 },
            )
            .ok_or("Could not make a mouse event.")?;
            window.sendEvent(&event);
        }
        Ok(())
    }

    /// Type text one key at a time, or press one named key (Enter, Escape, Backspace, Tab).
    pub unsafe fn type_keys(webview: PlatformWebview, text: &str, key: &str) -> Result<(), String> {
        let (window, _) = parts(&webview);
        let keys: Vec<(String, u16)> = match key {
            "" => text.chars().map(|c| (c.to_string(), 0)).collect(),
            "Enter" => vec![("\r".into(), 36)],
            "Escape" => vec![("\u{1b}".into(), 53)],
            "Backspace" => vec![("\u{7f}".into(), 51)],
            "Tab" => vec![("\t".into(), 48)],
            other => return Err(format!("No key {other}.")),
        };
        for (characters, code) in keys {
            let characters = NSString::from_str(&characters);
            for kind in [NSEventType::KeyDown, NSEventType::KeyUp] {
                let event = NSEvent::keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode(
                    kind,
                    NSPoint::new(0.0, 0.0),
                    NSEventModifierFlags::empty(),
                    now(),
                    window.windowNumber(),
                    None,
                    &characters,
                    &characters,
                    false,
                    code,
                )
                .ok_or("Could not make a key event.")?;
                window.sendEvent(&event);
            }
        }
        Ok(())
    }

    /// WebKit's own picture of the page, as PNG.
    pub unsafe fn snapshot(webview: PlatformWebview, send: Sender<Result<Vec<u8>, String>>) {
        let (_, view) = parts(&webview);
        let done = RcBlock::new(move |image: *mut NSImage, _error: *mut NSError| {
            let png = image
                .as_ref()
                .and_then(|image| image.TIFFRepresentation())
                .and_then(|tiff| NSBitmapImageRep::imageRepWithData(&tiff))
                .and_then(|rep| {
                    rep.representationUsingType_properties(
                        NSBitmapImageFileType::PNG,
                        &NSDictionary::new(),
                    )
                })
                .map(|data| data.to_vec())
                .ok_or_else(|| "WebKit gave no picture.".to_string());
            let _ = send.send(png);
        });
        view.takeSnapshotWithConfiguration_completionHandler(None, &done);
    }
}
