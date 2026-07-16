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

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;

  if (button.dataset.worth) {
    event.preventDefault();
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
    if (section instanceof HTMLDetailsElement) {
      section.open = true;
      button.setAttribute("aria-expanded", "true");
      section.querySelector("input[name='new_url']")?.focus({ preventScroll: true });
    }
    return;
  }

  if (button.dataset.decide) {
    event.preventDefault();
    lock(button);
    try {
      await post("/decide", {
        pub: button.dataset.pub || "",
        decision: button.dataset.decide,
        parent_slug: button.dataset.parent || "",
      });
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

document.querySelectorAll("[data-fix-form]").forEach((form) => {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector("button[type='submit']");
    const input = form.querySelector("input[name='new_url']");
    lock(button);
    try {
      await post("/decide", {
        pub: form.dataset.pub || "",
        parent_slug: form.dataset.parent || "",
        decision: "fix",
        new_url: input?.value.trim() || "",
      });
      leaveAndReload("LinkedIn updated");
    } catch (error) {
      unlock(button);
      announce(error.message, true);
    }
  });
});

const scrollRegions = Array.from(document.querySelectorAll(".identity-scroll-shell"));
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
let scrollCueFrame = 0;

function refreshScrollCues() {
  if (scrollCueFrame) return;
  scrollCueFrame = window.requestAnimationFrame(() => {
    scrollCueFrame = 0;
    scrollRegions.forEach((shell) => {
      const scroller = shell.querySelector(".identity-scroll");
      const cue = shell.querySelector("[data-scroll-cue]");
      if (!scroller || !cue) return;
      const hasMore = scroller.scrollHeight > scroller.clientHeight + 4
        && scroller.scrollTop + scroller.clientHeight < scroller.scrollHeight - 4;
      cue.hidden = !hasMore;
    });
  });
}

scrollRegions.forEach((shell) => {
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
});
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

document.querySelectorAll(".details[data-slug]").forEach((details) => {
  void loadDossier(details);
});

// Expandable decision-table rows lazy-load their dossier the first time they open.
document.querySelectorAll("details.decision-row[data-slug]").forEach((row) => {
  row.addEventListener("toggle", () => { if (row.open) void loadDossier(row); });
});
refreshScrollCues();

let reviewStateToken = document.body.dataset.stateToken || "";

function hasIdentityDraft() {
  const input = document.querySelector("details.alternate[open] input[name='new_url']");
  return Boolean(input && (document.activeElement === input || input.value.trim()));
}

async function pollFileState() {
  if (document.visibilityState !== "visible" || hasIdentityDraft()) return;
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) return;
    const state = await response.json();
    const currentStage = document.body.dataset.stage || "";
    if (state.stage && state.stage !== currentStage) {
      window.location.replace(`/?stage=${encodeURIComponent(state.stage)}`);
      return;
    }
    if (state.state_token && state.state_token !== reviewStateToken) {
      window.location.reload();
    }
  } catch {
    // The local observer may be restarting; the next poll will retry.
  }
}

window.setInterval(pollFileState, 5000);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") void pollFileState();
});
