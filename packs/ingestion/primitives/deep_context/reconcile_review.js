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
      leaveAndReload("Saved");
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
      leaveAndReload(button.dataset.complete === "worth" ? "People complete" : "All set");
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
refreshScrollCues();

if (document.body.dataset.stage === "waiting") {
  window.setInterval(() => {
    if (document.visibilityState === "visible") window.location.reload();
  }, 3000);
}
