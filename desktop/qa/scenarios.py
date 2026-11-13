"""The flows a person clicks through, each a list of checks with a time budget.

Each one is the shape of a bug that passed unit tests and was found by hand in the app: a menu
under the drawer (#963), a rename field that ate keystrokes (#963), a LinkedIn link that did
nothing (#1002), Keep/Skip and delete taking seconds (#901, #902), a set pick that didn't take
(#803). Budgets are for one person's local data on a laptop.
"""

from __future__ import annotations

import json
import time

from app import App, Failed

# Time budgets, ms.
PAGE = 1500  # a page's first rows
CLICK = 300  # the page answers a click (a drawer opens, a menu shows)
WRITE = 800  # a saved change shows

DRAWER = {"css": "[aria-label='Edit decision']"}
ROW = {"css": "[data-viewport] .row .who"}  # a row, clicked on its name as a person does


def people_open(app: App) -> None:
    """People opens with rows, and a row opens its drawer."""
    app.go("/people", {"css": "[data-viewport] .row"}, PAGE)
    if not app.shows(DRAWER):  # the page reopens the last person on a reload
        app.click(ROW)
        app.wait_for(DRAWER, CLICK, "the drawer opens")


def people_menus(app: App) -> None:
    """The drawer's Decision and Relationship menus open above the drawer and take a pick."""
    people_open(app)
    for menu in ("Edit decision", "Edit relationship"):
        app.click({"css": f"[aria-label='{menu}']"})
        app.wait_for({"css": "[role=listbox] [role=option]"}, CLICK, f"{menu} opens its menu")
        app.click({"css": "[role=listbox] [role=option]"})
        app.wait_for("!document.querySelector('[role=listbox]')", WRITE, f"{menu} takes the pick")


def people_keep_skip(app: App) -> None:
    """Keep and Skip on the drawer's bar save fast and leave the drawer on the same person."""
    people_open(app)
    name = app.js("document.querySelector('.name-pencil')?.closest('h2')?.innerText")
    for button, shown in (("Keep", "Keep"), ("Skip", "Skip")):
        app.click({"css": ".action-bar button", "text": button})
        app.wait_for(
            f"document.querySelector(\"[aria-label='Edit decision']\")?.innerText.trim() === {shown!r}",
            WRITE,
            f"{button} shows on the person",
        )
        app.wait_for(
            "[...document.querySelectorAll('.action-bar button')].every((button) => !button.disabled)",
            WRITE,
            f"after {button} the bar takes the next click",
        )
        now = app.js("document.querySelector('.name-pencil')?.closest('h2')?.innerText")
        if now != name:
            raise Failed(f"after {button} the drawer moved from {name!r} to {now!r}")


def people_rename(app: App) -> None:
    """The pencil opens the name field with the name selected; every letter typed then stays
    (the field once re-selected the name on each keystroke, so only the last letter survived)."""
    people_open(app)
    app.click({"text": "Rename", "exact": True})
    app.wait_for({"css": "input.name-edit"}, CLICK, "the name field opens")
    app.type("Jordan Bravo")
    typed = app.value("input.name-edit")
    if typed != "Jordan Bravo":
        raise Failed(f"typed 'Jordan Bravo' over the selected name, the field reads {typed!r}")
    app.key("Escape")
    app.wait_for("!document.querySelector('input.name-edit')", CLICK, "Escape closes the field")


def people_linkedin(app: App) -> None:
    """A LinkedIn link leaves the app for the browser."""
    app.go("/people", {"css": "[data-viewport] .row"}, PAGE)
    rows = app.js("document.querySelectorAll('[data-viewport] .row').length")
    for index in range(min(rows, 20)):
        app.click(ROW, index)
        try:
            app.wait_for({"css": "a[href*='linkedin.com/in']"}, WRITE, "the drawer shows a LinkedIn link", patience=1.5)
            break
        except Failed:
            continue
    else:
        raise Failed("none of the first 20 people has a LinkedIn link")
    app.opened()
    app.click({"css": "a[href*='linkedin.com/in']"})
    opened: list[str] = []
    deadline = time.time() + 1
    while time.time() < deadline and not any("linkedin.com/in" in url for url in opened):
        opened += app.opened()
        time.sleep(0.05)
    if not any("linkedin.com/in" in url for url in opened):
        raise Failed(f"the click opened nothing in the browser (opened: {opened})")


