"""The Searches page in Chrome, served the way the review server composes it.

AppRoutes answers first (the React shell at /searches and /searches/run), then the
Searches JSON routes, then the legacy search routes. The bundle is web/dist, so run
`pnpm --dir web build` before this test after a web change.

Changelog:
  2026-09-26: created with the React Searches page.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from packs.search.primitives.deep_search import search_harness
from packs.search.primitives.deep_search.results_web import model
from packs.search.primitives.deep_search.results_web.api import search_api
from packs.search.primitives.deep_search.results_web.server import _send, search_routes
from packs.shared.web.app import AppRoutes
from test_deep_search_catalog import _write_run

VERSION = "2026-09-26"
RATING = "expected_rating_1_to_5"
QUALIFICATION = "qualification_score"


def _person(person_id: str, name: str, *, ce: float, kind: str = RATING, passed: bool | None = None,
            reasoning: str = "") -> dict:
    return {
        "person_id": person_id, "name": name, "current_titles": "Staff Engineer",
        "current_companies": "Example Labs", "location": "Oakland, CA", "final_score": 0.8,
        "trait_scores": {"Backend depth": {"score": 0.9, "confidence": 0.8, "reason": "Built the API layer."}},
        "overall_reasoning": reasoning, "vertical_sources": ["title"], "matched_position_indexes": [0],
        "cross_encoder_score": ce, "cross_encoder_score_1_to_5": ce if kind == RATING else None,
        "cross_encoder_score_type": kind, "cross_encoder_passed": passed, "cross_encoder_status": "ok",
    }


def _profile(person_id: str) -> dict:
    return {"person_id": person_id, "positions": [
        {"position_title": "Staff Engineer", "company_name": "Example Labs", "start_date": "2022-01-01",
         "is_current": True}],
        "education": [{"school_name": "Example University", "degree": "BS", "end_year": 2014}]}


# run id, title, company, created, version, people, judged: person -> overall.
RUNS = (
    ("jordan-role", "Backend Engineer", "Example Labs", "2026-09-26T09:00:00Z", VERSION, (
        _person("p-casey", "Casey Delta", ce=4.1),
        _person("p-jordan", "Jordan Bravo", ce=4.6, reasoning="Leads the current reliability platform."),
        _person("p-morgan", "Morgan Echo", ce=2.4),
    ), {"p-jordan": 5}),
    ("casey-role", "Design Lead", "Sample Co", "2026-09-25T09:00:00Z", VERSION, (
        _person("p-avery", "Avery Golf", ce=3.5),
        _person("p-riley", "Riley Foxtrot", ce=0.85, kind=QUALIFICATION, passed=True),
    ), {}),
    ("morgan-role", "Data Scientist", "Example Labs", "2026-09-01T09:00:00Z", "2026-09-01", (
        _person("p-quinn", "Quinn Hotel", ce=4.0),
    ), {}),
)


def _judgment(overall: int) -> dict:
    return {"domain": {"score": overall, "why": "Built this exact system twice."},
            "opportunity": {"cap": 5, "why": "Opportunity fits."}, "overall_score": overall,
            "model": "test", "status": "ok"}


def _results_root(root: Path) -> None:
    """Synthetic runs as the harness saves them, registered in the catalog."""
    for run_id, title, company, created, version, people, judged in RUNS:
        query = f"{title} query"
        run = _write_run(root, run_id, title=title, company=company, created_at=created, status="completed",
                         search_version=version, found_by=[{"run": run_id, "pond": 1, "query": query}])
        run.joinpath("candidates.jsonl").write_text("".join(json.dumps(row) + "\n" for row in people))
        run.joinpath("profiles.jsonl").write_text(
            "".join(json.dumps(_profile(row["person_id"])) + "\n" for row in people))
        results = json.loads(run.joinpath("results.json").read_text())
        results["iterations"][0]["arm"]["artifacts"]["profiles_path"] = str(run / "profiles.jsonl")
        results["summary"]["groups"] = {"send_worthy": [
            {"person": row["person_id"], "name": row["name"],
             "found_by": [{"run": run_id, "pond": 1, "query": query}],
             **({"candidate_judgment": _judgment(judged[row["person_id"]])} if row["person_id"] in judged else {})}
            for row in people]}
        results["person_attribution"] = {"p-jordan": {
            "person_id": "p-jordan", "total_interactions": 42,
            "sources": [{"channel": "gmail", "total_interactions": 42, "operator_count": 1},
                        {"channel": "linkedin", "total_interactions": 0, "operator_count": 1}],
            "operators": [{"operator_id": "op-1", "operator_name": "Drew Kilo", "channels": ["gmail"],
                           "gmail_interactions": 42}]}}
        run.joinpath("results.json").write_text(json.dumps(results))
    search_harness.backfill_manifests(root)
    for run_id, *_ in RUNS:
        model.index_search(root, run_id, json.loads((root / run_id / "manifest.json").read_text()))


def _handler(root: Path) -> type[BaseHTTPRequestHandler]:
    """review/server.py's order: the shell, the Searches JSON routes, the legacy routes."""
    app = AppRoutes()
    searches = search_routes(root, base="/searches")
    searches_json = search_api(searches)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if not (app.get(self, parsed) or searches_json.get(self, parsed) or searches.get(self, parsed)):
                _send(self, b"not found", "text/plain", status=404)

        def log_message(self, fmt: str, *args: object) -> None:
            pass

    return Handler


class SearchesBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError:
            self.skipTest("Playwright is not installed")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        _results_root(root)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(root))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def test_pick_expand_switch_back_and_people_keep_one_document(self) -> None:
        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(self.base + "/searches")
            page.evaluate("window.__kept = true")
            kept = "window.__kept === true"

            # The newest version is preselected: the older run waits under "All".
            rows = page.locator("[data-search-list] [data-run-id]")
            expect(rows).to_have_count(2)
            self.assertEqual([row.get_attribute("data-run-id") for row in rows.all()], ["jordan-role", "casey-role"])
            expect(page.locator(f"[data-version='{VERSION}']")).to_have_attribute("aria-pressed", "true")
            expect(page.get_by_role("button", name="unversioned")).to_have_count(0)
            expect(page.locator("[data-catalog-count]")).to_have_text("2 of 3 searches")
            expect(page.locator("[data-run-empty]")).to_be_visible()
            page.locator("[data-version='all']").click()
            expect(rows).to_have_count(3)
            page.locator("[data-filter-text]").fill("design")
            expect(rows).to_have_count(1)
            page.locator("[data-filter-text]").fill("")

            # Picking a run swaps the pane in place and pushes its URL.
            page.locator("[data-run-id='jordan-role']").click()
            expect(page).to_have_url(self.base + "/searches/run?run_id=jordan-role")
            expect(page.locator("[data-run='jordan-role'] h1")).to_have_text("Backend Engineer")
            names = page.locator(".result-row .result-who b")
            expect(names).to_have_text(["Jordan Bravo", "Morgan Echo", "Casey Delta"])
            expect(page.locator("[data-person-id='p-jordan'] [data-overall]")).to_have_text("5/5")
            expect(page.locator("[data-person-id='p-casey'] .result-unscored")).to_have_text("Not judged")
            expect(page.locator("[data-person-id='p-jordan'] .source")).to_have_count(2)
            self.assertTrue(page.evaluate(kept))

            # A row opens in place to its evidence.
            page.locator("[data-person-id='p-jordan'] .result-main").click()
            evidence = page.locator("[data-person-id='p-jordan'] [data-evidence]")
            expect(evidence).to_contain_text("Leads the current reliability platform.")
            expect(evidence).to_contain_text("Example University")
            expect(page.locator("[data-person-id='p-jordan'] .result-main")).to_have_attribute("aria-expanded", "true")

            # Another run: the mixed scales show as two tables.
            page.locator("[data-run-id='casey-role']").click()
            expect(page).to_have_url(self.base + "/searches/run?run_id=casey-role")
            expect(page.locator("[data-run='casey-role'] .results-heading")).to_have_text(
                ["Jev qualification scores", "Rating-based scores"])
            expect(names).to_have_text(["Riley Foxtrot", "Avery Golf"])
            expect(page.locator("[data-run-id='casey-role']")).to_have_attribute("aria-current", "page")

            # Back and forward move between the runs.
            page.go_back()
            expect(page.locator("[data-run='jordan-role']")).to_be_visible()
            page.go_forward()
            expect(page.locator("[data-run='casey-role']")).to_be_visible()
            self.assertTrue(page.evaluate(kept))

            # The People tab and back keep the shell and the document.
            page.get_by_role("link", name="People").click()
            expect(page).to_have_url(self.base + "/people")
            expect(page.locator("[data-people]")).to_be_visible()
            page.get_by_role("link", name="Searches").click()
            expect(page).to_have_url(self.base + "/searches")
            expect(page.locator("[data-searches] [data-run-id]")).to_have_count(2)
            self.assertTrue(page.evaluate(kept))

            # A reload lands on the same run.
            page.goto(self.base + "/searches/run?run_id=casey-role")
            expect(page.locator("[data-run='casey-role'] h1")).to_have_text("Design Lead")
            self.assertEqual(errors, [])
            browser.close()


if __name__ == "__main__":
    unittest.main()
