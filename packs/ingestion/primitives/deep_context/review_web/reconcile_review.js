const toast = document.querySelector(".toast");
const stage = document.querySelector(".stage");

function announce(message, isError = false) {
  if (!toast) return;
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.add("show");
  window.clearTimeout(announce.timer);
  // Errors stay long enough to actually read (auth hints, network failures).
  announce.timer = window.setTimeout(
    () => toast.classList.remove("show"), isError ? 6000 : 1800);
}

function lock(button) {
  document.querySelectorAll("button").forEach((item) => { item.disabled = true; });
  button?.setAttribute("aria-busy", "true");
}

function unlock(button) {
  document.querySelectorAll("button").forEach((item) => { item.disabled = false; });
  button?.removeAttribute("aria-busy");
}

async function post(path, values) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(values),
  });
  if (!response.ok) {
    // Error bodies may be JSON payloads ({status, error}) — surface the human
    // message ("not signed in to Powerset; run $powerset login first"), never
    // the raw JSON blob. The status rides on the Error so callers can react
    // (needs_auth -> offer the browser sign-in).
    const text = (await response.text()) || "Could not save";
    let message = text;
    let status = "";
    try {
      const payload = JSON.parse(text);
      message = payload.error || payload.status || text;
      status = payload.status || "";
    } catch { /* plain-text error body */ }
    const error = new Error(message);
    error.status = status;
    throw error;
  }
  return response.json();
}

// Optional-feedback popover, mirrored off the network-search-app
// FeedbackForm: context label, auto-grow textarea, ⌘+Enter, send icon,
// then a "Got it, thanks!" beat before it closes. Posts to /feedback where
// the server folds in everything it knows (incl. retarget guidance).
// Module scope: the directory person pane AND the review cards both open it.
const SEND_ICON = "<svg viewBox='0 0 24 24' width='14' height='14' fill='none'"
  + " stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>"
  + "<path d='m22 2-7 20-4-9-9-4Z'/><path d='M22 2 11 13'/></svg>";
const CHECK_ICON = "<svg viewBox='0 0 24 24' width='14' height='14' fill='none'"
  + " stroke='currentColor' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'>"
  + "<path d='M20 6 9 17l-5-5'/></svg>";

function closeFeedbackPopover() {
  document.querySelector(".feedback-popover")?.remove();
}

// needs_auth recovery: one click starts auth.py's browser sign-in flow on
// this machine (used by the feedback popover and the retarget panel alert).
function signInButton(doneHint) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "feedback-login";
  button.textContent = "Sign in to Powerset";
  button.addEventListener("click", async () => {
    button.disabled = true;
    button.textContent = "Waiting for sign-in…";
    try {
      await post("/auth/login", {});
      announce(`Sign-in opened in your browser — finish there${doneHint}.`);
    } catch (error) {
      announce(error.message, true);
      button.disabled = false;
      button.textContent = "Sign in to Powerset";
    }
  });
  return button;
}

function offerSignIn(pop) {
  if (pop.querySelector(".feedback-login")) return;
  pop.append(signInButton(", then Send again"));
}

function feedbackPopover({ anchor, contextLabel, pub, slug, action, onDone }) {
  closeFeedbackPopover();
  const host = anchor.closest(".person-detail, .identity-card") || document.body;
  const pop = document.createElement("div");
  pop.className = "feedback-popover";
  if (contextLabel) {
    const label = document.createElement("p");
    label.className = "feedback-context";
    label.textContent = contextLabel;
    pop.append(label);
  }
  const textarea = document.createElement("textarea");
  textarea.rows = 2;
  textarea.maxLength = 4000;
  textarea.placeholder = 'e.g. "Wrong person — this is actually Jane Smith"';
  const footer = document.createElement("div");
  footer.className = "feedback-footer";
  footer.innerHTML = `<span class='feedback-hint'>&#8629; &#8984;+Enter</span>`
    + `<span class='feedback-actions'>`
    + `<button type='button' class='feedback-skip'>Skip</button>`
    + `<button type='button' class='feedback-send' aria-label='Send feedback' disabled>${SEND_ICON}</button>`
    + `</span>`;
  pop.append(textarea, footer);
  const send = footer.querySelector(".feedback-send");
  const skip = footer.querySelector(".feedback-skip");

  // Every way out lands here exactly once: send (after the thanks beat),
  // Skip, Escape, or clicking away. The caller's onDone applies the move.
  let settled = false;
  function finish() {
    if (settled) return;
    settled = true;
    document.removeEventListener("click", away);
    pop.remove();
    onDone?.();
  }

  async function submit() {
    const comment = textarea.value.trim();
    if (!comment || settled) return;
    send.disabled = true;
    skip.disabled = true;
    try {
      await post("/feedback", { pub, parent_slug: slug, comment, action });
    } catch (error) {
      announce(error.message, true);
      if (error.status === "needs_auth") offerSignIn(pop);
      send.disabled = false;
      skip.disabled = false;
      return;
    }
    settled = true;
    document.removeEventListener("click", away);
    pop.replaceChildren();
    pop.className = "feedback-popover feedback-done";
    pop.innerHTML = `<span class='feedback-done-badge'>${CHECK_ICON}</span>`
      + "<p>Got it, thanks! \u{1F64F}</p>";
    setTimeout(() => { pop.remove(); onDone?.(); }, 900);
  }

  textarea.addEventListener("input", () => {
    send.disabled = !textarea.value.trim();
    textarea.style.height = "auto";
    textarea.style.height = Math.min(textarea.scrollHeight, 140) + "px";
  });
  textarea.addEventListener("keydown", (event) => {
    event.stopPropagation();
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      void submit();
    }
    if (event.key === "Escape") finish();
  });
  send.addEventListener("click", () => void submit());
  skip.addEventListener("click", finish);
  pop.addEventListener("click", (event) => event.stopPropagation());

  host.append(pop);
  const hostRect = host.getBoundingClientRect();
  const anchorRect = anchor.getBoundingClientRect();
  pop.style.top = `${anchorRect.bottom - hostRect.top + host.scrollTop + 8}px`;
  pop.style.right = `${Math.max(8, hostRect.right - anchorRect.right)}px`;
  setTimeout(() => textarea.focus(), 80);
  function away(event) {
    if (!document.body.contains(pop)) {
      document.removeEventListener("click", away);
      return;
    }
    if (!pop.contains(event.target) && event.target !== anchor) finish();
  }
  setTimeout(() => document.addEventListener("click", away), 0);
}

async function fetchText(path) {
  try {
    const response = await fetch(path, { cache: "no-store" });
    if (!response.ok) return null;
    return await response.text();
  } catch {
    return null;
  }
}

const delay = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));

function leaveAndReload(message) {
  announce(message);
  stage?.classList.add("leaving");
  window.setTimeout(() => window.location.reload(), 170);
}

function leaveAndNavigate(message, url) {
  announce(message);
  stage?.classList.add("leaving");
  window.setTimeout(() => { window.location.href = url; }, 170);
}

// --- optimistic decision plumbing --------------------------------------------
// Decision clicks no longer reload the page: the card/row animates away at once,
// the POST runs in the background, and badges/steps are first bumped locally and
// then corrected from the response's authoritative progress counts.

function tabCountSpan(key) {
  return document.querySelector(`.decision-tab[data-tab='${key}'] span`);
}

function bumpTabCount(key, delta) {
  const span = tabCountSpan(key);
  if (!span) return;
  const current = parseInt(span.textContent || "0", 10);
  if (!Number.isNaN(current)) span.textContent = String(Math.max(0, current + delta));
}

function setTabCount(key, value) {
  const span = tabCountSpan(key);
  if (span && value !== undefined && value !== null) span.textContent = String(value);
}

function updateStepCount(step, count) {
  const small = step?.querySelector("small");
  if (!small) return;
  if (count) small.textContent = `${count} left`;
  else small.remove();
}

function applyProgress(progress) {
  if (!progress) return;
  setTabCount("review", progress.worth_pending);
  setTabCount("yes", progress.worth_yes);
  setTabCount("no", progress.worth_no);
  const steps = document.querySelectorAll(".stepper .step");
  updateStepCount(steps[0], progress.worth_pending);
  updateStepCount(steps[2], progress.linkedin_pending);
}

