/* The three measurements behind every wait on a Google page: is the page loading, is the
 * network alive, is the person still there. Not `networkidle`: Google keeps long-polling and
 * analytics connections open, so that state never arrives and every use of it burned its cap.
 *
 * Created: 2026-10-28
 */
const SETTLE_MS = 500; // two reads this far apart that agree: the page stopped changing
const DEAD_MS = 60000; // the network's fault, and nothing answered for this long since
// Chrome's own words for "no network" in request.failure().errorText. ERR_ABORTED is not one:
// that is a navigation cancelling the old page's requests.
const NO_NETWORK = /ERR_(INTERNET_DISCONNECTED|NAME_NOT_RESOLVED|ADDRESS_UNREACHABLE|NETWORK_CHANGED|CONNECTION_TIMED_OUT|TIMED_OUT|PROXY_CONNECTION_FAILED)/;

const watches = new WeakMap();

// One watch per page, fed by its network events. `document` is the page's own request still in
// flight: until it answers, the old page stays on screen, "complete" and unchanging, so a read
// cannot tell that the next page is loading; the watch can.
function networkWatch(page) {
  if (watches.has(page)) return watches.get(page);
  const watch = {
    lastResponseAt: Date.now(), lastFailure: null, failures: 0, firstFailureAt: 0, document: null,
    // Dead: a failure the network caused, then nothing answered for 60 s. The page's own
    // navigation failing is enough on its own (Chrome shows "No internet" and that page sends
    // nothing more, so no further failures can come); background traffic needs three, because
    // one aborted fetch on a Wi-Fi change is a blip while the person types.
    dead() {
      const failure = watch.lastFailure;
      return Boolean(failure) && Date.now() - watch.firstFailureAt >= DEAD_MS && (failure.document || watch.failures >= 3);
    },
  };
  const own = request => request.isNavigationRequest() && request.frame() === page.mainFrame();
  page.on("request", request => { if (own(request)) watch.document = request; });
  page.on("response", response => {
    watch.lastResponseAt = Date.now();
    watch.failures = 0;
    watch.lastFailure = null;
    if (response.request() === watch.document) watch.document = null;
  });
  page.on("requestfailed", request => {
    if (request === watch.document) watch.document = null;
    const errorText = (request.failure() || {}).errorText || "";
    if (!NO_NETWORK.test(errorText)) return;
    if (watch.failures === 0) watch.firstFailureAt = Date.now();
    watch.failures += 1;
    watch.lastFailure = { at: Date.now(), errorText, document: own(request) };
  });
  watches.set(page, watch);
  return watch;
}

// What the page shows (URL, then text), or null while it is loading: its own request in
// flight, the document not complete, or the execution context destroyed under the read (a
// navigation landed). The text is compared, never acted on.
async function read(page) {
  if (networkWatch(page).document) return null;
  try {
    return await page.evaluate(() => (document.readyState === "complete" ? `${location.href}\n${document.body.innerText}` : null));
  } catch (_) {
    return null;
  }
}

// Resolves with what the page shows once two reads 500 ms apart agree; null when it could not
// settle by `until` (an absolute time), because the window closed, or because the network died.
async function settled(page, until = Infinity) {
  const watch = networkWatch(page);
  let last = null;
  while (!page.isClosed() && !watch.dead() && Date.now() < until) {
    const now = await read(page);
    if (now !== null && now === last) return now;
    last = now;
    await new Promise(resolve => setTimeout(resolve, SETTLE_MS));
  }
  return null;
}

// One wait, three measurements. {status:"ok"} once `done(url)`; {status:"act"} when a settled
// page is the script's to read and click once (headless); needs_user_action with the reason
// otherwise: the network died, the window closed, nothing changed on a settled page for
// idleMs (human), or the deadline passed (the backstop).
async function waitUntil(page, { done, human = false, idleMs = 5 * 60000, deadlineMs = 15 * 60000, onProgress = () => {} }) {
  const watch = networkWatch(page);
  const deadline = Date.now() + deadlineMs;
  const stop = message => ({ status: "needs_user_action", message });
  let url = page.url();
  let shown = null;
  let since = Date.now();
  while (Date.now() < deadline) {
    const now = await settled(page, deadline);
    if (page.isClosed()) return stop("Google sign-in did not finish: the Chrome window was closed. Retry to reopen it.");
    if (watch.dead()) return stop("Powerpacks cannot reach Google: check the network, then retry.");
    if (now === null) break;
    if (page.url() !== url) {
      url = page.url();
      onProgress(url);
    }
    if (done(url)) return { status: "ok" };
    if (!human) return { status: "act" };
    if (now !== shown) {
      shown = now;
      since = Date.now();
    } else if (Date.now() - since >= idleMs) {
      return stop(`Google sign-in did not finish: nothing happened in the Chrome window for ${Math.round(idleMs / 60000)} minutes. Retry to reopen it.`);
    }
  }
  return stop(`Google sign-in did not finish in ${Math.round(deadlineMs / 60000)} minutes. Retry to reopen Chrome.`);
}

module.exports = { networkWatch, settled, waitUntil };
