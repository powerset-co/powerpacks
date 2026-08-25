const toast = document.querySelector(".toast");

function announce(message, isError = false) {
  if (!toast) return;
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.add("show");
  window.clearTimeout(announce.timer);
  announce.timer = window.setTimeout(
    () => toast.classList.remove("show"), isError ? 6000 : 1800);
}

async function post(path, values) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(values),
  });
  if (!response.ok) {
    const text = (await response.text()) || "Could not send feedback";
    let message = text;
    try {
      const payload = JSON.parse(text);
      message = payload.error || payload.status || text;
    } catch { /* plain text */ }
    throw new Error(message);
  }
  return response.json();
}

function closeFeedbackPopover() {
  document.querySelector(".feedback-popover")?.remove();
}

async function loadSearchDetails(body) {
  if (!body || body.dataset.loaded === "true" || body.dataset.loading === "true") return;
  body.dataset.loading = "true";
  try {
    const response = await fetch(`/api/search?run_id=${encodeURIComponent(body.dataset.searchBody)}`);
    if (!response.ok) throw new Error((await response.text()) || "Could not load results");
    body.innerHTML = await response.text();
    body.dataset.loaded = "true";
  } catch (error) {
    body.innerHTML = `<p class='loading-results'>${error.message}</p>`;
    announce(error.message, true);
  } finally {
    delete body.dataset.loading;
  }
}

function feedbackPopover(anchor) {
  closeFeedbackPopover();
  const host = anchor.closest(".candidate-indicators, .search-card") || document.body;
  const pop = document.createElement("div");
  pop.className = "feedback-popover";

  const label = document.createElement("p");
  label.className = "feedback-context";
  label.textContent = anchor.getAttribute("aria-label") || "Send feedback";
  const confirm = document.createElement("p");
  confirm.className = "feedback-confirm";
  confirm.textContent = "Send this feedback to Powerset?";
  const textarea = document.createElement("textarea");
  textarea.rows = 2;
  textarea.maxLength = 4000;
  textarea.setAttribute("aria-label", "Feedback note");
  textarea.placeholder = "Identifiers only — never include message content.";
  const footer = document.createElement("div");
  footer.className = "feedback-footer";
  footer.innerHTML = "<span class='feedback-hint'>&#8629; &#8984;+Enter</span>"
    + "<span class='feedback-actions'>"
    + "<button type='button' class='feedback-cancel'>Cancel</button>"
    + "<button type='button' class='feedback-send' disabled>Send</button>"
    + "</span>";
  pop.append(label, confirm, textarea, footer);

  const send = footer.querySelector(".feedback-send");
  const cancel = footer.querySelector(".feedback-cancel");
  let settled = false;

  function finish() {
    if (settled) return;
    settled = true;
    document.removeEventListener("click", away);
    pop.remove();
  }

  async function submit() {
    const comment = textarea.value.trim();
    if (!comment || settled) return;
    send.disabled = true;
    cancel.disabled = true;
    try {
      await post("/feedback", {
        run_id: anchor.dataset.feedbackRun || "",
        person_id: anchor.dataset.feedbackPerson || "",
        comment,
      });
    } catch (error) {
      announce(error.message, true);
      send.disabled = false;
      cancel.disabled = false;
      return;
    }
    settled = true;
    document.removeEventListener("click", away);
    pop.className = "feedback-popover feedback-done";
    pop.innerHTML = "<span class='feedback-done-badge'><svg viewBox='0 0 24 24' width='14' height='14' fill='none' stroke='currentColor' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'><path d='M20 6 9 17l-5-5'/></svg></span><p>Got it, thanks!</p>";
    window.setTimeout(() => pop.remove(), 900);
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
  cancel.addEventListener("click", finish);
  pop.addEventListener("click", (event) => event.stopPropagation());

  host.append(pop);
  const hostRect = host.getBoundingClientRect();
  const anchorRect = anchor.getBoundingClientRect();
  pop.style.top = `${anchorRect.bottom - hostRect.top + host.scrollTop + 8}px`;
  pop.style.right = `${Math.max(8, hostRect.right - anchorRect.right)}px`;
  window.setTimeout(() => textarea.focus(), 80);

  function away(event) {
    if (!document.body.contains(pop)) {
      document.removeEventListener("click", away);
      return;
    }
    if (!pop.contains(event.target) && event.target !== anchor) finish();
  }
  window.setTimeout(() => document.addEventListener("click", away), 0);
}

document.addEventListener("click", (event) => {
  const tab = event.target.closest("[data-pond-tab]");
  if (tab) {
    const body = tab.closest(".search-body");
    body.querySelectorAll("[data-pond-tab]").forEach((button) => {
      button.setAttribute("aria-selected", String(button === tab));
    });
    body.querySelectorAll("[data-pond-panel]").forEach((panel) => {
      panel.hidden = panel.dataset.pondPanel !== tab.dataset.pondTab;
    });
    return;
  }
  const trigger = event.target.closest("[data-feedback-run]");
  if (!trigger) return;
  event.preventDefault();
  event.stopPropagation();
  feedbackPopover(trigger);
});

document.querySelectorAll("[data-search-body]").forEach((body) => void loadSearchDetails(body));

document.addEventListener("error", (event) => {
  if (event.target.matches?.(".avatar img")) event.target.hidden = true;
}, true);

document.addEventListener("click", (event) => {
  const toggle = event.target.closest(".group-toggle");
  if (!toggle) return;
  toggle.closest(".result-group").classList.toggle("group-collapsed");
});

document.addEventListener("click", (event) => {
  const copy = event.target.closest("[data-copy-query]");
  if (!copy) return;
  event.stopPropagation();
  void navigator.clipboard.writeText(copy.dataset.copyQuery).then(() => {
    const prior = copy.textContent;
    copy.textContent = "✓";
    setTimeout(() => { copy.textContent = prior; }, 900);
  });
}, true);