// Local mutation responses already carry the authoritative token. Status polling
// is reserved for external agent/provider handoffs; never re-poll after our own save.
function adoptMutationState(response) {
  if (response?.state_token) reviewStateToken = response.state_token;
}

async function decideDecisionRow(button, row) {
  const worth = button.dataset.worth;          // the pile this row moves to
  const from = worth === "yes" ? "no" : "yes"; // the pile it is leaving
  row.querySelectorAll("button").forEach((item) => { item.disabled = true; });
  row.classList.add("leaving");
  bumpTabCount(from, -1);
  bumpTabCount(worth, 1);
  try {
    const [response] = await Promise.all([
      post("/worth", { pub: button.dataset.pub || "", worth,
                       parent_slug: button.dataset.parent || "" }),
      delay(170),
    ]);
    adoptMutationState(response);
    const list = row.closest("[data-decision-list]");
    if (list && typeof list.virtualRemove === "function") list.virtualRemove(row);
    else row.remove();
    applyProgress(response.progress);
    announce(worth === "yes" ? "Added" : "Rejected");
  } catch (error) {
    row.classList.remove("leaving");
    row.querySelectorAll("button").forEach((item) => { item.disabled = false; });
    bumpTabCount(from, 1);
    bumpTabCount(worth, -1);
    announce(error.message, true);
  }
}

// --- worth queue prefetch ----------------------------------------------------
// The NEXT card is fetched while the user reads the current one, so a decision
// swaps instantly instead of serializing behind the POST (which rewrites
// review.csv and can take hundreds of ms on large datasets). `exclude` carries
// the current card plus any in-flight decisions so the server's pick is
// race-free without waiting for those saves to land.
const inFlightWorth = new Set();
let worthPrefetch = null; // { promise } for the card AFTER the one on screen

function prefetchWorthCard(currentPub) {
  const exclude = [...inFlightWorth];
  if (currentPub) exclude.push(currentPub);
  worthPrefetch = {
    promise: fetchText(`/api/worth-card?exclude=${encodeURIComponent(exclude.join(","))}`),
  };
}

async function decideWorthCard(button, card) {
  const worth = button.dataset.worth;
  const pub = button.dataset.pub || "";
  // The optional collapsed "why" box: whatever is in it when Yes/No lands
  // rides along with the decision (saved to review.csv, filed as feedback).
  const note = (card.querySelector("[data-worth-note]")?.value || "").trim();
  card.querySelectorAll("button").forEach((item) => { item.disabled = true; });
  card.classList.add("leaving");
  bumpTabCount("review", -1); // leaves the Review queue for the yes/no pile
  bumpTabCount(worth, 1);
  inFlightWorth.add(pub);
  const panel = card.closest(".worth-panel");
  const oldHtml = panel ? panel.innerHTML : null;
  // parent_slug pins the patch to the exact parent this card was rendered
  // from — a worth key alone is ambiguous when split parents share a pub
  const postPromise = post("/worth", {
    pub, worth, parent_slug: button.dataset.parent || "", note,
  }); // fire-and-track, no await
  postPromise.finally(() => inFlightWorth.delete(pub));
  const prefetched = worthPrefetch?.promise
    || fetchText(`/api/worth-card?exclude=${encodeURIComponent(pub)}`);
  worthPrefetch = null; // consumed — the swap re-prefetches for the new card
  try {
    const [nextHtml] = await Promise.all([prefetched, delay(170)]);
    if (!panel || nextHtml === null) {
      // Could not swap in the next card: fall back to the serialized save+reload.
      const response = await postPromise;
      adoptMutationState(response);
      leaveAndReload("Saved");
      return;
    }
    panel.innerHTML = nextHtml; // next queue card, or the Decisions-ready state
    wireDynamicContent(panel);  // also prefetches the card after this one
    maybeAutoComplete(panel);
    postPromise.then((response) => {
      adoptMutationState(response);
      applyProgress(response.progress);
      pruneWorthPending(pub); // the settled decision leaves the typeahead's queue
      announce(worth === "yes" ? "Added" : "Rejected");
      if (Number(response.progress?.worth_pending) === 0) {
        leaveAndNavigate("People complete", "/?stage=enrich");
      }
    }).catch((error) => {
      // The save failed after the optimistic swap: restore the undecided card.
      if (panel && oldHtml !== null) {
        panel.innerHTML = oldHtml;
        wireDynamicContent(panel);
      }
      bumpTabCount("review", 1);
      bumpTabCount(worth, -1);
      announce(error.message, true);
    });
  } catch (error) {
    try {
      const response = await postPromise; // next-card fetch failed; save may still land
      adoptMutationState(response);
      applyProgress(response.progress);
      leaveAndReload("Saved");
    } catch (postError) {
      card.classList.remove("leaving");
      card.querySelectorAll("button").forEach((item) => { item.disabled = false; });
      bumpTabCount("review", 1);
      bumpTabCount(worth, -1);
      announce(postError.message, true);
    }
  }
}

// --- worth live search ---------------------------------------------------------
// ONE input shared by every worth view (filters as you type; no Search button).
// On the Yes/No tables it hides non-matching rows client-side with an "N of M"
// count. On the Review card view it is a typeahead over the pending queue the
// server embedded at render time; picking a name fetches that person's card
// through the same lock-free /api/worth-card path the prefetch uses and swaps
// it in — the current card stays visible until the selection lands.
let worthPendingNames = null; // [{key, name}] — pruned as decisions settle

function pruneWorthPending(key) {
  if (!worthPendingNames) return;
  const lower = (key || "").toLowerCase();
  worthPendingNames = worthPendingNames.filter(
    (entry) => (entry.key || "").toLowerCase() !== lower,
  );
}

async function jumpToWorthCard(key) {
  const panel = document.querySelector(".worth-panel");
  if (!panel) return;
  if (inFlightWorth.has(key)) {
    // Its decision is already saving: treat it as decided, keep the card.
    pruneWorthPending(key);
    announce("Already decided");
    return;
  }
  let response;
  try {
    response = await fetch(`/api/worth-card?pick=${encodeURIComponent(key)}`, { cache: "no-store" });
  } catch {
    announce("Could not load card", true);
    return;
  }
  if (response.status === 404) {
    // No longer pending (decided elsewhere / stale): prune locally, keep the
    // current card, no error dialog.
    pruneWorthPending(key);
    announce("Already decided");
    return;
  }
  if (!response.ok) {
    announce("Could not load card", true);
    return;
  }
  const nextHtml = await response.text();
  panel.querySelector("[data-card]")?.classList.add("leaving");
  await delay(170);
  panel.innerHTML = nextHtml; // the picked card, via the existing swap path
  maybeAutoComplete(panel);
  wireDynamicContent(panel);  // re-prefetches with the picked card excluded
}

function wireWorthTypeahead(box, input) {
  const listbox = box.querySelector("[data-search-list]");
  const island = box.querySelector("script[data-worth-pending]");
  if (!listbox || !island) return;
  if (worthPendingNames === null) {
    try {
      worthPendingNames = JSON.parse(island.textContent || "[]");
    } catch {
      worthPendingNames = [];
    }
  }
  let matches = [];
  let active = -1;

  function close(clear = false) {
    listbox.hidden = true;
    listbox.textContent = "";
    matches = [];
    active = -1;
    if (clear) input.value = "";
  }

  function select(index) {
    const entry = matches[index];
    if (!entry) return;
    close(true);
    void jumpToWorthCard(entry.key || "");
  }

  function render() {
    const query = input.value.trim().toLowerCase();
    if (!query) {
      close();
      return;
    }
    matches = (worthPendingNames || [])
      .filter((entry) => (entry.name || "").toLowerCase().includes(query))
      .slice(0, 8);
    active = matches.length ? 0 : -1;
    listbox.textContent = "";
    matches.forEach((entry, index) => {
      const item = document.createElement("li");
      item.setAttribute("role", "option");
      item.textContent = entry.name || entry.key || "";
      if (index === active) item.classList.add("active");
      // mousedown beats the input's blur, so a click still selects
      item.addEventListener("mousedown", (event) => {
        event.preventDefault();
        select(index);
      });
      listbox.append(item);
    });
    if (!matches.length) {
      const empty = document.createElement("li");
      empty.className = "worth-search-empty";
      empty.textContent = "No matches";
      listbox.append(empty);
    }
    listbox.hidden = false;
  }

  function highlight(delta) {
    if (!matches.length) return;
    active = (active + delta + matches.length) % matches.length;
    listbox.querySelectorAll("li").forEach((item, index) => {
      item.classList.toggle("active", index === active);
    });
  }

  input.addEventListener("input", render);
  input.addEventListener("focus", render);
  input.addEventListener("blur", () => close());
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      highlight(1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      highlight(-1);
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (active >= 0) select(active);
    } else if (event.key === "Escape") {
      event.preventDefault();
      close(true);
    }
  });
}

