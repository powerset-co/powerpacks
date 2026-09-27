"""The search catalog registers new runs and reads compact saved summaries."""

from __future__ import annotations

import json
import tempfile
import threading
from http.server import ThreadingHTTPServer
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from packs.search.primitives.deep_search import search_harness
from packs.search.primitives.deep_search.results_web import model
from packs.search.primitives.deep_search.results_web.model import load_catalog, load_search, load_searches
from packs.search.primitives.deep_search.results_web.server import _run_loader, make_handler

CANDIDATE = "0b6f8f3e-8f3e-4e6f-9a2b-1c2d3e4f5a6b"


def _write_run(root: Path, run_id: str, *, title: str, company: str, created_at: str, status: str,
               search_version: str | None = None, pond_chain: list | None = None,
               found_by: list | None = None, manifest: bool = True) -> Path:
    run = root / run_id
    run.mkdir(parents=True)
    candidates = run / "candidates.jsonl"
    candidates.write_text(json.dumps({"person_id": CANDIDATE, "cross_encoder_score": 0.8}) + "\n")
    results = {
        "schema_version": "search-harness.v1", "jd_id": run_id, "status": status,
        "title": title, "company": company, "created_at": created_at, "updated_at": created_at,
        "iterations": [{"pond_n": 1, "query": f"{title} query", "arm": {"artifacts": {"jsonl": str(candidates)}}}],
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
        model.index_search(self.root, "sail-role", json.loads(
            (self.root / "sail-role" / "manifest.json").read_text()))

    def test_catalog_reads_only_registered_rows_without_opening_runs(self) -> None:
        with mock.patch.object(Path, "read_text", autospec=True, side_effect=Path.read_text) as reads:
            cards = load_catalog(self.root)
        opened = [call.args[0].name for call in reads.call_args_list]
        self.assertEqual(opened, ["catalog.json"])
        self.assertEqual([(card.run_id, card.search_version, card.company) for card in cards],
                         [("sail-role", "2026-09-26", "Sail Research")])
        self.assertEqual(cards[0].candidates, 1)

    def test_catalog_does_not_scan_unregistered_history(self) -> None:
        (self.root / "catalog.json").unlink()
        self.assertEqual(load_catalog(self.root), ())

    def test_catalog_counts_overall_scores_and_updates_pins_without_loading_results(self) -> None:
        path = self.root / "sail-role" / "results.json"
        results = json.loads(path.read_text())
        rows = results["summary"]["groups"]["send_worthy"]
        for score in (5, 4, 3):
            rows.append({"person": f"candidate-{score}", "name": "Casey Example", "found_by": [],
                         "candidate_judgment": {"overall_score": score, "model": "test", "status": "ok"}})
        path.write_text(json.dumps(results))
        with (path.parent / "candidates.jsonl").open("a") as artifact:
            for score in (5, 4, 3):
                artifact.write(json.dumps({"person_id": f"candidate-{score}", "cross_encoder_score": score}) + "\n")
        tagged = {"tags": ["pinned", "Review"], "assignments": {CANDIDATE: ["pinned"], "candidate-5": ["Review"]}}
        (path.parent / "tags.json").write_text(json.dumps(tagged))
        model.index_search(self.root, "sail-role", json.loads((path.parent / "manifest.json").read_text()))
        card = load_catalog(self.root)[0]
        self.assertEqual((card.candidates, card.pinned, card.score_5, card.score_4, card.score_3), (4, 1, 1, 1, 1))
        tagged["assignments"]["candidate-5"] = ["pinned"]
        with mock.patch.object(model, "load_search", side_effect=AssertionError("must not load results")):
            model.update_catalog_pins(self.root, "sail-role", tagged)
        self.assertEqual(load_catalog(self.root)[0].pinned, 2)
        model.update_catalog_pins(self.root, "prior-role", tagged)
        self.assertEqual(len(load_catalog(self.root)), 1)

    def test_empty_groups_count_saved_grades_once_across_duplicate_ponds(self) -> None:
        path = self.root / "sail-role" / "results.json"
        results = json.loads(path.read_text())
        results["summary"]["groups"] = {}
        results["summary"]["pond_chain"] = [
            {"run": "sail-role", "pond_n": 1}, {"run": "sail-role", "pond_n": 1}]
        results["iterations"][0]["shortlist_grades"] = [
            {"person": CANDIDATE, "cross_encoder_score": 5,
             "candidate_judgment": {"overall_score": 4}}]
        path.write_text(json.dumps(results))
        model.index_search(self.root, "sail-role", json.loads((path.parent / "manifest.json").read_text()))
        card = load_catalog(self.root)[0]
        self.assertEqual((card.candidates, card.score_5, card.score_4, card.score_3), (1, 0, 1, 0))

    def test_ce_counts_follow_artifacts_and_exclude_unscored_summary_people(self) -> None:
        path = self.root / "sail-role" / "results.json"
        results = json.loads(path.read_text())
        results["summary"]["groups"]["send_worthy"].append({
            "person": "unscored-person", "cross_encoder_score": 5,
            "candidate_judgment": {"overall_score": 5}})
        path.write_text(json.dumps(results))
        manifest = json.loads((path.parent / "manifest.json").read_text())
        model.index_search(self.root, "sail-role", manifest)
        card = load_catalog(self.root)[0]
        self.assertEqual((card.ce_scored, card.candidates, card.score_5), (1, 1, 0))
        for run_id in ("sail-role", "prior-role"):
            (self.root / run_id / "candidates.jsonl").write_text(
                json.dumps({"person_id": CANDIDATE, "cross_encoder_score": None}) + "\n")
        model.index_search(self.root, "sail-role", manifest)
        card = load_catalog(self.root)[0]
        self.assertEqual((card.ce_scored, card.candidates, card.score_5), (0, 0, 0))
        self.assertEqual(len(load_catalog(self.root)), 1)

    def test_saving_history_does_not_register_it(self) -> None:
        run = self.root / "prior-role"
        results = json.loads((run / "results.json").read_text())
        search_harness._save(results, run)
        self.assertEqual([card.run_id for card in load_catalog(self.root)], ["sail-role"])

    def test_initialize_registers_new_search_and_save_refreshes_it(self) -> None:
        run = self.root / "new-role"
        jd = self.root / "jd.txt"
        jd.write_text("Backend Engineer")
        queries = self.root / "queries.json"
        queries.write_text(json.dumps([{"key": "role", "query": "backend engineer"}]))
        search_harness.initialize_run(run_dir=run, jd_path=jd, queries_path=queries, retrieval={})
        cards = {card.run_id: card for card in load_catalog(self.root)}
        self.assertEqual(cards["new-role"].search_version, search_harness.SEARCH_VERSION)
        self.assertEqual((cards["new-role"].candidates, cards["new-role"].ce_scored), (0, 0))
        results = json.loads((run / "results.json").read_text())
        results["status"] = "awaiting_diagnosis"
        search_harness._save(results, run)
        self.assertEqual(next(card.status for card in load_catalog(self.root) if card.run_id == "new-role"),
                         "awaiting_diagnosis")

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
        self.assertNotIn("Prior Role", index)
        self.assertEqual(index.count("class='catalog-row'"), 1)
        self.assertNotIn("data-search-body", index)
        self.assertIn("data-search-body='sail-role'", page)
        self.assertEqual(page.count("class='search-card'"), 1)
        self.assertEqual((health["searches"], missing.exception.code), (1, 404))


if __name__ == "__main__":
    unittest.main()