def people_delete(app: App) -> None:
    """Delete asks in the bar, and the person leaves the list fast."""
    people_open(app)
    count = app.js("document.querySelector('[aria-rowcount]').getAttribute('aria-rowcount')")
    app.click({"css": ".action-bar button", "text": "Delete"})
    app.wait_for({"css": ".confirming"}, CLICK, "Delete asks first")
    app.click({"css": ".confirming button"})
    app.wait_for(
        f"document.querySelector('[aria-rowcount]').getAttribute('aria-rowcount') !== {count!r}",
        WRITE,
        "the person leaves the list",
    )


LOADING = "document.querySelectorAll('[data-viewport] .row-loading').length === 0"
ROW_COUNT = "Number(document.querySelector('[aria-rowcount]').getAttribute('aria-rowcount')) - 1"


def people_scroll(app: App) -> None:
    """Scrolling past the first slice fills the rows in fast (the list loads 200 at a time)."""
    app.go("/people", {"css": "[data-viewport] .row"}, PAGE)
    for fraction in (0.5, 1):
        app.mark = app.js("performance.now()")
        app.js(f"(() => {{ const v = document.querySelector('[data-viewport]'); v.scrollTop = v.scrollHeight * {fraction}; return true }})()")
        app.wait_for(f"({LOADING}) && document.querySelectorAll('[data-viewport] .row').length > 0", WRITE,
                     f"the rows {int(fraction * 100)}% down load")


def people_search(app: App) -> None:
    """Typing in the search box narrows the list fast."""
    app.go("/people", {"css": "[data-viewport] .row"}, PAGE)
    total = app.js(ROW_COUNT)
    app.click({"css": "[data-bar] input[type=search]"})
    app.type("aaron")
    app.wait_for(f"({ROW_COUNT}) < {total} && ({LOADING})", WRITE, "the search narrows the list")
    names = app.js("[...document.querySelectorAll('[data-viewport] .row .who')].map((n) => n.innerText.toLowerCase())")
    if not names or not all("aaron" in name for name in names):
        raise Failed(f"rows that don't match 'aaron': {[n for n in names if 'aaron' not in n][:3]}")


def people_tab(app: App) -> None:
    """Picking Skip in the Decision filter shows only skipped people, fast."""
    app.go("/people", {"css": "[data-viewport] .row"}, PAGE)
    app.click({"css": "[data-bar] [aria-label='Decision']"})
    app.wait_for({"css": "[role=listbox] [role=option]"}, CLICK, "the Decision filter opens")
    app.click({"css": "[role=listbox] [role=option]", "text": "Skip"})
    app.wait_for("location.search.includes('tab=no')", CLICK, "the tab changes")
    app.wait_for(f"({LOADING}) && [...document.querySelectorAll('[data-viewport] .row')].every((r) => r.innerText.includes('Skip'))",
                 WRITE, "only skipped people show")


def people_select_all(app: App) -> None:
    """Select-all selects every matching person, not just the loaded rows."""
    app.go("/people", {"css": "[data-viewport] .row"}, PAGE)
    total = app.js(ROW_COUNT)
    app.click({"css": "[data-select-all]"})
    app.wait_for(f"(document.querySelector('[data-action-bar]')?.innerText ?? '').includes('{total:,} selected')",
                 WRITE, f"{total:,} selected")


def search_set_picker(app: App) -> None:
    """The chat's Search in picker opens, and a pick shows at once."""
    app.go("/agent", {"text": "Search in"}, PAGE)
    app.click({"text": "Search in"})
    app.wait_for({"css": "[role=listbox] [role=option]"}, CLICK, "the picker opens")
    options = app.js("[...document.querySelectorAll('[role=listbox] [role=option]')].map((o) => o.innerText.trim())")
    current = app.js("document.querySelector('[role=listbox] [aria-selected=true]')?.innerText.trim()")
    pick = next((option for option in options if option != current), None)
    if pick is None:
        raise Failed(f"only one place to search: {options}")
    app.click({"text": pick, "exact": True})
    app.wait_for({"text": pick.split("·")[0].strip()}, CLICK, f"the picker shows {pick!r}")