function wireWorthTableFilter(box, input) {
  const count = box.querySelector("[data-search-count]");
  input.addEventListener("focus", () => {
    const list = document.querySelector("[data-decision-list]");
    if (!list) return;
    if (typeof list.holdRowsLive === "function") list.holdRowsLive(true);
    if (typeof list.prefetchAllRows === "function") void list.prefetchAllRows();
  });
  input.addEventListener("blur", () => {
    const list = document.querySelector("[data-decision-list]");
    if (list && typeof list.holdRowsLive === "function") list.holdRowsLive(false);
  });
  input.addEventListener("input", async () => {
    const query = input.value.trim().toLowerCase();
    const list = document.querySelector("[data-decision-list]");
    if (!list || typeof list.applyNameFilter !== "function") return;
    const result = await list.applyNameFilter(query);
    if (input.value.trim().toLowerCase() !== query) return; // superseded keystroke
    if (count) {
      count.hidden = !query;
      if (query) count.textContent = `${result.shown} of ${result.total}`;
    }
  });
}

function wireWorthSearch(box) {
  if (box.dataset.wired) return;
  box.dataset.wired = "true";
  const input = box.querySelector("input");
  if (!input) return;
  if (box.dataset.searchView === "review") wireWorthTypeahead(box, input);
  else wireWorthTableFilter(box, input);
}

// --- linkedin queue prefetch -------------------------------------------------
// Same pattern as the worth queue: the next parent's card is fetched while the
// user reads the current one (exclude = current + in-flight PARENT SLUGS — the
// linkedin queue is parent-keyed), so a decision swaps instantly and the
// /decide POST settles in the background. A parent that still has pending
// candidates after a partial decision simply reappears on a later fetch once
// its save lands and it leaves the in-flight set.
const inFlightLinkedin = new Set();
let linkedinPrefetch = null; // { promise } for the card AFTER the one on screen

function prefetchLinkedinCard(currentParent) {
  const exclude = [...inFlightLinkedin];
  if (currentParent) exclude.push(currentParent);
  linkedinPrefetch = {
    promise: fetchText(`/api/linkedin-card?exclude=${encodeURIComponent(exclude.join(","))}`),
  };
}

// ONE guidance box, two vocabularies: "guidance" (No — provide LinkedIn or
// re-research) and "skip" (an optional why-note whose submit button IS the
// skip). The server renders the guidance wording per card; the first morph
// stashes it on the form so switching back restores the card's own copy.
const SKIP_MODE_COPY = {
  label: "Skip — anything we should know? (optional)",
  placeholder: "e.g. 'don't recognize this person' or 'can't tell which is right'",
  button: "Skip",
};

function setGuidanceMode(details, mode, { keepClosed = false } = {}) {
  const form = details.querySelector("[data-retarget-form]");
  const summary = details.querySelector("summary");
  const textarea = form?.querySelector("textarea[name='guidance']");
  const button = form?.querySelector("button[type='submit']");
  if (!form || !summary || !textarea || !button) return;
  if (!form.dataset.guidanceLabel) {
    form.dataset.guidanceLabel = summary.textContent;
    form.dataset.guidancePlaceholder = textarea.placeholder;
    form.dataset.guidanceButton = button.textContent;
  }
  const skip = mode === "skip";
  form.dataset.mode = skip ? "skip" : "";
  summary.textContent = skip ? SKIP_MODE_COPY.label : form.dataset.guidanceLabel;
  textarea.placeholder = skip ? SKIP_MODE_COPY.placeholder : form.dataset.guidancePlaceholder;
  textarea.required = !skip;
  button.textContent = skip ? SKIP_MODE_COPY.button : form.dataset.guidanceButton;
  if (keepClosed) return;
  details.open = true;
  textarea.focus({ preventScroll: true });
}

// Collapsing a skip-morphed box reverts it, so the next open shows the card's
// own guidance wording again ("toggle" does not bubble — capture phase).
document.addEventListener("toggle", (event) => {
  const details = event.target;
  if (!(details instanceof HTMLElement)
      || !details.classList.contains("retarget-guidance") || details.open) return;
  const form = details.querySelector("[data-retarget-form]");
  if (form?.dataset.mode === "skip") setGuidanceMode(details, "guidance", { keepClosed: true });
}, true);

// The review cards' "…" menu (general feedback). The directory pane binds its
// own delegation scoped to the detail pane, so directory clicks are excluded
// here — one popover, never opened twice.
document.addEventListener("click", (event) => {
  if (event.target.closest("[data-directory-detail]")) return;
  const toggle = event.target.closest("[data-menu-toggle]");
  if (toggle) {
    event.preventDefault();
    const items = toggle.parentElement.querySelector(".person-menu-items");
    if (items) items.hidden = !items.hidden;
    return;
  }
  const general = event.target.closest("[data-feedback-general]");
  if (general) {
    event.preventDefault();
    const menu = general.closest("[data-person-menu]");
    menu?.querySelector(".person-menu-items")?.setAttribute("hidden", "");
    const card = general.closest(".identity-card");
    const name = card?.querySelector(".profile-copy h2")?.textContent?.trim() || "this person";
    feedbackPopover({
      anchor: menu || general,
      contextLabel: `Feedback on ${name} — wrong or missing info?`,
      pub: general.dataset.pub || "",
      slug: general.dataset.parent || "",
      action: "general",
    });
    return;
  }
  document.querySelectorAll(".identity-card .person-menu-items:not([hidden])")
    .forEach((el) => { el.hidden = true; });
});

async function decideLinkedinCard(card, values, message) {
  const panel = card.closest("[data-linkedin-panel]");
  const parentSlug = values.parent_slug || card.dataset.parent || "";
  if (!panel) {
    // Markup without the swap panel: serialized save + reload.
    lock(card.querySelector("button"));
    try {
      await post("/decide", values);
      leaveAndReload(message);
    } catch (error) {
      unlock(card.querySelector("button"));
      announce(error.message, true);
    }
    return;
  }
  card.querySelectorAll("button, input").forEach((item) => { item.disabled = true; });
  card.classList.add("leaving");
  inFlightLinkedin.add(parentSlug);
  const oldHtml = panel.innerHTML;
  const postPromise = post("/decide", values); // fire-and-track, no await
  postPromise.finally(() => inFlightLinkedin.delete(parentSlug));
  const prefetched = linkedinPrefetch?.promise
    || fetchText(`/api/linkedin-card?exclude=${encodeURIComponent(parentSlug)}`);
  linkedinPrefetch = null; // consumed — the swap re-prefetches for the new card
  try {
    const [nextHtml] = await Promise.all([prefetched, delay(170)]);
    if (nextHtml === null) {
      // Could not fetch the next card: fall back to the serialized save+reload.
      const response = await postPromise;
      adoptMutationState(response);
      leaveAndReload(message);
      return;
    }
    panel.innerHTML = nextHtml; // next parent's card, or the finished state
    wireDynamicContent(panel);  // also prefetches the card after this one
    postPromise.then((response) => {
      adoptMutationState(response);
      applyProgress(response.progress);
      if (Number(response.progress?.linkedin_pending) === 0) {
        // Last decision: a non-preview page load self-completes the stage
        // server-side and paints the go-back handoff state directly.
        leaveAndNavigate("Review complete", "/?stage=linkedin");
        return;
      }
      announce(message);
    }).catch((error) => {
      // The save failed after the optimistic swap: restore the undecided card.
      panel.innerHTML = oldHtml;
      wireDynamicContent(panel);
      announce(error.message, true);
    });
  } catch (error) {
    try {
      const response = await postPromise; // next-card fetch failed; save may still land
      adoptMutationState(response);
      applyProgress(response.progress);
      leaveAndReload(message);
    } catch (postError) {
      card.classList.remove("leaving");
      card.querySelectorAll("button, input").forEach((item) => { item.disabled = false; });
      announce(postError.message, true);
    }
  }
}

