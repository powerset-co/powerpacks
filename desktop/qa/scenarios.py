"""The flows a person clicks through, each a list of checks with a time budget.

Each one is the shape of a bug that passed unit tests and was found by hand in the app: a menu
under the drawer (#963), a rename field that ate keystrokes (#963), a LinkedIn link that did
nothing (#1002), Keep/Skip and delete taking seconds (#901, #902), a set pick that didn't take
(#803). Budgets are for one person's local data on a laptop.
"""

from __future__ import annotations

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


SCENARIOS = {
    "people-open": people_open,
    "people-menus": people_menus,
    "people-keep-skip": people_keep_skip,
    "people-rename": people_rename,
    "people-linkedin": people_linkedin,
    "people-delete": people_delete,
    "search-set-picker": search_set_picker,
}
