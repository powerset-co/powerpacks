const toast = document.querySelector(".toast");
const stage = document.querySelector(".stage");

function announce(message, isError = false) {
  if (!toast) return;
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.add("show");
  window.clearTimeout(announce.timer);
  announce.timer = window.setTimeout(() => toast.classList.remove("show"), 1800);
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
  if (!response.ok) throw new Error((await response.text()) || "Could not save");
  return response.json();
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

// After an in-place mutation the server's state token changes; adopt it so the
// background file-state poller doesn't treat our own write as external news and
// reload the page we just updated optimistically.
async function adoptServerState() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) return;
    const state = await response.json();
    if (state.state_token) reviewStateToken = state.state_token;
  } catch {
    // the next poll will resync
  }
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
      post("/worth", { pub: button.dataset.pub || "", worth }),
      delay(170),
    ]);
    await adoptServerState();
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

async function decideWorthCard(button, card) {
  const worth = button.dataset.worth;
  card.querySelectorAll("button").forEach((item) => { item.disabled = true; });
  card.classList.add("leaving");
  bumpTabCount("review", -1); // leaves the Review queue for the yes/no pile
  bumpTabCount(worth, 1);
  try {
    const [response] = await Promise.all([
      post("/worth", { pub: button.dataset.pub || "", worth }),
      delay(170),
    ]);
    await adoptServerState();
    applyProgress(response.progress);
    announce(worth === "yes" ? "Added" : "Rejected");
    const panel = card.closest(".worth-panel");
    const nextHtml = await fetchText("/api/worth-card");
    if (!panel || nextHtml === null) {
      leaveAndReload("Saved"); // fallback: could not swap in the next card
      return;
    }
    panel.innerHTML = nextHtml; // next queue card, or the Decisions-ready state
    wireDynamicContent(panel);
  } catch (error) {
    card.classList.remove("leaving");
    card.querySelectorAll("button").forEach((item) => { item.disabled = false; });
    bumpTabCount("review", 1);
    bumpTabCount(worth, -1);
    announce(error.message, true);
  }
}

async function decideLinkedinCard(card, values, message) {
  card.querySelectorAll("button").forEach((item) => { item.disabled = true; });
  card.classList.add("leaving");
  try {
    const [response] = await Promise.all([post("/decide", values), delay(170)]);
    await adoptServerState();
    applyProgress(response.progress);
    announce(message);
    const nextHtml = await fetchText("/api/linkedin-card");
    if (nextHtml === null) {
      leaveAndReload("Saved");
      return;
    }
    const template = document.createElement("template");
    template.innerHTML = nextHtml; // next identity card, or the finished state
    const next = template.content.firstElementChild;
    // Swap the whole stage wrapper so the passive enrichment note refreshes too.
    const holder = card.closest(".linkedin-stage") || card;
    holder.replaceWith(...template.content.childNodes);
    if (next) wireDynamicContent(next);
  } catch (error) {
    card.classList.remove("leaving");
    card.querySelectorAll("button").forEach((item) => { item.disabled = false; });
    announce(error.message, true);
  }
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button || button.disabled) return;

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

  if (button.hasAttribute("data-open-fix")) {
    event.preventDefault();
    const sectionId = button.getAttribute("aria-controls");
    const section = sectionId ? document.getElementById(sectionId) : null;
    if (section instanceof HTMLElement) {
      section.hidden = false;
      button.setAttribute("aria-expanded", "true");
      section.querySelector("input[name='new_url']")?.focus({ preventScroll: true });
    }
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
    try {
      await post("/approve-enrichment", {});
      leaveAndReload("Approved");
    } catch (error) {
      unlock(button);
      announce(error.message, true);
    }
    return;
  }

  if (button.dataset.complete) {
    event.preventDefault();
    lock(button);
    try {
      await post("/complete", { stage: button.dataset.complete });
      const next = {
        worth: ["People complete", "/?stage=enrich"],
        enrich: ["Enrichment complete", "/?stage=linkedin"],
        linkedin: ["All set", "/?stage=done"],
      }[button.dataset.complete] || ["Saved", window.location.href];
      leaveAndNavigate(next[0], next[1]);
    } catch (error) {
      unlock(button);
      announce(error.message, true);
    }
  }
});

function wireFixForm(form) {
  if (form.dataset.wired) return;
  form.dataset.wired = "true";
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = form.querySelector("input[name='new_url']");
    const values = {
      pub: form.dataset.pub || "",
      parent_slug: form.dataset.parent || "",
      decision: "fix",
      new_url: input?.value.trim() || "",
    };
    const card = form.closest(".identity-card");
    if (card) {
      void decideLinkedinCard(card, values, "LinkedIn updated");
      return;
    }
    const button = form.querySelector("button[type='submit']");
    lock(button);
    try {
      await post("/decide", values);
      leaveAndReload("LinkedIn updated");
    } catch (error) {
      unlock(button);
      announce(error.message, true);
    }
  });
}

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
  root.querySelectorAll("[data-fix-form]").forEach(wireFixForm);
  root.querySelectorAll(".identity-scroll-shell").forEach(wireScrollShell);
  refreshScrollCues();
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

  async function fetchChunk() {
    if (fetching) return;
    fetching = true;
    loadingNote.hidden = false;
    try {
      const query = `view=${encodeURIComponent(view)}&offset=${fetchedRows}&limit=${chunkSize}`;
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
const statusPollMs = 5000;

function hasIdentityDraft() {
  return Array.from(document.querySelectorAll("[data-fix-form] input[name='new_url']")).some(
    (input) => !input.closest("[hidden]") && Boolean(input.value.trim()),
  );
}

async function pollFileState() {
  if (document.visibilityState !== "visible") return;
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) return;
    const state = await response.json();
    const currentStage = document.body.dataset.stage || "";
    const isStagePreview = document.body.dataset.preview === "true";
    const preserveDraft = hasIdentityDraft();
    if (!isStagePreview && state.stage && state.stage !== currentStage) {
      if (preserveDraft) return;
      window.location.replace(`/?stage=${encodeURIComponent(state.stage)}`);
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

void pollFileState();
window.setInterval(pollFileState, statusPollMs);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") void pollFileState();
});
