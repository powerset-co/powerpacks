"""The Searches page in Chrome, served the way the review server composes it.

AppRoutes answers first (the React shell at /searches and /searches/run), then the
Searches JSON routes, then the legacy search routes. The bundle is web/dist, so run
`pnpm --dir web build` before this test after a web change.

Changelog:
  2026-09-26: created with the React Searches page.
  2026-09-26: the run's controls: tags (saved through /searches/tags), filters, score and
    search feedback (the feedback route is stubbed in the handler), CSV of the filtered rows.
"""

from __future__ import annotations

import csv
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
        "linkedin_url": "https://linkedin.com/in/" + name.lower().replace(" ", "-"),
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


def _handler(root: Path, feedback: list[dict[str, list[str]]]) -> type[BaseHTTPRequestHandler]:
    """review/server.py's order: the shell, the Searches JSON routes, the legacy routes.

    POST /searches/feedback is answered here ("submitted") and its form kept in `feedback`,
    so no test reaches Powerset; tags go to the real route and land in the run's tags.json.
    """
    app = AppRoutes()
    searches = search_routes(root, base="/searches")
    searches_json = search_api(searches)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if not (app.get(self, parsed) or searches_json.get(self, parsed) or searches.get(self, parsed)):
                _send(self, b"not found", "text/plain", status=404)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/searches/feedback":
                length = int(self.headers.get("Content-Length", "0"))
                feedback.append(urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8")))
                _send(self, json.dumps({"ok": True, "status": "submitted"}).encode(), "application/json")
            elif not searches.post(self, parsed):
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
        self.root = root
        self.feedback: list[dict[str, list[str]]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(root, self.feedback))
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

            # Every run, newest first; the search box is the one filter.
            rows = page.locator("[data-search-list] [data-run-id]")
            expect(rows).to_have_count(3)
            self.assertEqual([row.get_attribute("data-run-id") for row in rows.all()],
                             ["jordan-role", "casey-role", "morgan-role"])
            expect(page.locator("[data-version]")).to_have_count(0)
            expect(page.locator("[data-catalog-count]")).to_have_count(0)
            expect(page.locator("[data-run-empty]")).to_be_visible()
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
            expect(page.locator("[data-person-id='p-jordan'] .result-line")).to_have_attribute("aria-expanded", "true")
            # The LinkedIn mark under the name opens the profile, not the row.
            expect(page.locator("[data-person-id='p-jordan'] .result-linkedin")).to_have_attribute("href", "https://linkedin.com/in/jordan-bravo")

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
            expect(page.locator("[data-searches] [data-run-id]")).to_have_count(3)
            self.assertTrue(page.evaluate(kept))

            # A reload lands on the same run.
            page.goto(self.base + "/searches/run?run_id=casey-role")
            expect(page.locator("[data-run='casey-role'] h1")).to_have_text("Design Lead")
            self.assertEqual(errors, [])
            browser.close()


    def test_tag_filter_score_export_and_feedback(self) -> None:
        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900}, accept_downloads=True)
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(self.base + "/searches/run?run_id=jordan-role")
            names = page.locator(".result-row .result-who b")
            expect(names).to_have_text(["Jordan Bravo", "Morgan Echo", "Casey Delta"])
            count = page.locator("[data-result-count] [role=status]")
            expect(count).to_have_text("3 results")

            # Tag a person: the chip shows at once and the tags are saved to the run.
            add = page.get_by_role("button", name="Add tag to Casey Delta", exact=True)
            expect(add).to_be_enabled()
            add.click()
            field = page.get_by_role("textbox", name="Add tag", exact=True)
            field.fill("Backend | Infra")
            with page.expect_response(lambda response: response.url.endswith("/searches/tags")
                                      and response.request.method == "POST"):
                field.press("Enter")
            page.keyboard.press("Escape")
            chip = page.locator("[data-person-id='p-casey'] [data-tag='Backend | Infra']")
            expect(chip).to_be_visible()
            saved = json.loads((self.root / "jordan-role" / "tags.json").read_text())
            self.assertEqual(saved["assignments"], {"p-casey": ["Backend | Infra"]})

            # A reload reads them back through GET /searches/tags.
            page.reload()
            expect(chip).to_be_visible()

            # Tagged (1) keeps only the tagged person; the count reads the same rows.
            page.get_by_role("button", name="Tagged (1)", exact=True).click()
            expect(names).to_have_text(["Casey Delta"])
            expect(count).to_have_text("1 of 3 results")

            # CSV takes every filtered row, named for their tags.
            with page.expect_download() as download:
                page.get_by_role("button", name="CSV", exact=True).click()
            with open(download.value.path(), newline="") as handle:
                exported = list(csv.DictReader(handle))
            self.assertEqual([row["Title"] for row in exported], ["Staff Engineer"])
            self.assertIn("Casey Delta", exported[0]["Name"])
            self.assertTrue(download.value.suggested_filename.startswith("backend-infra_"))
            page.get_by_role("button", name="Tagged (1)", exact=True).click()
            expect(names).to_have_count(3)

            # The overall score filter (results.js data-score-filter).
            page.get_by_role("button", name="Overall score 5", exact=True).click()
            expect(names).to_have_text(["Jordan Bravo"])
            with page.expect_download() as download:
                page.get_by_role("button", name="CSV", exact=True).click()
            with open(download.value.path(), newline="") as handle:
                self.assertEqual([row["Overall Score"] for row in csv.DictReader(handle)], ["5"])
            page.get_by_role("button", name="All scores", exact=True).click()
            expect(names).to_have_count(3)

            # Score a person: the badge shows it and the record is sent on the five-point scale.
            page.get_by_role("button", name="Score Morgan Echo", exact=True).click()
            page.get_by_role("radio", name="Score 3:").check()
            page.get_by_role("dialog").get_by_role("textbox").fill("Worth a call")
            page.get_by_role("button", name="Save", exact=True).click()
            expect(page.get_by_role("button", name="Score Morgan Echo", exact=True)).to_have_text("Your score: 3/5")
            page.wait_for_function("localStorage.getItem('powerpacks:pending-feedback:v1') === '[]'")

            # Search feedback from the header.
            page.get_by_role("button", name="Send feedback about Backend Engineer", exact=True).click()
            page.get_by_role("dialog").get_by_role("textbox").fill("Too senior overall")
            page.get_by_role("button", name="Send", exact=True).click()
            expect(page.get_by_text("Sent.", exact=True)).to_be_visible()
            page.wait_for_function("localStorage.getItem('powerpacks:pending-feedback:v1') === '[]'")
            self.assertEqual(self.feedback, [
                {"run_id": ["jordan-role"], "person_id": ["p-morgan"], "comment": ["Worth a call"],
                 "human_judgment": ['{"score":3,"scale":5}']},
                {"run_id": ["jordan-role"], "comment": ["Too senior overall"]},
            ])

            # Untag: the chip leaves and the saved tags follow.
            page.get_by_role("button", name="Edit tags for Casey Delta", exact=True).click()
            with page.expect_response(lambda response: response.url.endswith("/searches/tags")
                                      and response.request.method == "POST"):
                page.get_by_role("dialog", name="Tags for Casey Delta").get_by_role("button", name="Backend | Infra", exact=True).click()
            page.keyboard.press("Escape")
            expect(chip).to_have_count(0)
            self.assertEqual(json.loads((self.root / "jordan-role" / "tags.json").read_text())["assignments"], {})
            self.assertEqual(errors, [])
            browser.close()


if __name__ == "__main__":
    unittest.main()
