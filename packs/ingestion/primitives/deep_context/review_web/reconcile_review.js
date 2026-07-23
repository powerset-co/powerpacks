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
    pub, worth, parent_slug: button.dataset.parent || "",
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
        linkedin: ["All set", "/?stage=done"],
      }[button.dataset.complete] || ["Saved", window.location.href];
      leaveAndNavigate(next[0], next[1]);
    } catch (error) {
      completingStage = false;
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
const statusPollMs = 1000;
const observesExternalUpdates = document.body.dataset.externalUpdates === "true";

function hasIdentityDraft() {
  return Array.from(document.querySelectorAll("[data-fix-form] input[name='new_url']")).some(
    (input) => !input.closest("[hidden]") && Boolean(input.value.trim()),
  );
}

async function pollFileState() {
  if (document.visibilityState !== "visible") return;
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
