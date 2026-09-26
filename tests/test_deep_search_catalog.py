"""The search catalog: manifest display cells, the version stamp, and the lazy list."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from packs.search.primitives.deep_search import search_harness
from packs.search.primitives.deep_search.results_web.model import load_catalog, load_search, load_searches
from packs.search.primitives.deep_search.results_web.server import ThreadingHTTPServer, _run_loader, make_handler

CANDIDATE = "0b6f8f3e-8f3e-4e6f-9a2b-1c2d3e4f5a6b"


def _write_run(root: Path, run_id: str, *, title: str, company: str, created_at: str, status: str,
               search_version: str | None = None, pond_chain: list | None = None,
               found_by: list | None = None, manifest: bool = True) -> Path:
    run = root / run_id
    run.mkdir(parents=True)
    results = {
        "schema_version": "search-harness.v1", "jd_id": run_id, "status": status,
        "title": title, "company": company, "created_at": created_at, "updated_at": created_at,
        "iterations": [{"pond_n": 1, "query": f"{title} query", "arm": {"artifacts": {}}}],
        "summary": {
            "deduped_candidate_count": 1 if found_by is not None else 0,
            "pond_chain": pond_chain or [{"run": run_id, "pond_n": 1, "query": f"{title} query",
                                          "result_count": 3, "cost_usd": 0.25}],
            "groups": {"send_worthy": [{"person": CANDIDATE, "name": "Jordan Bravo", "rerank_score": 0.9,
                                        "found_by": found_by}]} if found_by is not None else {},
            "total_cost_usd": 0.25,
        },
    }
    if search_version:
        results["search_version"] = search_version
    run.joinpath("results.json").write_text(json.dumps(results), encoding="utf-8")
    run.joinpath("usage.jsonl").write_text(json.dumps({"cost_usd": 0.25}) + "\n", encoding="utf-8")
    if manifest:
        run.joinpath("manifest.json").write_text(json.dumps({
            "schema_version": "search-harness.manifest.v1", "status": status, "jd_id": run_id,
            "ponds_run": 1, "cost_usd": 0.25}), encoding="utf-8")
    return run


class ManifestTests(unittest.TestCase):
    def test_new_runs_are_stamped_and_the_manifest_carries_the_display_cells(self) -> None:
        results = search_harness.build_initial_results(
            {"company_name": "Acme", "source_title": "Backend Engineer"},
            [{"key": "role", "query": "backend engineers in oakland"}], job_id="acme-backend")
        self.assertEqual(results["search_version"], search_harness.SEARCH_VERSION)
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "acme-backend"
            run.mkdir()
            results["summary"] = {"deduped_candidate_count": 7}
            results["updated_at"] = "2026-09-26T00:00:00Z"
            manifest = search_harness._manifest(results, run)
        self.assertEqual({key: manifest[key] for key in ("title", "company", "candidates", "search_version")},
                         {"title": "Backend Engineer", "company": "Acme", "candidates": 7,
                          "search_version": search_harness.SEARCH_VERSION})
        self.assertEqual((manifest["created_at"], manifest["updated_at"]),
                         (results["created_at"], "2026-09-26T00:00:00Z"))

    def test_backfill_writes_display_cells_once_keeps_a_backup_and_never_stamps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = _write_run(root, "old-role", title="Old Role", company="Acme",
                             created_at="2026-08-01T00:00:00Z", status="completed")
            _write_run(root, "no-manifest", title="Loose", company="Acme",
                       created_at="2026-08-02T00:00:00Z", status="completed", manifest=False)
            before = old.joinpath("results.json").read_bytes()
            self.assertEqual(search_harness.backfill_manifests(root), ["old-role"])
            manifest = json.loads(old.joinpath("manifest.json").read_text())
            self.assertEqual((manifest["title"], manifest["company"], manifest["created_at"], manifest["candidates"]),
                             ("Old Role", "Acme", "2026-08-01T00:00:00Z", 0))
            self.assertIsNone(manifest["search_version"])
            self.assertEqual(len(list(old.glob("manifest.json.bkup-*"))), 1)
            self.assertEqual(old.joinpath("results.json").read_bytes(), before)
            self.assertEqual(search_harness.backfill_manifests(root), [])
            self.assertFalse((root / "no-manifest" / "manifest.json").exists())


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        _write_run(self.root, "prior-role", title="Prior Role", company="Acme",
                   created_at="2026-08-01T00:00:00Z", status="completed")
        _write_run(self.root, "sail-role", title="Sail Role", company="Sail Research",
                   created_at="2026-09-20T00:00:00Z", status="awaiting_diagnosis", search_version="2026-09-26",
                   pond_chain=[{"run": "prior-role", "pond_n": 1, "query": "Prior Role query", "result_count": 3,
                                "cost_usd": 0.25},
                               {"run": "sail-role", "pond_n": 1, "query": "Sail Role query", "result_count": 5,
                                "cost_usd": 0.25}],
                   found_by=[{"run": "prior-role", "pond": 1, "query": "Prior Role query"}])
        search_harness.backfill_manifests(self.root)

    def test_catalog_reads_manifests_only_newest_first(self) -> None:
        with mock.patch.object(Path, "read_text", autospec=True, side_effect=Path.read_text) as reads:
            cards = load_catalog(self.root)
        opened = [call.args[0].name for call in reads.call_args_list]
        self.assertNotIn("results.json", opened)
        self.assertEqual([(card.run_id, card.search_version, card.company) for card in cards],
                         [("sail-role", "2026-09-26", "Sail Research"), ("prior-role", "", "Acme")])
        self.assertEqual(cards[0].candidates, 1)

    def test_one_run_loads_the_runs_its_summary_references(self) -> None:
        search = load_search(self.root, "sail-role")
        self.assertEqual([pond.run_id for pond in search.ponds], ["prior-role", "sail-role"])
        self.assertEqual(load_searches(self.root, "sail-role")[0].run_id, "sail-role")
        self.assertIsNone(load_search(self.root, "missing"))
        loader = _run_loader(self.root)
        self.assertIs(loader("sail-role"), loader("sail-role"))
        self.assertIsNone(loader("../sail-role"))

    def test_list_page_opens_no_results_body_and_run_page_renders_one(self) -> None:
        loader = _run_loader(self.root)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            self.root, lambda: (), catalog=lambda: load_catalog(self.root), load_one=loader))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with mock.patch.object(Path, "read_text", autospec=True, side_effect=Path.read_text) as reads:
                with urllib.request.urlopen(base + "/", timeout=5) as response:
                    index = response.read().decode("utf-8")
            self.assertNotIn("results.json", [call.args[0].name for call in reads.call_args_list])
            with urllib.request.urlopen(base + "/run?run_id=sail-role", timeout=5) as response:
                page = response.read().decode("utf-8")
            with urllib.request.urlopen(base + "/healthz", timeout=5) as response:
                health = json.loads(response.read())
            with self.assertRaises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(base + "/run?run_id=nope", timeout=5)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertIn("data-catalog", index)
        self.assertIn("data-newest-version='2026-09-26'", index)
        self.assertIn("data-version='unversioned'", index)
        self.assertEqual(index.count("class='catalog-row'"), 2)
        self.assertNotIn("data-search-body", index)
        self.assertIn("data-search-body='sail-role'", page)
        self.assertEqual(page.count("class='search-card'"), 1)
        self.assertEqual((health["searches"], missing.exception.code), (2, 404))


class BrowserCatalogTests(CatalogTests):
    def test_version_chips_filter_and_arrows_move(self) -> None:
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("Playwright is not installed")
        loader = _run_loader(self.root)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            self.root, lambda: (), catalog=lambda: load_catalog(self.root), load_one=loader))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel="chrome", headless=True)
                page = browser.new_page(viewport={"width": 1300, "height": 800})
                page.goto(f"http://127.0.0.1:{server.server_address[1]}/")
                # The newest stamped version is selected by default; the unversioned run is hidden.
                expect(page.locator(".catalog-row:visible")).to_have_count(1)
                expect(page.locator("[data-catalog-count]")).to_have_text("1 of 2")
                page.get_by_role("button", name="2026-09-26").click()
                expect(page.locator(".catalog-row:visible")).to_have_count(2)
                page.get_by_role("button", name="unversioned").click()
                expect(page.locator(".catalog-row:visible")).to_have_count(1)
                expect(page.locator(".catalog-row:visible .catalog-title strong")).to_have_text("Prior Role")
                page.get_by_role("button", name="unversioned").click()
                page.keyboard.press("j")
                page.keyboard.press("j")
                expect(page.locator(".catalog-row:focus .catalog-title strong")).to_have_text("Prior Role")
                page.keyboard.press("Enter")
                page.wait_for_url("**/run?run_id=prior-role")
                expect(page.locator(".search-identity strong")).to_have_text("Prior Role")
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