// Debug-only carousel (?debug=1): browse the queue without deciding. Prev/Next
// refetch the card endpoints with an explicit index; nothing is ever written.
async function carouselNav(button) {
  const shell = button.closest("[data-queue-total]");
  if (!shell) return;
  const total = Math.max(1, parseInt(shell.dataset.queueTotal || "1", 10));
  const current = parseInt(shell.dataset.queueIndex || "0", 10) || 0;
  const index = (current + (button.dataset.carousel === "next" ? 1 : total - 1)) % total;
  const path = document.body.dataset.stage === "worth" ? "/api/worth-card" : "/api/linkedin-card";
  const nextHtml = await fetchText(`${path}?debug=1&index=${index}`);
  if (nextHtml === null) {
    announce("Could not load card", true);
    return;
  }
  const template = document.createElement("template");
  template.innerHTML = nextHtml;
  const next = template.content.firstElementChild;
  shell.replaceWith(...template.content.childNodes);
  if (next) wireDynamicContent(next);
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button || button.disabled) return;

  if (button.dataset.carousel) {
    event.preventDefault();
    void carouselNav(button);
    return;
  }

  if (button.hasAttribute("data-copy-continue")) {
    // The end-of-review handoff: hand the user the exact phrase for Codex.
    event.preventDefault();
    try {
      const phrase = button.dataset.phrase || "Review complete proceed with enrichment";
      await navigator.clipboard.writeText(phrase);
      announce(button.dataset.toast || "Copied");
    } catch (error) {
      announce(`Copy failed — type: ${button.dataset.phrase || "the phrase shown"}`, true);
    }
    return;
  }

  if (button.hasAttribute("data-show-more")) {
    // "+ show N more" toggle on Work/Education fact lists.
    event.preventDefault();
    const holder = button.closest("dd") || button.parentElement;
    const expanded = button.dataset.expanded === "true";
    holder?.querySelectorAll("[data-more-item]").forEach((item) => { item.hidden = expanded; });
    button.dataset.expanded = expanded ? "false" : "true";
    button.textContent = expanded
      ? (button.dataset.moreLabel || "+ show more")
      : (button.dataset.lessLabel || "show fewer");
    refreshScrollCues();
    return;
  }

  if (button.dataset.worth) {
    event.preventDefault();
    const row = button.closest("details.decision-row");
    if (row) {
      void decideDecisionRow(button, row);
      return;
    }
    const worthCard = button.closest(".worth-card");
    if (worthCard) {
      void decideWorthCard(button, worthCard);
      return;
    }
    // compatibility markup without a card/row context: keep the reload flow
    lock(button);
    try {
      await post("/worth", { pub: button.dataset.pub || "", worth: button.dataset.worth });
      leaveAndReload(button.dataset.worth === "yes" ? "Added" : "Rejected");
    } catch (error) {
      unlock(button);
      announce(error.message, true);
    }
    return;
  }

  if (button.hasAttribute("data-open-guidance")) {
    // "No" / "None of these" expands the card's guidance box — ONE input owns
    // both paste-the-right-URL (applies directly, no spend) and re-research.
    event.preventDefault();
    const details = button.closest(".identity-decision")?.querySelector(".retarget-guidance");
    if (details instanceof HTMLElement) {
      setGuidanceMode(details, "guidance");
      button.setAttribute("aria-expanded", "true");
    }
    return;
  }

  if (button.hasAttribute("data-open-skip")) {
    // "Skip" opens the SAME guidance box re-worded as an optional why-note;
    // its submit performs the actual skip (detach + sibling withdrawal).
    event.preventDefault();
    const details = button.closest(".identity-decision")?.querySelector(".retarget-guidance");
    if (details instanceof HTMLElement) setGuidanceMode(details, "skip");
    return;
  }

  if (button.dataset.decide) {
    event.preventDefault();
    const values = {
      pub: button.dataset.pub || "",
      decision: button.dataset.decide,
      parent_slug: button.dataset.parent || "",
    };
    const card = button.closest(".identity-card");
    if (card) {
      void decideLinkedinCard(card, values, button.dataset.toast || "Saved");
      return;
    }
    lock(button);
    try {
      await post("/decide", values);
      leaveAndReload(button.dataset.toast || "Saved");
    } catch (error) {
      unlock(button);
      announce(error.message, true);
    }
    return;
  }

  if (button.hasAttribute("data-approve-enrichment")) {
    event.preventDefault();
    lock(button);
    // Same guard as the stage-complete buttons (#291): the approved job's
    // manifest writes rotate the state token DURING this click, and the
    // freshness observer's reload would tear down the page before the POST
    // leaves the browser — the click silently vanishes. Park the observer;
    // the success path reloads deliberately anyway.
    completingStage = true;
    try {
      await post("/approve-enrichment", {});
      leaveAndReload("Approved");
    } catch (error) {
      completingStage = false;
      unlock(button);
      announce(error.message, true);
    }
    return;
  }

  if (button.dataset.complete) {
    event.preventDefault();
    lock(button);
    completingStage = true;
    try {
      await post("/complete", { stage: button.dataset.complete });
      const next = {
        worth: ["People complete", "/?stage=enrich"],
        enrich: ["Enrichment complete", "/?stage=linkedin"],
        // Finish transforms THIS screen into the go-back handoff state —
        // never a surprise jump to the directory.
        linkedin: ["Review complete", "/?stage=linkedin"],
      }[button.dataset.complete] || ["Saved", window.location.href];
      leaveAndNavigate(next[0], next[1]);
    } catch (error) {
      completingStage = false;
      unlock(button);
      announce(error.message, true);
    }
  }
});

// Guided retargets from a review card. The directory pane binds its own submit
// handler (it also refreshes the sidebar queue panel), so directory forms are
// excluded here; this document-level handler covers the LinkedIn review cards,
// which are swapped in as fragments after every decision. LINEAR review:
// queueing removes the person from the queue (the server excludes active
// re-research), so on success the panel advances straight to the next card —
// results apply automatically in the background, and only a failed job brings
// the person back.
document.addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-retarget-form]");
  if (!form || form.closest("[data-directory-detail]")) return;
  event.preventDefault();
  const textarea = form.querySelector("textarea[name='guidance']");
  const guidance = (textarea?.value || "").trim();
  if (form.dataset.mode === "skip") {
    // Skip mode: the submit IS the skip (the same detach + sibling withdrawal
    // the old inline Skip performed); a typed note rides the /decide POST as
    // feedback — one request, nothing to race.
    const values = { pub: form.dataset.pub || "", decision: "detach",
                     parent_slug: form.dataset.parent || "" };
    if (guidance) values.note = guidance;
    const card = form.closest(".identity-card");
    if (card) {
      void decideLinkedinCard(card, values, "Skipped");
      return;
    }
    const button = form.querySelector("button[type='submit']");
    lock(button);
    try {
      await post("/decide", values);
      leaveAndReload("Skipped");
    } catch (error) {
      unlock(button);
      announce(error.message, true);
    }
    return;
  }
  if (!guidance) return;
  const button = form.querySelector("button[type='submit']");
  if (button) button.disabled = true;
  try {
    await post("/retarget", { pub: form.dataset.pub || "",
                              parent_slug: form.dataset.parent || "", guidance });
    announce("Queued for re-research — moving on");
    const card = form.closest(".identity-card");
    const panel = card?.closest("[data-linkedin-panel]");
    if (panel) {
      const slug = card.dataset.parent || form.dataset.parent || "";
      const next = await fetchText(
        `/api/linkedin-card?exclude=${encodeURIComponent(slug)}`);
      if (next !== null) {
        panel.innerHTML = next;
        wireDynamicContent(panel);
        return;
      }
    }
    // The debug/preview carousel has no swap panel; a reload re-renders the
    // queue without the now-inflight person, so the next card shows at the
    // same index — the same linear move, one page paint later.
    if (form.closest(".linkedin-stage[data-queue-index]")) {
      leaveAndReload("Queued for re-research — moving on");
      return;
    }
    // Directory-adjacent or non-panel surfaces keep the inline note.
    const note = form.querySelector("[data-retarget-note]");
    if (note) {
      note.textContent = "Queued — results apply automatically in the background";
      note.hidden = false;
    }
  } catch (error) {
    if (button) button.disabled = false;
    announce(error.message, true);
  }
});