REFINED = "ML infra engineers · Bay Area"  # the root's quick search, refined twice in its chat
GO_BACK = 15000  # `rerank --use` on the chat: uv starts and copies a saved step; no model turn
REFINE = 120000  # a chat turn that runs `rerank --criteria` (the model picks it, then scores)
TAGS = "[...document.querySelectorAll('[data-step-trail] [data-step-tag]')].map((tag) => tag.innerText.replace('✕', '').trim())"
ROW_IDS = "[...document.querySelectorAll('.result-row')].map((row) => row.dataset.personId + ':' + (row.querySelector('.result-score')?.innerText ?? ''))"


def search_refine(app: App) -> None:
    """A refined search shows its tags; the first tag goes back to the search (the rows and the tags
    change), and a message refines what shows (a new tag, new rows). Needs the root's refined search
    linked to its chat and ChatGPT signed in; the second step spends model tokens."""
    app.go("/agent", {"text": REFINED}, PAGE)
    app.click({"text": REFINED})
    app.wait_for(f"{TAGS}.length >= 2", PAGE, "the refined search shows its tags")
    tags, rows = app.js(TAGS), app.js(ROW_IDS)
    if tags[0] != REFINED:
        raise Failed(f"the first tag is {tags[0]!r}, not the search's title")
    app.click({"css": "[data-step-trail] button[data-step-tag]"})
    app.wait_for(f"{TAGS}.length === 1 && {TAGS}[0] === {json.dumps(REFINED)}", GO_BACK,
                 "going back leaves only the search's tag")
    if app.js(ROW_IDS) == rows:
        raise Failed("went back to the search, but the rows and scores are the same")
    rows = app.js(ROW_IDS)
    app.click({"css": "#agent-message"})
    app.type("prefer people who mentor junior engineers")
    app.click({"css": "[aria-label='Send']"})
    app.wait_for(f"{TAGS}.length === 2 && /mentor/i.test({TAGS}[1])", REFINE, "the refinement adds its tag")
    if app.js(ROW_IDS) == rows:
        raise Failed("the refinement added its tag, but the rows and scores are the same")


# The pieces of each search card's header row (the query, "+N filters", the set, the step, the
# chevron) as boxes, and the pairs that overlap.
HEADER_OVERLAPS = """(() => {
  const pairs = [];
  for (const card of document.querySelectorAll("section[aria-label=Search]")) {
    const header = card.firstElementChild;
    const parts = [...header.children]
      .flatMap((el) => (el.tagName === "DIV" ? [...el.children] : [el]))
      .filter((el) => el.getClientRects().length);
    const boxes = parts.map((el) => [el.innerText.trim() || el.getAttribute("aria-label"), el.getBoundingClientRect()]);
    for (let i = 0; i < boxes.length; i++)
      for (let j = i + 1; j < boxes.length; j++) {
        const [a, p] = boxes[i], [b, q] = boxes[j];
        if (p.left < q.right - 1 && q.left < p.right - 1 && p.top < q.bottom - 1 && q.top < p.bottom - 1) pairs.push(a + " / " + b);
      }
  }
  return pairs;
})()"""


def search_card_narrow(app: App) -> None:
    """With the chat at its narrowest beside the results, the search card's header stacks its
    pieces instead of drawing the query, the set, "+N filters" and "Showing now" over each other,
    folded and opened. Needs the root's refined search linked to its chat."""
    app.js("localStorage.setItem('agent.results-share', '0.95')")  # the chat keeps its 320 px floor
    app.go("/people", {"css": "main"}, PAGE)
    app.go("/agent", {"text": REFINED}, PAGE)
    app.click({"text": REFINED})
    app.wait_for({"css": "section[aria-label=Search]"}, PAGE, "the chat shows its search card")
    try:
        overlaps = app.js(HEADER_OVERLAPS)
        if overlaps:
            raise Failed(f"folded, the card's header overlaps: {overlaps}")
        app.click({"css": "section[aria-label=Search] [aria-label='Show filters']"})
        app.wait_for({"text": "Search Filters"}, CLICK, "the card opens")
        overlaps = app.js(HEADER_OVERLAPS)
        if overlaps:
            raise Failed(f"opened, the card's header overlaps: {overlaps}")
    finally:
        app.js("localStorage.removeItem('agent.results-share')")


