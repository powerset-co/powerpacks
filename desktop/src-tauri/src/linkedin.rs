//! Read the user's LinkedIn connections in the sign-in view, once they are signed in there.
//!
//! The Python importer (packs/ingestion/primitives/discover/linkedin/connections.py) leaves a
//! request (`app-read-request.json`: known slugs, when to stop, how far to scroll); this scrolls
//! the connections list in the view with the same page script the Chrome importer used, reports
//! progress, and leaves the result as `app-read.json` in the same shape the script produced.
//! Page scripts cannot be awaited across the native boundary, so the script writes into
//! `window.__pp` and this polls it.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{mpsc, Arc, Mutex};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::{AppHandle, Emitter, Manager, Url};

use crate::signin;

pub const PROGRESS_EVENT: &str = "linkedin://progress";
const REQUEST: &str = ".powerpacks/network-import/discover/linkedin/app-read-request.json";
const RESULT: &str = ".powerpacks/network-import/discover/linkedin/app-read.json";
pub const CONNECTIONS_URL: &str = "https://www.linkedin.com/mynetwork/invite-connect/connections/";
const CARD_LINK: &str = r#"main a[href*="/in/"]"#;
const POLL: Duration = Duration::from_millis(1000);
const CARDS_TIMEOUT: Duration = Duration::from_secs(60);
const EVAL_TIMEOUT: Duration = Duration::from_secs(10);
/// One load is a 0.5–1.5 s pause plus page time; the deadline leaves room for every load.
const SECONDS_PER_LOAD: u64 = 4;

#[derive(Deserialize)]
struct Request {
    known: Vec<String>,
    stop_after_known: u32,
    max_loads: u32,
}

#[derive(Clone, Serialize)]
pub struct Progress {
    pub read: u64,
    pub total: u64,
}

/// `JSON.stringify(expr)` in the view; `null` when the expression is unset.
fn eval(app: &AppHandle, expression: &str) -> Result<Value, String> {
    let view = app
        .get_webview(signin::LABEL)
        .ok_or("The sign-in view is closed.")?;
    let (sender, receiver) = mpsc::channel();
    let sender = Arc::new(Mutex::new(Some(sender)));
    view.eval_with_callback(
        // Parenthesised: JavaScript rejects `a && b ?? null`.
        format!("JSON.stringify(({expression}) ?? null)"),
        move |text| {
            if let Some(sender) = sender.lock().expect("eval sender").take() {
                let _ = sender.send(text);
            }
        },
    )
    .map_err(|error| error.to_string())?;
    let text = receiver
        .recv_timeout(EVAL_TIMEOUT)
        .map_err(|_| "LinkedIn's page did not answer.")?;
    // The callback gets the JSON of the result, which is itself a JSON string.
    let inner: String = serde_json::from_str(&text).unwrap_or(text);
    Ok(serde_json::from_str(&inner).unwrap_or(Value::Null))
}

fn run(app: &AppHandle, script: &str) -> Result<(), String> {
    let view = app
        .get_webview(signin::LABEL)
        .ok_or("The sign-in view is closed.")?;
    view.eval(script).map_err(|error| error.to_string())
}

/// Poll `expression` until it is not null, or `timeout` passes.
fn wait_for(
    app: &AppHandle,
    expression: &str,
    timeout: Duration,
    mut on_tick: impl FnMut(&AppHandle),
) -> Result<Value, String> {
    let deadline = Instant::now() + timeout;
    loop {
        // No answer is "not ready": before the view's first page commits, wry queues scripts
        // and drops their callbacks, and a page mid-navigation answers late.
        let value = eval(app, expression).unwrap_or(Value::Null);
        if !value.is_null() {
            return Ok(value);
        }
        if Instant::now() > deadline {
            return Err("LinkedIn did not show the connections list.".into());
        }
        on_tick(app);
        std::thread::sleep(POLL);
    }
}

/// LinkedIn's own count at the top of the list ("1,234 connections").
const TOTAL_SCRIPT: &str = r#"
(() => {
  const text = [...document.querySelectorAll("main *")]
    .map((element) => (element.childElementCount ? "" : element.textContent.trim()))
    .find((text) => /^[\d,.]+ connections?$/i.test(text));
  return text ? Number(text.replace(/\D/g, "")) : null;
})()"#;