const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
let scrollCueFrame = 0;

function refreshScrollCues() {
  if (scrollCueFrame) return;
  scrollCueFrame = window.requestAnimationFrame(() => {
    scrollCueFrame = 0;
    document.querySelectorAll(".identity-scroll-shell").forEach((shell) => {
      const scroller = shell.querySelector(".identity-scroll");
      const cue = shell.querySelector("[data-scroll-cue]");
      if (!scroller || !cue) return;
      const hasMore = scroller.scrollHeight > scroller.clientHeight + 4
        && scroller.scrollTop + scroller.clientHeight < scroller.scrollHeight - 4;
      cue.hidden = !hasMore;
    });
  });
}

function wireScrollShell(shell) {
  if (shell.dataset.wired) return;
  shell.dataset.wired = "true";
  const scroller = shell.querySelector(".identity-scroll");
  const cue = shell.querySelector("[data-scroll-cue]");
  if (!scroller || !cue) return;
  scroller.addEventListener("scroll", refreshScrollCues, { passive: true });
  cue.addEventListener("click", () => {
    scroller.scrollBy({
      top: Math.max(160, scroller.clientHeight * 0.7),
      behavior: reduceMotion ? "auto" : "smooth",
    });
  });
}

window.addEventListener("resize", refreshScrollCues);

async function loadDossier(details) {
  if (details.dataset.loaded) return;
  const body = details.querySelector(".dossier-text");
  if (!body) return;
  details.dataset.loaded = "true";
  body.setAttribute("aria-busy", "true");
  body.textContent = "Loading…";
  try {
    const response = await fetch(`/api/dossier?slug=${encodeURIComponent(details.dataset.slug || "")}`);
    if (response.ok) {
      body.innerHTML = await response.text();
    } else {
      body.textContent = "No details found";
    }
  } catch {
    body.textContent = "Could not load details";
  } finally {
    body.removeAttribute("aria-busy");
    refreshScrollCues();
  }
}

// Expandable decision-table rows lazy-load their dossier the first time they open.
// (`toggle` does not bubble, so rows inserted by the infinite scroll are wired
// through this same helper as they are created.)
function wireDecisionRow(row) {
  if (row.dataset.wired) return;
  row.dataset.wired = "true";
  row.addEventListener("toggle", () => { if (row.open) void loadDossier(row); });
}

// One wiring pass for anything the server renders — the initial page and every
// fragment swapped in without a reload (next cards, fetched decision rows).
function wireDynamicContent(root) {
  root.querySelectorAll(".details[data-slug]").forEach((details) => { void loadDossier(details); });
  root.querySelectorAll("details.decision-row[data-slug]").forEach(wireDecisionRow);
  root.querySelectorAll("[data-worth-search]").forEach(wireWorthSearch);
  root.querySelectorAll(".identity-scroll-shell").forEach(wireScrollShell);
  refreshScrollCues();
  // A visible queue card kicks off the prefetch of the card after it, so the
  // next decision swaps instantly instead of waiting on the save.
  const worthButton = root.querySelector(".worth-card [data-worth][data-pub]");
  if (worthButton) prefetchWorthCard(worthButton.dataset.pub || "");
  const linkedinPanel = document.querySelector("[data-linkedin-panel]");
  if (linkedinPanel) {
    const currentCard = linkedinPanel.querySelector("[data-card][data-parent]");
    if (currentCard) prefetchLinkedinCard(currentCard.dataset.parent || "");
  }
}

wireDynamicContent(document);