def refresh_toggle(app: App) -> None:
    """Scheduled tasks' daily switch flips on the click and the flip is saved (it survives a
    reload); it is put back as it was."""
    switch = {"css": "[role=switch][aria-label='Refresh every day']"}
    checked = "document.querySelector('[role=switch]')?.getAttribute('aria-checked')"
    app.go("/tasks", switch, PAGE)
    before = app.js(checked)
    for state in ("false" if before == "true" else "true", before):
        app.click(switch)
        app.wait_for(f"{checked} === {state!r}", CLICK, f"the switch shows {state}")
        time.sleep(WRITE / 1000)
        app.go("/tasks", switch, PAGE)
        if app.js(checked) != state:
            raise Failed(f"the switch went back to {app.js(checked)!r} after a reload")



# Answers Accounts' sign-in check with WhatsApp signed out, then signed in once `window.__qaLinked`
# is set, whatever the real answer. A page reload drops it.
FAKE_WHATSAPP_LINK = """(() => {
  if (!window.__qaFetch) window.__qaFetch = window.fetch;
  window.__qaLinked = false;
  window.fetch = async (input, init) => {
    const response = await window.__qaFetch(input, init);
    if (!String(input).startsWith('/accounts/api/signed-in')) return response;
    const body = await response.json();
    return new Response(JSON.stringify({ ...body, whatsapp: window.__qaLinked }), { headers: { 'Content-Type': 'application/json' } });
  };
  return true;
})()"""
PROMPT = {"css": "section[aria-label='Refresh after connecting']"}
WHATSAPP_ROW = "[...document.querySelectorAll('button[aria-pressed]')].find(b => b.textContent.startsWith('WhatsApp'))?.textContent ?? ''"


def accounts_refresh_prompt(app: App) -> None:
    """Linking iMessage, Gmail or WhatsApp on Accounts offers Refresh now once per source: never
    on a page that loads already connected, and not again for the same source after Later. The
    link is simulated (the sign-in check answers WhatsApp signed out, then signed in)."""
    app.js("(localStorage.removeItem('accounts.refreshPrompted'), true)")
    app.go("/accounts", {"text": "Your sources"}, PAGE)
    time.sleep(PAGE / 1000)
    if app.shows(PROMPT):
        raise Failed("the prompt showed on a page that loaded already connected")
    try:
        for attempt in ("first", "second"):
            app.go("/people", {"css": "main"}, PAGE)
            app.js(FAKE_WHATSAPP_LINK)
            app.go("/accounts", {"text": "Your sources"}, PAGE)
            app.wait_for(f"({WHATSAPP_ROW}).endsWith('Not connected')", PAGE, "WhatsApp shows Not connected")
            app.mark = app.js("performance.now()")
            app.js("(window.__qaLinked = true, dispatchEvent(new Event('visibilitychange')), true)")
            app.wait_for(f"({WHATSAPP_ROW}).endsWith('Connected') && !({WHATSAPP_ROW}).endsWith('Not connected')", PAGE, "WhatsApp turns Connected")
            if attempt == "first":
                app.mark = app.js("performance.now()")
                app.wait_for(PROMPT, CLICK, "the prompt offers Refresh now")
                app.picture("accounts-refresh-prompt")
                app.click({"css": PROMPT["css"] + " button:nth-of-type(2)"})
                app.wait_for(f"!document.querySelector({json.dumps(PROMPT['css'])})", CLICK, "Later closes it")
            else:
                time.sleep(CLICK / 1000)
                if app.shows(PROMPT):
                    raise Failed("the prompt showed a second time for WhatsApp")
    finally:
        app.js("(window.__qaFetch && (window.fetch = window.__qaFetch), true)")

SCENARIOS = {
    "people-open": people_open,
    "people-menus": people_menus,
    "people-keep-skip": people_keep_skip,
    "people-rename": people_rename,
    "people-linkedin": people_linkedin,
    "people-delete": people_delete,
    "people-scroll": people_scroll,
    "people-search": people_search,
    "people-tab": people_tab,
    "people-select-all": people_select_all,
    "search-set-picker": search_set_picker,
    "search-refine": search_refine,
    "search-card-narrow": search_card_narrow,
    "refresh-toggle": refresh_toggle,
    "accounts-refresh-prompt": accounts_refresh_prompt,
}
