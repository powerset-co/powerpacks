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
      post("/worth", { pub: button.dataset.pub || "", worth }),
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
  card.querySelectorAll("button").forEach((item) => { item.disabled = true; });
  card.classList.add("leaving");
  bumpTabCount("review", -1); // leaves the Review queue for the yes/no pile
  bumpTabCount(worth, 1);
  inFlightWorth.add(pub);
  const panel = card.closest(".worth-panel");
  const oldHtml = panel ? panel.innerHTML : null;
  const postPromise = post("/worth", { pub, worth }); // fire-and-track, no await
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
    postPromise.then((response) => {
      adoptMutationState(response);
      applyProgress(response.progress);
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

const linkedinBufferTarget = 10;
const linkedinRefillAt = 5;

function linkedinBufferCards(buffer) {
  return Array.from(buffer?.querySelectorAll(":scope > [data-linkedin-buffer-card]") || []);
}

function setLinkedinBufferSaving(buffer, saving) {
  if (!buffer) return;
  buffer.toggleAttribute("data-saving", saving);
  buffer.setAttribute("aria-busy", saving ? "true" : "false");
  buffer.querySelectorAll("button, input").forEach((item) => { item.disabled = saving; });
}

function showFirstLinkedinCard(buffer) {
  const cards = linkedinBufferCards(buffer);
  cards.forEach((item, index) => { item.hidden = index !== 0; });
  if (cards[0]) wireDynamicContent(cards[0]);
  return cards[0] || null;
}

function optimisticLinkedinAdvance(card, values) {
  const buffer = card.closest("[data-linkedin-buffer]");
  const current = card.closest("[data-linkedin-buffer-card]");
  if (!buffer || !current) return null;
  const sameParentResolves = values.decision === "keep" || values.decision === "fix";
  const parent = current.dataset.parent || "";
  current.hidden = true;
  const next = linkedinBufferCards(buffer).find((item) => {
    if (item === current) return false;
    return !(sameParentResolves && item.dataset.parent === parent);
  });
  if (next) {
    next.hidden = false;
    wireDynamicContent(next);
  } else {
    const saving = document.createElement("div");
    saving.className = "empty-state linkedin-saving";
    saving.dataset.linkedinSaving = "true";
    saving.innerHTML = "<h2>Saving…</h2>";
    buffer.append(saving);
  }
  setLinkedinBufferSaving(buffer, true);
  return { buffer, current };
}

function rollbackLinkedinAdvance(advance) {
  if (!advance) return;
  advance.buffer.querySelector("[data-linkedin-saving]")?.remove();
  linkedinBufferCards(advance.buffer).forEach((item) => { item.hidden = true; });
  advance.current.hidden = false;
  setLinkedinBufferSaving(advance.buffer, false);
  wireDynamicContent(advance.current);
}

function appendLinkedinCards(buffer, htmlCards) {
  const existing = new Set(
    linkedinBufferCards(buffer).map((item) => `${item.dataset.parent}:${item.dataset.pub}`),
  );
  const template = document.createElement("template");
  template.innerHTML = (htmlCards || []).join("");
  template.content.querySelectorAll("[data-linkedin-buffer-card]").forEach((item) => {
    const key = `${item.dataset.parent}:${item.dataset.pub}`;
    if (existing.has(key)) return;
    item.hidden = true;
    existing.add(key);
    buffer.append(item);
    wireDynamicContent(item);
  });
}

async function refillLinkedinBuffer(buffer) {
  const held = linkedinBufferCards(buffer).length;
  if (held > linkedinRefillAt) return;
  const target = parseInt(buffer.dataset.bufferTarget || `${linkedinBufferTarget}`, 10);
  const limit = Math.max(0, target - held);
  if (!limit) return;
  try {
    const response = await fetch(
      `/api/linkedin-cards?offset=${held}&limit=${limit}`,
      { cache: "no-store" },
    );
    if (!response.ok) throw new Error("Could not preload more people");
    const payload = await response.json();
    appendLinkedinCards(buffer, payload.cards);
    adoptMutationState(payload);
  } catch (error) {
    // The visible queue remains usable; the next successful save retries refill.
    announce(error.message, true);
  }
}

async function decideLinkedinCard(card, values, message) {
  const advance = optimisticLinkedinAdvance(card, values);
  if (!advance) {
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
  try {
    const response = await post("/decide", values);
    adoptMutationState(response);
    applyProgress(response.progress);
    // A keep/fix resolves the WHOLE parent (siblings are withdrawn server-side), so
    // remove the parent's card. resolved_pubs still drives per-pub removal for any
    // legacy single-candidate markup where the wrapper is keyed on the pub.
    const resolved = new Set(response.resolved_pubs || [values.pub]);
    const resolvedParent = advance.current.dataset.parent || values.parent_slug || "";
    const sameParentResolves = values.decision === "keep" || values.decision === "fix";
    linkedinBufferCards(advance.buffer).forEach((item) => {
      const byPub = resolved.has(item.dataset.pub || "");
      const byParent = sameParentResolves && resolvedParent
        && item.dataset.parent === resolvedParent;
      if (byPub || byParent) item.remove();
    });
    advance.buffer.querySelector("[data-linkedin-saving]")?.remove();
    if (response.complete_html) {
      advance.buffer.outerHTML = response.complete_html;
      wireDynamicContent(document);
    } else {
      showFirstLinkedinCard(advance.buffer);
      setLinkedinBufferSaving(advance.buffer, false);
      void refillLinkedinBuffer(advance.buffer);
    }
    announce(message);
  } catch (error) {
    rollbackLinkedinAdvance(advance);
    announce(error.message, true);
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
  // A visible worth queue card kicks off the prefetch of the card after it,
  // so the next decision swaps instantly instead of waiting on the save.
  const worthButton = root.querySelector(".worth-card [data-worth][data-pub]");
  if (worthButton) prefetchWorthCard(worthButton.dataset.pub || "");
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
const statusPollMs = 1000;
const observesExternalUpdates = document.body.dataset.externalUpdates === "true";

function hasIdentityDraft() {
  return Array.from(document.querySelectorAll("[data-fix-form] input[name='new_url']")).some(
    (input) => !input.closest("[hidden]") && Boolean(input.value.trim()),
  );
}

async function pollFileState() {
  if (document.visibilityState !== "visible") return;
  const currentStage = document.body.dataset.stage || "";
  if (!observesExternalUpdates) return;
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) return;
    const state = await response.json();
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

if (observesExternalUpdates) {
  void pollFileState();
  window.setInterval(pollFileState, statusPollMs);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") void pollFileState();
  });
}
