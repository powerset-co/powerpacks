/* Bring the app that started this session back to the front.
 *
 * macOS gives every process the bundle id of the app that launched it
 * (Terminal, Ghostty, the Claude or Codex desktop app) in __CFBundleIdentifier.
 * Call this after closing a login window so the user lands back where they were
 * instead of on whatever window macOS picks next.
 *
 * Created: 2026-10-03
 */
const { execFileSync } = require("child_process");

function returnFocus() {
  const bundle = process.env.__CFBundleIdentifier;
  if (!bundle) return;
  try {
    execFileSync("open", ["-b", bundle], { timeout: 5000 });
  } catch (_) {
    // focus is a nicety; the login already succeeded
  }
}

module.exports = { returnFocus };