/// The scroll loop and card reader from the Chrome importer, writing into window.__pp.
fn scroll_script(request: &Request) -> String {
    format!(
        r#"
window.__pp = {{ read: 0, done: null, rows: null }};
(async () => {{
  const cardLink = {card_link:?};
  const knownSet = new Set({known});
  const stopAfterKnown = {stop_after_known};
  const maxLoads = {max_loads};
  const slugs = () => {{
    const seen = new Set();
    for (const a of document.querySelectorAll(cardLink)) {{
      const m = (a.getAttribute("href") || "").match(/\/in\/([^/?#]+)/);
      if (m) seen.add(decodeURIComponent(m[1]).toLowerCase());
    }}
    return seen;
  }};
  const reachedKnown = () => stopAfterKnown > 0
    && [...slugs()].filter((slug) => knownSet.has(slug)).length >= stopAfterKnown;
  const showMore = () => [...document.querySelectorAll("main button")]
    .find((b) => /show more|load more/i.test(b.innerText || ""));
  const main = document.querySelector("main");
  const scroller = main.scrollHeight > main.clientHeight ? main : document.scrollingElement;
  let lastGrowth = performance.now();
  let loads = 0;
  let stopped = "limit";
  while (loads < maxLoads) {{
    if (reachedKnown()) {{ stopped = "known"; break; }}
    const before = slugs().size;
    const height = scroller.scrollHeight;
    scroller.scrollTop = scroller.scrollHeight;
    const button = showMore();
    if (button) button.click();
    loads += 1;
    window.__pp.read = slugs().size;
    await new Promise((resolve) => setTimeout(resolve, 500 + Math.random() * 1000));
    if (scroller.scrollHeight !== height || slugs().size !== before) lastGrowth = performance.now();
    else if (performance.now() - lastGrowth >= 15000) {{ stopped = "end"; break; }}
  }}
  if (loads >= maxLoads && reachedKnown()) stopped = "known";
  const rows = [];
  const seen = new Set();
  for (const a of document.querySelectorAll(cardLink)) {{
    const m = (a.getAttribute("href") || "").match(/\/in\/([^/?#]+)/);
    if (!m || seen.has(m[1])) continue;
    seen.add(m[1]);
    const card = a.closest("li") || a.parentElement;
    const lines = (card ? card.innerText : "").split("\n").map((line) => line.trim()).filter(Boolean);
    let connectedOn = "";
    let headline = "";
    for (const line of lines.slice(1)) {{
      const date = line.match(/^connected on (.+)$/i);
      if (date) connectedOn = date[1];
      else if (line.length > headline.length) headline = line;
    }}
    rows.push({{ slug: m[1], name: lines[0] || "", headline, connected_on: connectedOn }});
  }}
  let owner = "";
  try {{
    const me = await fetch("https://www.linkedin.com/in/me/", {{ redirect: "follow" }});
    const m = me.url.match(/\/in\/([^/?#]+)/);
    owner = m ? m[1] : "";
  }} catch (error) {{}}
  window.__pp.rows = rows;
  window.__pp.done = {{ loads, stopped, owner_slug: owner }};
}})();
"#,
        card_link = CARD_LINK,
        known = serde_json::to_string(&request.known).expect("known slugs"),
        stop_after_known = request.stop_after_known,
        max_loads = request.max_loads,
    )
}

fn read_request(root: &Path) -> Result<Request, String> {
    let text = fs::read_to_string(root.join(REQUEST))
        .map_err(|_| "Powerpacks has not asked for a LinkedIn read.")?;
    serde_json::from_str(&text).map_err(|error| format!("Bad LinkedIn read request: {error}"))
}

fn result_path(root: &Path) -> PathBuf {
    root.join(RESULT)
}

/// Read the list in the sign-in view and leave the result for the importer. Blocks for the
/// whole read; the page shows `linkedin://progress` meanwhile.
pub fn read(app: &AppHandle, root: &Path) -> Result<Progress, String> {
    let request = read_request(root)?;
    let view = app
        .get_webview(signin::LABEL)
        .ok_or("The sign-in view is closed.")?;
    let on_list = view
        .url()
        .map(|url| url.as_str().starts_with(CONNECTIONS_URL))
        .unwrap_or(false);
    if !on_list {
        let url: Url = CONNECTIONS_URL.parse().expect("connections url");
        view.navigate(url).map_err(|error| error.to_string())?;
    }
    // The view may still show the page it was on; wait for the list itself.
    wait_for(
        app,
        &format!(
            "location.href.startsWith({CONNECTIONS_URL:?}) && document.querySelector({CARD_LINK:?}) && true || null"
        ),
        CARDS_TIMEOUT,
        |_| {},
    )?;
    let total = eval(app, TOTAL_SCRIPT)?
        .as_u64()
        .ok_or("LinkedIn did not show how many connections you have.")?;
    let _ = app.emit(PROGRESS_EVENT, Progress { read: 0, total });

    run(app, &scroll_script(&request))?;
    let timeout = Duration::from_secs(u64::from(request.max_loads) * SECONDS_PER_LOAD + 60);
    let done = wait_for(app, "window.__pp.done", timeout, |app| {
        if let Ok(read) = eval(app, "window.__pp.read") {
            let _ = app.emit(
                PROGRESS_EVENT,
                Progress {
                    read: read.as_u64().unwrap_or(0),
                    total,
                },
            );
        }
    })?;
    let rows = eval(app, "window.__pp.rows")?;
    let read = rows.as_array().map(Vec::len).unwrap_or(0) as u64;
    let payload = json!({
        "status": "ok",
        "connections": rows,
        "total": total,
        "loads": done.get("loads").cloned().unwrap_or(json!(0)),
        "stopped": done.get("stopped").cloned().unwrap_or(json!("limit")),
        "owner_slug": done.get("owner_slug").cloned().unwrap_or(json!("")),
    });
    let path = result_path(root);
    fs::create_dir_all(path.parent().expect("result dir")).map_err(|error| error.to_string())?;
    fs::write(&path, serde_json::to_vec(&payload).expect("result json"))
        .map_err(|error| error.to_string())?;
    let _ = fs::remove_file(root.join(REQUEST));
    Ok(Progress { read, total })
}