// --- infinite scroll + windowed decision list --------------------------------
// The decision tables render only a first chunk server-side; further chunks are
// fetched from /api/decision-rows as the user nears the bottom. To keep the DOM
// bounded with variable-height rows, off-screen chunks are "parked": their real
// nodes (listeners, open/loaded dossier state intact) are detached and replaced
// by an exact-height spacer measured at park time — no height estimation.
// Scrolling back toward an edge re-inserts the parked nodes and shrinks the
// spacer by the same measured amount, so scroll position never jumps.
function setupInfiniteDecisionList(list) {
  const view = list.dataset.view || "";
  const chunkSize = Math.max(1, parseInt(list.dataset.chunk || "40", 10));
  let total = Math.max(0, parseInt(list.dataset.total || "0", 10));
  const maxLiveChunks = 4; // live DOM rows are bounded to 4 chunks
  const edge = 600; // px margin that triggers fetch / park / unpark

  const topSpacer = document.createElement("div");
  const bottomSpacer = document.createElement("div");
  topSpacer.className = "virtual-spacer";
  bottomSpacer.className = "virtual-spacer";
  const loadingNote = document.createElement("div");
  loadingNote.className = "decision-loading";
  loadingNote.textContent = "Loading more…";
  loadingNote.hidden = true;
  list.prepend(topSpacer);
  list.append(loadingNote, bottomSpacer);

  // chunks[i] = { nodes, height? } in list order; [firstLive..lastLive] are in the DOM.
  const chunks = [{ nodes: Array.from(list.querySelectorAll("details.decision-row")) }];
  let firstLive = 0;
  let lastLive = 0;
  let fetchedRows = chunks[0].nodes.length;
  let fetching = false;
  let filterQuery = ""; // non-empty while the live-search filter owns the row set
  let allRowsFetch = null; // in-flight fetch-every-remaining-row pass (focus prefetch / filter)
  let searchHold = false; // true while the search box has focus: keep prefetched rows live

  const spacerHeight = (spacer) => parseFloat(spacer.style.height) || 0;
  const setSpacer = (spacer, delta) => {
    spacer.style.height = `${Math.max(0, spacerHeight(spacer) + delta)}px`;
  };
  const measure = (nodes) => nodes.reduce((sum, node) => sum + node.offsetHeight, 0);

  function parkTop() {
    const chunk = chunks[firstLive];
    chunk.height = measure(chunk.nodes);
    chunk.nodes.forEach((node) => node.remove());
    setSpacer(topSpacer, chunk.height);
    firstLive += 1;
  }
  function parkBottom() {
    const chunk = chunks[lastLive];
    chunk.height = measure(chunk.nodes);
    chunk.nodes.forEach((node) => node.remove());
    setSpacer(bottomSpacer, chunk.height);
    lastLive -= 1;
  }
  function unparkTop() {
    firstLive -= 1;
    const chunk = chunks[firstLive];
    topSpacer.after(...chunk.nodes);
    setSpacer(topSpacer, -chunk.height);
  }
  function unparkBottom() {
    lastLive += 1;
    const chunk = chunks[lastLive];
    loadingNote.before(...chunk.nodes);
    setSpacer(bottomSpacer, -chunk.height);
  }

  async function fetchChunk(limit = chunkSize) {
    if (fetching) return;
    fetching = true;
    loadingNote.hidden = false;
    try {
      const query = `view=${encodeURIComponent(view)}&offset=${fetchedRows}&limit=${limit}`;
      const response = await fetch(`/api/decision-rows?${query}`, { cache: "no-store" });
      if (!response.ok) throw new Error("Could not load more rows");
      const payload = await response.json();
      const template = document.createElement("template");
      template.innerHTML = (payload.rows || []).join("");
      const nodes = Array.from(template.content.querySelectorAll("details.decision-row"));
      if (nodes.length) {
        nodes.forEach(wireDecisionRow);
        chunks.push({ nodes });
        loadingNote.before(...nodes); // fetch only fires with nothing parked below
        lastLive = chunks.length - 1;
        fetchedRows += nodes.length;
      } else {
        fetchedRows = total; // scope shrank server-side; stop asking
      }
    } catch (error) {
      announce(error.message || "Could not load more rows", true);
    } finally {
      fetching = false;
      loadingNote.hidden = true;
      scheduleUpdate();
    }
  }

  let updateFrame = 0;
  function scheduleUpdate() {
    if (updateFrame) return;
    updateFrame = window.requestAnimationFrame(() => {
      updateFrame = 0;
      updateWindow();
    });
  }

  // The list scrolls itself on height-constrained layouts and scrolls WITH the
  // window on tall ones, so edge detection uses viewport-relative rects (valid
  // in both modes) rather than the list's own scrollTop.
  function visibleBand() {
    const rect = list.getBoundingClientRect();
    const viewportBottom = window.innerHeight || document.documentElement.clientHeight;
    return { top: Math.max(rect.top, 0), bottom: Math.min(rect.bottom, viewportBottom) };
  }

  function updateWindow() {
    if (filterQuery || allRowsFetch || searchHold) return; // filter/prefetch/focus owns the rows
    for (let guard = 0; guard < 20; guard += 1) {
      const band = visibleBand();
      const nearBottom = bottomSpacer.getBoundingClientRect().top <= band.bottom + edge;
      const nearTop = topSpacer.getBoundingClientRect().bottom >= band.top - edge;
      let changed = false;
      if (nearBottom && lastLive < chunks.length - 1) {
        unparkBottom();
        changed = true;
      } else if (nearTop && firstLive > 0) {
        unparkTop();
        changed = true;
      }
      // Keep the live window bounded; only park chunks fully outside the viewport.
      while (lastLive - firstLive + 1 > maxLiveChunks) {
        const topNodes = chunks[firstLive].nodes;
        const bottomNodes = chunks[lastLive].nodes;
        const topEnd = topNodes.length
          ? topNodes[topNodes.length - 1].getBoundingClientRect().bottom : -Infinity;
        const bottomStart = bottomNodes.length
          ? bottomNodes[0].getBoundingClientRect().top : Infinity;
        if (topEnd < band.top - edge) {
          parkTop();
          changed = true;
        } else if (bottomStart > band.bottom + edge) {
          parkBottom();
          changed = true;
        } else {
          break;
        }
      }
      if (!changed) break;
    }
    if (bottomSpacer.getBoundingClientRect().top <= visibleBand().bottom + edge
        && lastLive === chunks.length - 1 && fetchedRows < total) {
      void fetchChunk();
    }
  }

  // --- live name filter (worth search) ---------------------------------------
  // While a query is active the virtual window is suspended: every fetched row
  // is made live (parked chunks re-inserted, spacers zeroed, remaining rows
  // fetched) and non-matching rows are hidden — pure client-side filtering.
  // Clearing the query unhides everything and hands control back to the
  // windowing logic, which re-parks whatever sits outside the viewport.
  // Focusing the search box starts this same pass early (prefetchAllRows), so
  // the rows usually arrive during the human pause between focus and the first
  // keystroke instead of stalling the first filter. Single-flight: the focus
  // prefetch and a keystroke's filter share one run, and windowing stays
  // suspended while it fetches so mid-run parking can't reorder appended rows.
  function fetchAllRemainingRows() {
    if (allRowsFetch) return allRowsFetch;
    if (fetchedRows >= total) return Promise.resolve();
    allRowsFetch = (async () => {
      try {
        while (fetchedRows < total) {
          const before = fetchedRows;
          await fetchChunk(200); // the server caps each window at 200 rows
          if (fetchedRows === before) break; // fetch failed: filter what we have
        }
      } finally {
        allRowsFetch = null;
      }
    })();
    return allRowsFetch;
  }

  async function ensureAllLive() {
    while (firstLive > 0) unparkTop();
    while (lastLive < chunks.length - 1) unparkBottom();
    topSpacer.style.height = "0px";
    bottomSpacer.style.height = "0px";
    await fetchAllRemainingRows();
  }

  let filterChain = Promise.resolve();
  list.applyNameFilter = (query) => {
    // Serialized so rapid keystrokes resolve in order with correct counts.
    filterChain = filterChain.then(async () => {
      filterQuery = query;
      if (query) await ensureAllLive();
      let shown = 0;
      chunks.forEach((chunk) => chunk.nodes.forEach((node) => {
        const match = !query || (node.dataset.name || "").includes(query);
        node.hidden = !match;
        if (match) shown += 1;
      }));
      if (!query) scheduleUpdate(); // windowing resumes over the restored rows
      return { shown, total };
    });
    return filterChain;
  };

  // The search box warms the table on focus: at a few thousand rows the first
  // filter otherwise pays ~20 sequential row-window fetches before it can run.
  // The hold keeps the prefetched rows LIVE while the box has focus — without
  // it the windowing logic re-parks them and the first keystroke pays the full
  // re-insertion cost right back.
  list.prefetchAllRows = () => ensureAllLive();
  list.holdRowsLive = (on) => {
    searchHold = Boolean(on);
    if (!searchHold) scheduleUpdate(); // windowing resumes on blur
  };

  // Optimistic decisions remove a live row without a reload: drop it from its
  // chunk, shrink the totals the fetch offsets are computed from, and let the
  // window refill from below (or collapse to the empty state).
  list.virtualRemove = (row) => {
    chunks.some((chunk) => {
      const at = chunk.nodes.indexOf(row);
      if (at !== -1) {
        chunk.nodes.splice(at, 1);
        return true;
      }
      return false;
    });
    row.remove();
    total = Math.max(0, total - 1);
    fetchedRows = Math.max(0, fetchedRows - 1);
    list.dataset.total = String(total);
    if (total === 0) {
      const page = list.closest(".decision-page");
      if (page) {
        page.outerHTML = "<div class='empty-state decision-empty'><div class='empty-mark'>0</div>"
          + `<h2>No ${view === "yes" ? "yes" : "no"} decisions</h2></div>`;
      }
      return;
    }
    scheduleUpdate();
  };

  list.addEventListener("scroll", scheduleUpdate, { passive: true });
  window.addEventListener("scroll", scheduleUpdate, { passive: true });
  window.addEventListener("resize", scheduleUpdate);
  updateWindow(); // keep fetching while the first chunk does not fill the viewport
}

const decisionList = document.querySelector("[data-decision-list]");
if (decisionList) setupInfiniteDecisionList(decisionList);

let reviewStateToken = document.body.dataset.stateToken || "";
// True from the moment a stage-complete button is clicked: the freshness
// observer must not reload the page out from under the pending POST +
// navigation (the free-work job's manifest writes rotate the state token in
// exactly that window, and a reload tears down the JS before it can leave).
let completingStage = false;
let lastServerStage = "";
const observesExternalUpdates = document.body.dataset.externalUpdates === "true";

let autoCompleted = false;

// First arrival at "Decisions ready": fire the same flow the Continue button
// runs (POST /complete + navigate) so the user never clicks through a done
// screen. Server marks the block data-auto-complete only when the stage is
// not yet completed, so deliberate revisits keep the button and never yank.
function maybeAutoComplete(root) {
  if (autoCompleted || completingStage) return;
  const button = (root || document).querySelector("[data-auto-complete]");
  if (!button) return;
  autoCompleted = true;
  button.click();
}

function hasIdentityDraft() {
  return Array.from(document.querySelectorAll("[data-retarget-form] textarea[name='guidance']")).some(
    (textarea) => Boolean(textarea.value.trim()),
  );
}

// Update the enrich screen's counts in place from an SSE job-progress payload
// (no reload, no fetch — the numbers rode in on the event). Returns false when
// the current screen has no enrich state to update.
function renderJobProgress(job) {
  const state = document.querySelector(".enrich-state");
  if (!state || !job || !job.counts) return false;
  const text = state.querySelector("p");
  const bar = state.querySelector(".enrich-progress");
  const fill = state.querySelector(".enrich-progress-fill");
  const counts = job.counts;
  if (job.phase === "judging_retargets") {
    if (text) text.textContent = `${counts.done || 0} of ${counts.total || 0} checked`;
    return true;
  }
  const total = counts.total || 0;
  const done = Math.min(total, counts.completed || 0);
  if (text) text.textContent = `${done} of ${total} complete`;
  if (bar && fill && total) {
    bar.setAttribute("aria-valuemax", String(total));
    bar.setAttribute("aria-valuenow", String(done));
    fill.style.width = `${Math.round((done / total) * 100)}%`;
  }
  return true;
}

