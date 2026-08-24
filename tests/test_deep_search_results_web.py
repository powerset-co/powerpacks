"""Static deep-search results viewer: artifact joins, rendering, and feedback."""

from __future__ import annotations

import gzip
import json
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path

from packs.search.primitives.deep_search.results_web.feedback import build_feedback_request
from packs.search.primitives.deep_search.results_web.model import load_searches
from packs.search.primitives.deep_search.results_web.rendering import render_page, render_search_body
from packs.search.primitives.deep_search.results_web.server import (
    ThreadingHTTPServer,
    build_parser,
    make_handler,
)


class ResultsWebTest(unittest.TestCase):
    PERSON = "0b6f8f3e-8f3e-4e6f-9a2b-1c2d3e4f5a6b"

    def _pond_artifacts(self, base: Path, name: str, *, score: float,
                        title: str, company: str, query: str) -> dict[str, object]:
        artifact_dir = base / "artifacts" / name
        artifact_dir.mkdir(parents=True)
        results_path = artifact_dir / "results.jsonl"
        results_path.write_text(json.dumps({
            "person_id": self.PERSON,
            "name": "Jordan Bravo",
            "current_titles": title,
            "current_companies": company,
            "location": "Oakland, California",
            "final_score": str(score),
            "trait_scores": json.dumps({
                "Pond collaboration": {
                    "score": 0.61,
                    "confidence": 0.73,
                    "reason": f"Jordan collaborated on the {name} system.",
                },
                "Pond-native distributed systems": {
                    "score": score,
                    "confidence": 0.91,
                    "reason": f"Jordan shipped the {name} system.",
                },
            }),
        }) + "\n", encoding="utf-8")
        profiles_path = artifact_dir / "profiles.jsonl.gz"
        with gzip.open(profiles_path, "wt", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "person_id": self.PERSON,
                "profile_picture_url": f"https://example.com/{name}.jpg",
            }) + "\n")
        return {
            "pond_n": 1,
            "query": query,
            "pool_stats": {"score_histogram": {
                "0.9+": 1, "0.8-0.9": 2, "0.7-0.8": 3,
                "0.6-0.7": 4, "below 0.6": 5,
            }},
            "arm": {"artifacts": {
                "jsonl": str(results_path.relative_to(base)),
                "profiles_path": str(profiles_path.relative_to(base)),
            }},
        }

    def _fixture(self, directory: str, *, with_jd: bool = False,
                 schema_version: str = "search-harness.v1") -> Path:
        base = Path(directory)
        root = base / ".powerpacks" / "deep-search"
        current = root / "jordan-role"
        prior = root / "jordan-role-prior"
        current.mkdir(parents=True)
        prior.mkdir(parents=True)
        if with_jd:
            current.joinpath("evaluation-traits.json").write_text(json.dumps([
                {"meaning": "nice-to-have", "temporal": "all",
                 "value": "JD collaboration"},
                {"meaning": "core", "temporal": "all",
                 "value": "JD distributed systems"},
            ]), encoding="utf-8")
        current_iteration = self._pond_artifacts(
            base, "current", score=0.72, title="Software Engineer",
            company="Example Labs", query="Software Engineer in Oakland")
        prior_iteration = self._pond_artifacts(
            base, "prior", score=0.88, title="Senior Software Engineer",
            company="Bravo Systems", query="Distributed systems engineer")
        prior.joinpath("results.json").write_text(json.dumps({
            "schema_version": schema_version,
            "iterations": [prior_iteration],
        }), encoding="utf-8")
        candidate = {
            "person": self.PERSON,
            "name": "Jordan Bravo",
            "linkedin_url": "https://linkedin.com/in/jordan-bravo",
            "anchored_score": 0.88,
            "rerank_score": 0.88,
            "level": "Senior individual contributor",
            "timing": "28 months in seat",
            "pedigree_prior": "strong",
            "move_plausibility": "in-band",
            "why": "Jordan has direct distributed systems evidence.",
            "found_by": [
                {"run": "jordan-role", "pond": 1,
                 "query": "Software Engineer in Oakland"},
                {"run": "jordan-role-prior", "pond": 1,
                 "query": "Distributed systems engineer"},
            ],
        }
        if with_jd:
            candidate["jd_trait_scores"] = json.dumps({
                "JD collaboration": {
                    "score": 0.58,
                    "confidence": 0.76,
                    "reason": "The JD rubric found collaboration evidence.",
                },
                "JD distributed systems": {
                    "score": 0.93,
                    "confidence": 0.95,
                    "reason": "The JD rubric found direct systems evidence.",
                },
            })
        current.joinpath("results.json").write_text(json.dumps({
            "schema_version": schema_version,
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "created_at": "2026-08-24T10:00:00Z",
            "iterations": [current_iteration],
            "summary": {
                "total_cost_usd": 0.42,
                "pond_chain": [
                    {"run": "jordan-role", "pond_n": 1,
                     "query": "Software Engineer in Oakland",
                     "diagnosis": "wrong_specialty", "move": "add_adjacent_pond",
                     "result_count": 50, "cost_usd": 0.1},
                    {"run": "jordan-role-prior", "pond_n": 1,
                     "query": "Distributed systems engineer",
                     "diagnosis": None, "move": "stop",
                     "result_count": 20, "cost_usd": 0.2},
                ],
                "groups": {
                    "send_worthy": [candidate], "chat_worthy": [],
                    "wrong_timing_relationship": [], "passed": [],
                },
            },
        }), encoding="utf-8")
        # A non-search result artifact in the same root is ignored.
        corpus = root / "jd-memory-corpus"
        corpus.mkdir()
        corpus.joinpath("results.json").write_text("[]", encoding="utf-8")
        return root

    def test_loads_summary_and_joins_best_pond_traits_and_avatar(self):
        with tempfile.TemporaryDirectory() as directory:
            searches = load_searches(self._fixture(directory))
        self.assertEqual(len(searches), 1)
        search = searches[0]
        self.assertEqual([pond.result_count for pond in search.ponds], [50, 20])
        self.assertEqual([pond.good_count for pond in search.ponds], [1, 1])
        candidate = search.groups[0].candidates[0]
        self.assertEqual(candidate.title, "Senior Software Engineer")
        self.assertEqual(candidate.company, "Bravo Systems")
        self.assertEqual(candidate.location, "Oakland, California")
        self.assertEqual(candidate.avatar_url, "https://example.com/prior.jpg")
        self.assertEqual(candidate.found_run, "jordan-role-prior")
        self.assertEqual(candidate.pond_traits[0].name, "Pond collaboration")
        self.assertEqual(candidate.jd_traits, ())
        self.assertFalse(search.has_jd_ranking)

    def test_current_schema_renders_only_pond_native_table(self):
        with tempfile.TemporaryDirectory() as directory:
            search = load_searches(self._fixture(directory))[0]
            page = render_page((search,))
            detail = render_search_body(search)
        self.assertIn("data-search-body='jordan-role'", page)
        for expected in (
            "Jordan Bravo", "Senior Software Engineer", "Bravo Systems",
            "Oakland, California", "88%", "Pond-native distributed systems",
            "Jordan shipped the prior system.", "Senior individual contributor",
            "Pond Ranking", "results-table", "trait-indicator",
            "91% confidence", "1</strong> good",
            "https://linkedin.com/in/jordan-bravo", "data-feedback-person",
        ):
            self.assertIn(expected, detail)
        self.assertNotIn("JD Ranking", detail)
        self.assertNotIn("Beta", detail)
        self.assertNotIn("trait-indicator-core", detail)
        self.assertNotIn("score-histogram", detail)
        self.assertNotIn("candidate-card", detail)
        self.assertNotIn("trait-strip", detail)
        self.assertEqual(detail.count("class='results-table'"), 1)
        self.assertIn("data-ranking-tab='pond'>Pond Ranking", detail)
        self.assertIn("role='tab' aria-selected='true'", detail)

    def test_jd_ranking_requires_explicit_jd_scores(self):
        with tempfile.TemporaryDirectory() as directory:
            search = load_searches(self._fixture(directory, with_jd=True))[0]
            detail = render_search_body(search)
        self.assertTrue(search.has_jd_ranking)
        self.assertEqual(search.groups[0].candidates[0].jd_traits[0].name,
                         "JD distributed systems")
        self.assertIn("JD Ranking", detail)
        self.assertIn("Beta", detail)
        self.assertEqual(detail.count("class='results-table'"), 2)
        self.assertIn("data-ranking-panel='jd' hidden", detail)
        pond_panel, jd_panel = detail.split("data-ranking-panel='jd' hidden", 1)
        self.assertIn("Jordan shipped the prior system.", pond_panel)
        self.assertNotIn("The JD rubric found direct systems evidence.", pond_panel)
        self.assertIn("The JD rubric found direct systems evidence.", jd_panel)
        self.assertLess(jd_panel.index("JD distributed systems"),
                        jd_panel.index("JD collaboration"))

    def test_evaluation_file_alone_does_not_enable_jd_ranking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._fixture(directory)
            root.joinpath("jordan-role", "evaluation-traits.json").write_text(json.dumps([
                {"meaning": "core", "temporal": "all", "value": "Pond collaboration"},
            ]), encoding="utf-8")
            search = load_searches(root)[0]
        self.assertFalse(search.has_jd_ranking)
        self.assertNotIn("JD Ranking", render_search_body(search))

    def test_jd_ranking_sorts_each_group_by_overall_score(self):
        with tempfile.TemporaryDirectory() as directory:
            search = load_searches(self._fixture(directory, with_jd=True))[0]
        original = search.groups[0].candidates[0]
        group = replace(search.groups[0], candidates=(
            replace(original, name="Lower Score", jd_score=0.25),
            replace(original, name="Higher Score", jd_score=0.95),
        ))
        detail = render_search_body(replace(search, groups=(group, *search.groups[1:])))
        pond_panel, jd_panel = detail.split("data-ranking-panel='jd' hidden", 1)
        self.assertLess(pond_panel.index("Lower Score"), pond_panel.index("Higher Score"))
        self.assertLess(jd_panel.index("Higher Score"), jd_panel.index("Lower Score"))

    def test_rejects_legacy_search_harness_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._fixture(directory, schema_version="search-harness.v0")
            with self.assertRaisesRegex(ValueError, "expected schema_version search-harness.v1"):
                load_searches(root)

    def test_explicit_scope_arguments_and_run_dir_query(self):
        run_args = build_parser().parse_args(["--run-dir", "/tmp/jordan-role"])
        root_args = build_parser().parse_args(["--root", "/tmp/deep-search"])
        self.assertEqual(run_args.run_dir, "/tmp/jordan-role")
        self.assertEqual(root_args.root, "/tmp/deep-search")

        with tempfile.TemporaryDirectory() as directory:
            search = load_searches(self._fixture(directory))[0]
            searches = (search, replace(search, run_id="other-role", title="Other Role"))
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(searches))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                with urllib.request.urlopen(base + "/", timeout=5) as response:
                    index = response.read().decode("utf-8")
                query = urllib.parse.urlencode({"run_dir": "/tmp/jordan-role"})
                with urllib.request.urlopen(base + "/?" + query, timeout=5) as response:
                    scoped = response.read().decode("utf-8")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
        self.assertIn("Other Role", index)
        self.assertNotIn("Other Role", scoped)
        self.assertIn("Senior Backend Engineer", scoped)
        self.assertEqual(scoped.count("class='search-card'"), 1)
        self.assertIn("class='search-details' open", scoped)
        self.assertIn("<b>1</b> search", scoped)

    def test_feedback_request_contains_identifiers_not_saved_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            search = load_searches(self._fixture(directory))[0]
        candidate = search.groups[0].candidates[0]
        body = build_feedback_request(
            search, "Score should be lower", candidate, environ={}).body()
        self.assertEqual(body["metadata"], {
            "source": "powerpacks-deep-search-results",
            "action": "candidate",
            "run_id": "jordan-role",
            "queries": ["Software Engineer in Oakland", "Distributed systems engineer"],
            "person_name": "Jordan Bravo",
            "linkedin_url": "https://linkedin.com/in/jordan-bravo",
        })
        serialized = json.dumps(body)
        self.assertNotIn("Jordan shipped", serialized)
        self.assertNotIn("distributed systems evidence", serialized)

    def test_server_renders_and_posts_resolved_candidate_feedback(self):
        sent = []

        def sender(request):
            sent.append(request)
            return {"status": "submitted", "feedback_id": "feedback-1"}

        with tempfile.TemporaryDirectory() as directory:
            searches = load_searches(self._fixture(directory))
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(searches, sender))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                with urllib.request.urlopen(base + "/", timeout=5) as response:
                    self.assertIn("Saved results", response.read().decode("utf-8"))
                with urllib.request.urlopen(
                        base + "/api/search?run_id=jordan-role", timeout=5) as response:
                    self.assertIn("Jordan Bravo", response.read().decode("utf-8"))
                body = urllib.parse.urlencode({
                    "run_id": "jordan-role",
                    "person_id": self.PERSON,
                    "comment": "Score should be lower",
                }).encode("utf-8")
                request = urllib.request.Request(
                    base + "/feedback", data=body, method="POST",
                    headers={"Origin": base,
                             "Content-Type": "application/x-www-form-urlencoded"})
                with urllib.request.urlopen(request, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
        self.assertEqual(payload["status"], "submitted")
        self.assertEqual(sent[0].metadata["person_name"], "Jordan Bravo")


if __name__ == "__main__":
    unittest.main()
