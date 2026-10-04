/* The one place the Chrome automation scripts pick a browser and act like it.
 *
 * Google Chrome is preferred; Brave is the fallback (both are Chromium, so
 * playwright-core drives either). The user agent matches the installed major
 * version so it agrees with the Sec-CH-UA hints the browser sends. After a login
 * window closes, returnFocus brings back the app that started the session:
 * macOS gives every process that app's bundle id in __CFBundleIdentifier
 * (Terminal, Ghostty, the Claude or Codex desktop app).
 *
 * Created: 2026-10-03
 */
const fs = require("fs");
const { execFileSync } = require("child_process");

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const BRAVE = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser";
const FALLBACK_MAJOR = 149;

// Spread into launchPersistentContext options.
function browserTarget() {
  if (fs.existsSync(CHROME)) return { channel: "chrome" };
  if (fs.existsSync(BRAVE)) return { executablePath: BRAVE };
  throw new Error("Install Google Chrome or Brave to continue.");
}

function desktopUserAgent() {
  let major = FALLBACK_MAJOR;
  try {
    const binary = fs.existsSync(CHROME) ? CHROME : BRAVE;
    const match = /(\d+)\.\d+\.\d+/.exec(execFileSync(binary, ["--version"], { encoding: "utf8", timeout: 5000 }));
    if (match) major = Number(match[1]);
  } catch (_) {
    // keep the fallback major
  }
  return `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${major}.0.0.0 Safari/537.36`;
}

function returnFocus() {
  const bundle = process.env.__CFBundleIdentifier;
  if (!bundle) return;
  try {
    execFileSync("open", ["-b", bundle], { timeout: 5000 });
  } catch (_) {
    // focus is a nicety; the login already succeeded
  }
}

module.exports = { browserTarget, desktopUserAgent, returnFocus };