// Re-snapshot /api/status. Invoked by the server's SSE nudge stream — the
// browser never polls; the single-writer server pushes when anything changes.
// No visibility gating: events are rare and a snapshot costs ~20ms, so hidden
// tabs stay current too and are already correct when refocused.
async function syncFileState() {
  if (completingStage) return; // a stage-complete navigation is in flight
  const currentStage = document.body.dataset.stage || "";
  if (!observesExternalUpdates) return;
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) return;
    const state = await response.json();
    const isStagePreview = document.body.dataset.preview === "true";
    const preserveDraft = hasIdentityDraft();
    // Feed-forward: auto-navigation only ever moves FORWARD through the
    // stages, and only on a transition OBSERVED while this page was open (a
    // live handoff). A stage difference that already existed at page load
    // means the user deliberately opened this page (e.g. revisiting the worth
    // Review tab while the flow sits at enrich) — never yank them off it; the
    // token reload below still refreshes the page they chose to stay on.
    const stageOrder = ["worth", "enrich", "linkedin", "done"];
    const movesForward =
      stageOrder.indexOf(state.stage) > stageOrder.indexOf(currentStage);
    const observedTransition = Boolean(lastServerStage) && state.stage !== lastServerStage;
    lastServerStage = state.stage || lastServerStage;
    if (!isStagePreview && state.stage && state.stage !== currentStage
        && movesForward && observedTransition) {
      if (preserveDraft) return;
      window.location.replace(state.stage === "done"
        ? "/directory" : `/?stage=${encodeURIComponent(state.stage)}`);
      return;
    }
    if (state.state_token && state.state_token !== reviewStateToken) {
      if (preserveDraft) return;
      window.location.reload();
    }
  } catch {
    // The local observer may be restarting; the next poll will retry.
  }
}

// --- directory browse view (/directory) --------------------------------------
// Read-only reference surface: the sidebar's Yes/No worth tabs (default Yes)
// and search input filter an A-Z list rendered in chunks from the embedded
// island (scrolling appends more; the count shows "N of M" within the active
// tab; Enter opens the first match), and clicking a name fetches that person's
// pane from /api/person. Nothing here ever writes.
function setupDirectory() {
  const list = document.querySelector("[data-directory-list]");
  const detail = document.querySelector("[data-directory-detail]");
  const island = document.querySelector("script[data-directory-people]");
  if (!list || !detail || !island) return;
  let people = [];
  try { people = JSON.parse(island.textContent || "[]"); } catch { people = []; }
  const CHUNK = 150;
  const tabs = Array.from(document.querySelectorAll("[data-directory-tab]"));
  const box = document.querySelector("[data-directory-search]");
  const input = box?.querySelector("input");
  const count = box?.querySelector("[data-search-count]");
  let activeTab = tabs.find((tab) => tab.classList.contains("active"))?.dataset.directoryTab || "";
  let activeSlug = new URLSearchParams(window.location.search).get("person") || "";
  // A ?person= deep link lands on that person's own tab, so the server-rendered
  // pane always has its sidebar entry visible.
  const selected = people.find((entry) => entry.slug === activeSlug);
  if (selected && tabs.length) {
    const worth = selected.worth || "maybe";
    if (worth !== activeTab && tabs.some((tab) => tab.dataset.directoryTab === worth)) {
      activeTab = worth;
      tabs.forEach((tab) => tab.classList.toggle("active", tab.dataset.directoryTab === worth));
    }
  }
  let filtered = [];
  let rendered = 0;

  function entryButton(entry) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "directory-item";
    item.dataset.slug = entry.slug || "";
    item.textContent = entry.name || entry.slug || "";
    if (entry.slug === activeSlug) item.classList.add("active");
    return item;
  }

  function renderMore() {
    filtered.slice(rendered, rendered + CHUNK).forEach((entry) => list.append(entryButton(entry)));
    rendered = Math.min(filtered.length, rendered + CHUNK);
  }

  function fillViewport() {
    while (rendered < filtered.length && list.scrollHeight <= list.clientHeight + 200) renderMore();
  }

  function refreshList() {
    const scope = tabs.length && activeTab
      ? people.filter((entry) => (entry.worth || "maybe") === activeTab)
      : people;
    const query = (input?.value || "").trim().toLowerCase();
    filtered = query
      ? scope.filter((entry) => (entry.name || "").toLowerCase().includes(query))
      : scope;
    if (count) {
      count.hidden = !query;
      if (query) count.textContent = `${filtered.length} of ${scope.length}`;
    }
    list.textContent = "";
    rendered = 0;
    renderMore();
    fillViewport();
    list.scrollTop = 0;
  }

  async function loadPerson(slug, { keepScroll = false } = {}) {
    let response;
    try {
      response = await fetch(`/api/person?slug=${encodeURIComponent(slug)}`, { cache: "no-store" });
    } catch {
      announce("Could not load person", true);
      return false;
    }
    if (!response.ok) {
      announce("Could not load person", true);
      return false;
    }
    const scrollTop = detail.scrollTop;
    detail.innerHTML = await response.text();
    wireDynamicContent(detail);
    detail.scrollTop = keepScroll ? scrollTop : 0;
    return true;
  }

  async function selectPerson(slug) {
    if (!slug || slug === activeSlug) return;
    if (!(await loadPerson(slug))) return;
    activeSlug = slug;
    list.querySelectorAll(".directory-item").forEach((item) => {
      item.classList.toggle("active", item.dataset.slug === slug);
    });
    window.history.replaceState(null, "", `/directory?person=${encodeURIComponent(slug)}`);
  }

  function bumpDirectoryTab(worth, delta) {
    const span = document.querySelector(`[data-directory-tab='${worth}'] span`);
    if (!span) return;
    const current = parseInt(span.textContent || "0", 10);
    if (!Number.isNaN(current)) span.textContent = String(Math.max(0, current + delta));
  }

  // Move-to-Yes/No on the person pane: the /worth post, island entry, tab
  // counts, sidebar list, and advance-to-next all happen here — after the
  // feedback popover settles, so the pane never swaps under an open form.
  async function applyWorth({ pub, worth, slug }) {
    const prevIndex = filtered.findIndex((item) => item.slug === slug);
    try {
      await post("/worth", { pub, worth, parent_slug: slug });
    } catch (error) {
      detail.querySelectorAll("[data-dir-worth]").forEach((item) => { item.disabled = false; });
      announce(error.message, true);
      return;
    }
    const entry = people.find((item) => item.slug === slug);
    if (entry) {
      bumpDirectoryTab(entry.worth || "maybe", -1);
      entry.worth = worth;
      bumpDirectoryTab(worth, 1);
    }
    // Keep the sidebar where it was: the decided person leaves this tab, so
    // the same index now holds the next person — advance straight to them.
    // refreshList only renders the first chunk; render until the old scroll
    // offset exists again or the restore silently clamps to the top chunk.
    const listScroll = list.scrollTop;
    refreshList();
    while (rendered < filtered.length && list.scrollHeight < listScroll + list.clientHeight) {
      renderMore();
    }
    list.scrollTop = listScroll;
    announce(`Moved ${entry?.name || "person"} to ${worth === "yes" ? "Yes" : "No"}`);
    const next = (prevIndex >= 0 && filtered.length)
      ? filtered[Math.min(prevIndex, filtered.length - 1)] : null;
    if (next && next.slug !== slug) {
      await selectPerson(next.slug);
    } else {
      await loadPerson(slug, { keepScroll: true }); // re-render buttons for the new state
    }
  }

  // Two-step decide: clicking Yes/No opens the optional-why form on the person
  // being decided; the move itself waits until the form settles (send or skip),
  // so the label, the pane, and the feedback all refer to the same person.
  detail.addEventListener("click", (event) => {
    const button = event.target.closest("[data-dir-worth]");
    if (!button || button.disabled) return;
    event.preventDefault();
    const worth = button.dataset.dirWorth || "";
    const slug = button.dataset.parent || activeSlug;
    const pub = button.dataset.pub || "";
    const entry = people.find((item) => item.slug === slug);
    const anchor = detail.querySelector(".person-detail-actions")
      || detail.querySelector(".person-detail");
    if (!anchor) {
      void applyWorth({ pub, worth, slug });
      return;
    }
    detail.querySelectorAll("[data-dir-worth]").forEach((item) => { item.disabled = true; });
    feedbackPopover({
      anchor,
      contextLabel: `Move ${entry?.name || "person"} to ${worth === "yes" ? "Yes" : "No"} — optional: why?`,
      pub,
      slug,
      action: worth === "yes" ? "worth_yes" : "worth_no",
      onDone: () => void applyWorth({ pub, worth, slug }),
    });
  });

  // "…" overflow menu: general feedback that isn't a worth decision or a
  // retarget (wrong/missing info). Same popover, action "general", no move.
  detail.addEventListener("click", (event) => {
    const toggle = event.target.closest("[data-menu-toggle]");
    if (toggle) {
      event.preventDefault();
      const items = toggle.parentElement.querySelector(".person-menu-items");
      if (items) items.hidden = !items.hidden;
      return;
    }
    const general = event.target.closest("[data-feedback-general]");
    if (!general) return;
    event.preventDefault();
    general.closest(".person-menu-items")?.setAttribute("hidden", "");
    const slug = general.dataset.parent || activeSlug;
    const entry = people.find((item) => item.slug === slug);
    const anchor = detail.querySelector(".person-detail-actions")
      || detail.querySelector(".person-detail");
    if (!anchor) return;
    feedbackPopover({
      anchor,
      contextLabel: `Feedback on ${entry?.name || "this person"} — wrong or missing info?`,
      pub: general.dataset.pub || "",
      slug,
      action: "general",
    });
  });

  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-person-menu]")) return;
    detail.querySelectorAll(".person-menu-items:not([hidden])")
      .forEach((el) => { el.hidden = true; });
  });

  // Auto-filed feedback (retarget guidance) has no popover; when its
  // fire-and-forget post fails, the panel says so instead of staying silent.
  function renderFeedbackAlert(alert) {
    if (!retargetPanel) return false;
    let box = retargetPanel.querySelector("[data-feedback-alert]");
    if (!alert || !alert.status) {
      box?.remove();
      return false;
    }
    if (!box) {
      box = document.createElement("div");
      box.dataset.feedbackAlert = "";
      box.className = "retarget-feedback-alert";
      retargetPanel.append(box);
    }
    box.textContent = "";
    const line = document.createElement("small");
    line.textContent = `Feedback not sent: ${alert.error || alert.status}`;
    box.append(line);
    if (alert.status === "needs_auth") box.append(signInButton(""));
    if (alert.error !== renderFeedbackAlert.lastError) {
      renderFeedbackAlert.lastError = alert.error;
      announce(alert.error || "Feedback could not be sent", true);
    }
    return true;
  }


  // Guided retargets: submit guidance from the person pane, watch the queue in
  // the sidebar panel. This page has no SSE by design, so the panel polls only
  // while an item is active and goes quiet when the queue drains.
  const retargetPanel = document.querySelector("[data-retarget-panel]");
  const retargetItems = document.querySelector("[data-retarget-items]");
  const RETARGET_ACTIVE = ["queued", "researching", "judging", "hydrating"];
  let retargetTimer = null;
  const retargetSeen = {};

  function retargetRow(item) {
    const row = document.createElement("li");
    row.className = `retarget-item retarget-${item.state}`;
    const name = document.createElement("button");
    name.type = "button";
    name.className = "retarget-name";
    name.textContent = item.name || item.slug;
    name.addEventListener("click", () => void selectPerson(item.slug));
    const chip = document.createElement("span");
    chip.className = "retarget-chip";
    chip.textContent = (item.state || "").replace("_", " ");
    row.append(name, chip);
    if (item.detail) {
      const line = document.createElement("small");
      line.textContent = item.detail;
      row.append(line);
    }
    return row;
  }

  async function refreshRetargets() {
    if (!retargetPanel || !retargetItems) return;
    let data;
    try {
      const response = await fetch("/api/retargets", { cache: "no-store" });
      if (!response.ok) return;
      data = await response.json();
    } catch { return; }
    const items = data.items || [];
    const hasAlert = renderFeedbackAlert(data.feedback_alert);
    retargetPanel.hidden = !items.length && !hasAlert;
    retargetItems.textContent = "";
    items.forEach((item) => retargetItems.append(retargetRow(item)));
    // A just-finished item announces itself and refreshes the open pane.
    items.forEach((item) => {
      const prev = retargetSeen[item.pub];
      if (prev && RETARGET_ACTIVE.includes(prev) && !RETARGET_ACTIVE.includes(item.state)) {
        if (item.state === "applied") announce(`Retargeted ${item.name}`);
        else announce(`${item.name}: ${item.detail || item.state}`, item.state === "failed");
        if (item.slug === activeSlug) void loadPerson(item.slug, { keepScroll: true });
      }
      retargetSeen[item.pub] = item.state;
    });
    const active = items.some((item) => RETARGET_ACTIVE.includes(item.state));
    if (active && !retargetTimer) retargetTimer = setInterval(refreshRetargets, 3000);
    if (!active && retargetTimer) { clearInterval(retargetTimer); retargetTimer = null; }
  }

  detail.addEventListener("submit", async (event) => {
    const form = event.target.closest("[data-retarget-form]");
    if (!form) return;
    event.preventDefault();
    const textarea = form.querySelector("textarea[name='guidance']");
    const guidance = (textarea?.value || "").trim();
    if (!guidance) return;
    const button = form.querySelector("button[type='submit']");
    if (button) button.disabled = true;
    try {
      await post("/retarget", { pub: form.dataset.pub || "",
                                parent_slug: form.dataset.parent || "", guidance });
      if (textarea) textarea.value = "";
      announce("Queued for re-research");
      void refreshRetargets();
    } catch (error) {
      announce(error.message, true);
    } finally {
      if (button) button.disabled = false;
    }
  });

  void refreshRetargets();

  list.addEventListener("click", (event) => {
    const item = event.target.closest(".directory-item");
    if (item) void selectPerson(item.dataset.slug || "");
  });
  list.addEventListener("scroll", () => {
    if (list.scrollTop + list.clientHeight >= list.scrollHeight - 400) renderMore();
  }, { passive: true });

  tabs.forEach((tab) => tab.addEventListener("click", () => {
    if (tab.dataset.directoryTab === activeTab) return;
    activeTab = tab.dataset.directoryTab || "";
    tabs.forEach((item) => item.classList.toggle("active", item === tab));
    refreshList();
  }));

  if (input) {
    input.addEventListener("input", refreshList);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        if (filtered.length) void selectPerson(filtered[0].slug || "");
      } else if (event.key === "Escape") {
        input.value = "";
        refreshList();
      }
    });
  }

  refreshList();
  if (activeSlug) {
    // The server already rendered this person's pane; make sure their sidebar
    // entry exists (render up to it) and is visible.
    const at = filtered.findIndex((entry) => entry.slug === activeSlug);
    while (at >= rendered && rendered < filtered.length) renderMore();
    list.querySelector(".directory-item.active")?.scrollIntoView({ block: "center" });
  }
}

if (document.body.dataset.stage === "directory") setupDirectory();

maybeAutoComplete(document);

if (observesExternalUpdates) {
  void syncFileState();
  const serverEvents = new EventSource("/api/events");
  serverEvents.onmessage = (message) => {
    let payload = null;
    try { payload = JSON.parse(message.data); } catch { payload = null; }
    // Pure job-progress events update the counts in place; everything else
    // (mutations, job terminals) re-snapshots and reloads on a token change.
    if (payload && payload.job && renderJobProgress(payload.job)) return;
    void syncFileState();
  };
  // A reconnect implies missed events — re-snapshot on every open.
  serverEvents.onopen = () => { void syncFileState(); };
}
