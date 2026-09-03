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

from packs.search.primitives.deep_search.results_web import RESULTS_JS
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
    UNGRADED = "1c7a9a4f-9a4f-4b7c-8d3e-2f3a4b5c6d7e"
    SECOND = "2d8b0b5a-0b5a-4c8d-9e4f-3a4b5c6d7e8f"

    def _pond_artifacts(self, base: Path, name: str, *, score: float,
                        title: str, company: str, query: str) -> dict[str, object]:
        artifact_dir = base / "artifacts" / name
        artifact_dir.mkdir(parents=True)
        results_path = artifact_dir / "results.jsonl"
        results_path.write_text(json.dumps({
            "person_id": self.PERSON,
            "name": "Jordan Bravo",
            "linkedin_url": "https://linkedin.com/in/jordan-bravo",
            "current_titles": title,
            "current_companies": company,
            "location": "Oakland, California",
            "final_score": str(score),
            "overall_reasoning": f"Jordan is a direct match for the {name} brief.",
            "source_channel": "gmail",
            "source_operator": "Alex Operator",
            "vertical_sources": ["role", "location"],
            "matched_position_indexes": [0],
            "trait_scores": json.dumps({
                "Works across teams": {
                    "score": 0.61,
                    "confidence": 0.73,
                    "reason": f"Jordan collaborated on the {name} system.",
                },
                "Builds reliable distributed systems": {
                    "score": score,
                    "confidence": 0.91,
                    "reason": f"Jordan shipped the {name} system.",
                },
            }),
        }) + "\n" + json.dumps({
            "person_id": self.UNGRADED,
            "name": "Casey Delta",
            "current_titles": "Platform Engineer",
            "current_companies": "Delta Works",
            "location": "Reno, Nevada",
            "final_score": "0.45",
            "overall_reasoning": "Casey has adjacent platform evidence only.",
            "vertical_sources": ["role"],
            "matched_position_indexes": [0],
            "trait_scores": json.dumps({
                "Builds reliable distributed systems": {
                    "score": 0.45, "confidence": 0.5,
                    "reason": "Casey maintains internal platform services.",
                },
            }),
        }) + "\n" + json.dumps({
            "person_id": self.SECOND,
            "name": "Morgan Echo",
            "current_titles": "Backend Engineer",
            "current_companies": "Echo Systems",
            "location": "Sacramento, California",
            "final_score": "0.52",
            "overall_reasoning": "Morgan has direct storage-engine evidence.",
            "vertical_sources": ["role"],
            "matched_position_indexes": [0],
            "trait_scores": json.dumps({
                "Builds reliable distributed systems": {
                    "score": 0.52, "confidence": 0.6,
                    "reason": "Morgan maintains a storage engine.",
                },
            }),
        }) + "\n", encoding="utf-8")
        profiles_path = artifact_dir / "profiles.jsonl.gz"
        with gzip.open(profiles_path, "wt", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "person_id": self.PERSON,
                "profile_picture_url": f"https://example.com/{name}.jpg",
                "summary": "Jordan Bravo leads reliability work on large distributed systems.",
                "location": "Oakland, California, United States",
                "positions": [
                    {"position_title": "Senior Software Engineer",
                     "company_name": "Bravo Systems",
                     "company_domain": "bravo.example.com",
                     "company_headcount": 120, "company_stage": "SERIES_B",
                     "company_funding_total": 45000000, "is_current": True,
                     "start_date": "2023-01-01T00:00:00Z", "end_date": None,
                     "dense_text": f"Leads the {name} reliability platform."},
                    {"position_title": "Software Engineer",
                     "company_name": "Example Labs", "is_current": False,
                     "start_date": "2020-02-01T00:00:00Z",
                     "end_date": "2022-12-01T00:00:00Z",
                     "description": "Built data pipelines."},
                ],
                "education": [
                    {"school_name": "Oakland State", "degree": "BS",
                     "field_of_study": "Computer Science",
                     "start_year": 2014, "end_year": 2018},
                ],
            }) + "\n")
            handle.write(json.dumps({
                "person_id": self.UNGRADED,
                "name": "Casey Delta",
                "summary": "Casey Delta runs internal platform tooling.",
            }) + "\n")
            handle.write(json.dumps({
                "person_id": self.SECOND,
                "name": "Morgan Echo",
                "summary": "Morgan Echo builds storage engines.",
            }) + "\n")
        return {
            "pond_n": 1,
            "query": query,
            "pool_stats": {"score_histogram": {
                "0.9+": 1, "0.8-0.9": 2, "0.7-0.8": 3,
                "0.6-0.7": 4, "below 0.6": 5,
            }},
            "arm": {"artifacts": {
                "jsonl": str(results_path),
                "profiles_path": str(profiles_path),
            }},
        }

    def _fixture(self, directory: str, *, jd_fit: bool = True) -> Path:
        """Two graded people (Jordan, Morgan) and one ungraded (Casey); jd_fit=False
        reproduces a run saved before rows carried JD trait statuses."""
        base = Path(directory)
        root = base / ".powerpacks" / "deep-search"
        current = root / "jordan-role"
        prior = root / "jordan-role-prior"
        current.mkdir(parents=True)
        prior.mkdir(parents=True)
        current.joinpath("jd.txt").write_text(
            "Acme needs a senior backend engineer.\nBuild reliable systems.", encoding="utf-8")
        current_iteration = self._pond_artifacts(
            base, "current", score=0.72, title="Software Engineer",
            company="Example Labs", query="Software Engineer in Oakland")
        prior_iteration = self._pond_artifacts(
            base, "prior", score=0.88, title="Senior Software Engineer",
            company="Bravo Systems", query="Distributed systems engineer")
        prior.joinpath("results.json").write_text(json.dumps({
            "iterations": [prior_iteration],
        }), encoding="utf-8")
        candidate = {
            "person": self.PERSON,
            "name": "Jordan Bravo",
            "linkedin_url": "https://linkedin.com/in/jordan-bravo",
            "rerank_score": 0.88,
            "fit_experts": {
                "role_fit": {
                    "label": "strong-fit",
                    "why": "Senior IC scope and systems work match the role.",
                },
                "company_taste": {
                    "label": "strong",
                    "why": "Bravo Systems hires strong reliability engineers.",
                },
                "craft_and_potential": {
                    "label": "strong",
                    "why": "Jordan repeatedly shipped high-quality reliability systems.",
                },
                "move_feasibility": {
                    "label": "plausible",
                    "why": "The role and compensation make a move plausible now.",
                },
            },
            "why": "Jordan has direct distributed systems evidence.",
            "found_by": [
                {"run": "jordan-role", "pond": 1,
                 "query": "Software Engineer in Oakland"},
                {"run": "jordan-role-prior", "pond": 1,
                 "query": "Distributed systems engineer"},
            ],
        }
        second = {
            "person": self.SECOND,
            "name": "Morgan Echo",
            "rerank_score": 0.52,
            "fit_experts": {
                "role_fit": {"label": "adjacent-fit",
                             "why": "Storage work is adjacent to the role."},
            },
            "why": "Morgan covers the JD traits but ranks low in the pond.",
            "found_by": [
                {"run": "jordan-role", "pond": 1,
                 "query": "Software Engineer in Oakland"},
            ],
        }
        summary = {
            "total_cost_usd": 0.42,
            "pond_chain": [
                {"run": "jordan-role", "pond_n": 1,
                 "query": "Software Engineer in Oakland",
                 "diagnosis": "wrong_specialty", "move": "add_adjacent_pond",
                 "result_count": 50, "cost_usd": 0.1},
                {"run": "jordan-role-prior", "pond_n": 1,
                 "query": "Distributed systems engineer",
                 "diagnosis": None, "move": "stop", "below_threshold": True,
                 "result_count": 20, "cost_usd": 0.2},
            ],
            "groups": {
                "send_worthy": [candidate], "chat_worthy": [second],
                "wrong_timing_relationship": [], "passed": [],
            },
        }
        if jd_fit:
            candidate["jd_fit"] = {"coverage": 0.6, "traits": [
                {"trait": "Builds reliable distributed systems", "status": "doing_now",
                 "evidence": "Led the reliability platform at Bravo Systems."},
                {"trait": "Postgres internals", "status": "thin",
                 "evidence": "No database internals work on record."},
            ]}
            second["jd_fit"] = {"coverage": 0.95, "traits": [
                {"trait": "Builds reliable distributed systems", "status": "doing_now",
                 "evidence": "Maintains a storage engine at Echo Systems."},
            ]}
            summary["jd_fit_order"] = [
                {"person": self.SECOND, "name": "Morgan Echo", "group": "chat_worthy",
                 "coverage": 0.95, "rerank_score": 0.52},
                {"person": self.PERSON, "name": "Jordan Bravo", "group": "send_worthy",
                 "coverage": 0.6, "rerank_score": 0.88},
            ]
        current.joinpath("results.json").write_text(json.dumps({
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "created_at": "2026-08-24T10:00:00Z",
            "iterations": [current_iteration],
            "summary": summary,
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
        self.assertEqual([pond.reviewed_count for pond in search.ponds], [2, 1])
        self.assertEqual([pond.below_threshold for pond in search.ponds], [False, True])
        candidate = search.groups[0].candidates[0]
        self.assertEqual(candidate.title, "Senior Software Engineer")
        self.assertEqual(candidate.company, "Bravo Systems")
        self.assertEqual(candidate.location, "Oakland, California")
        self.assertEqual(candidate.avatar_url, "https://example.com/prior.jpg")
        self.assertEqual(candidate.found_run, "jordan-role-prior")
        self.assertEqual(candidate.in_pond("jordan-role-prior", 1).traits[0].name,
                         "Works across teams")
        self.assertEqual(candidate.in_pond("jordan-role", 1).final_score, 0.72)
        self.assertEqual(candidate.in_pond("jordan-role-prior", 1).final_score, 0.88)

    def test_page_has_one_candidate_and_trait_reasoning_table(self):
        with tempfile.TemporaryDirectory() as directory:
            search = load_searches(self._fixture(directory))[0]
            page = render_page((search,))
            detail = render_search_body(search)
        self.assertIn("data-search-body='jordan-role'", page)
        for expected in (
            "Jordan Bravo", "Senior Software Engineer", "Bravo Systems",
            "Oakland, California", "88%", "Builds reliable distributed systems",
            "Jordan shipped the prior system.", "Main search",
            "results-table", "trait-indicator", "1</strong> annotated", "50 retrieved",
            "https://linkedin.com/in/jordan-bravo", "linkedin-icon", "data-feedback-person",
        ):
            self.assertIn(expected, detail)
        self.assertNotIn("score-histogram", detail)
        self.assertNotIn("candidate-card", detail)
        self.assertNotIn("trait-strip", detail)
        self.assertEqual(detail.count("class='results-table'"), 3)   # two ponds + beta
        self.assertIn("data-pond-tab='jordan-role:1'", detail)
        self.assertIn("data-pond-tab='jordan-role-prior:1'", detail)
        self.assertIn("role='tab' aria-selected='true'", detail)
        self.assertIn("data-pond-panel='jordan-role-prior:1' hidden", detail)
        self.assertNotIn("JD Ranking", detail)
        self.assertNotIn("group-band", detail)
        self.assertNotIn("group-toggle", detail)
        self.assertNotIn("result-group", detail)
        self.assertNotIn(">Passed<", detail)
        self.assertNotIn("confidence", detail)
        self.assertNotIn("overall", detail)
        person_cell = detail.split("<td class='candidate-person-cell'>", 1)[1].split("</td>", 1)[0]
        indicator_cell = detail.split("<td class='candidate-indicators'>", 1)[1].split("</td>", 1)[0]
        self.assertNotIn("data-feedback-person", person_cell)
        self.assertIn("data-feedback-person", indicator_cell)
        self.assertNotIn("candidate-fit-reason", detail)
        self.assertIn("Search chain", detail)
        self.assertIn("<summary>Job description</summary>", page)
        self.assertIn("M20.5 2h-17A1.5", detail)
        self.assertIn("Acme needs a senior backend engineer.", page)
        self.assertNotIn("<b>1</b><small>results</small>", page)

    def test_candidate_row_has_one_reasoned_badge_per_fit_expert(self):
        with tempfile.TemporaryDirectory() as directory:
            search = load_searches(self._fixture(directory))[0]
            detail = render_search_body(search)

        indicator_cell = detail.split("<td class='candidate-indicators'>", 1)[1].split("</td>", 1)[0]
        self.assertEqual(indicator_cell.count("class='badge'"), 4)
        self.assertIn(">Role fit · Strong fit<", indicator_cell)
        self.assertIn("Senior IC scope and systems work match the role.", indicator_cell)
        self.assertIn(">Company taste · Strong company signal<", indicator_cell)
        self.assertIn("Bravo Systems hires strong reliability engineers.", indicator_cell)
        self.assertIn(">Craft/potential · Strong craft<", indicator_cell)
        self.assertIn("Jordan repeatedly shipped high-quality reliability systems.", indicator_cell)
        self.assertIn(">Move feasibility · Plausible now<", indicator_cell)
        self.assertIn("The role and compensation make a move plausible now.", indicator_cell)
        badges = indicator_cell.split("<div class='candidate-badges'>", 1)[1].split("</div>", 1)[0]
        self.assertNotIn(">Matched<", badges)
        self.assertNotIn("candidate-badges", detail.split(
            "<td class='candidate-person-cell'>", 1)[1].split("</td>", 1)[0])
        self.assertLess(indicator_cell.index("trait-indicators"),
                        indicator_cell.index("candidate-badges"))

    def test_beta_rows_list_jd_traits_as_a_second_score_list_and_main_rows_do_not(self):
        with tempfile.TemporaryDirectory() as directory:
            search = load_searches(self._fixture(directory))[0]
            detail = render_search_body(search)

        main, beta = detail.split("<div data-view-panel='jd-fit'", 1)
        self.assertNotIn("jd-fit-list", main)
        self.assertNotIn("jd-fit-chip", main)
        indicator_cell = beta.split("Jordan Bravo", 1)[1].split(
            "<td class='candidate-indicators'>", 1)[1].split("</td>", 1)[0]
        self.assertIn("<div class='jd-fit-list'>", indicator_cell)
        self.assertIn(">JD traits (beta)<", indicator_cell)
        self.assertIn(">JD fit 60%<", indicator_cell)
        jd_list = indicator_cell.split("<div class='jd-fit-list'>", 1)[1]
        # Same shape as the trait list: ladder value as the score badge, then trait: status.
        self.assertIn("<b class='trait-score-badge trait-score-high'>95%</b>", jd_list)
        # Status and the profile evidence read inline, like the pond trait rows above them.
        self.assertIn("<strong>Builds reliable distributed systems:</strong> <em>Doing it now</em> "
                      "Led the reliability platform at Bravo Systems.", jd_list)
        self.assertIn("<b class='trait-score-badge trait-score-low'>25%</b>", jd_list)
        self.assertIn("<strong>Postgres internals:</strong> <em>Thin</em> "
                      "No database internals work on record.", jd_list)
        self.assertNotIn("role='tooltip'", jd_list.split("<div class='candidate-badges'>", 1)[0])
        self.assertEqual(jd_list.count("class='trait-indicator jd-trait'"), 2)
        self.assertEqual(indicator_cell.count("class='badge'"), 4)       # fit badges untouched
        self.assertLess(indicator_cell.index("<div class='trait-indicators'>"),
                        indicator_cell.index("jd-fit-list"))
        self.assertLess(indicator_cell.index("jd-fit-list"),
                        indicator_cell.index("<div class='candidate-badges'>"))

    def test_older_runs_without_jd_fit_render_without_the_beta_list(self):
        with tempfile.TemporaryDirectory() as directory:
            search = load_searches(self._fixture(directory, jd_fit=False))[0]
            detail = render_search_body(search)

        self.assertIsNone(search.groups[0].candidates[0].jd_fit)
        self.assertEqual(search.jd_fit_order, ())
        self.assertNotIn("jd-fit-list", detail)
        self.assertNotIn("jd-fit-chip", detail)
        indicator_cell = detail.split("<td class='candidate-indicators'>", 1)[1].split("</td>", 1)[0]
        self.assertEqual(indicator_cell.count("class='badge'"), 4)
        self.assertIn("data-view-tab='jd-fit'", detail)
        beta = detail.split("data-view-panel='jd-fit'", 1)[1]
        self.assertIn("No JD fit annotations in this run.", beta)
        self.assertNotIn("candidate-row", beta)

    def test_beta_panel_orders_graded_candidates_by_jd_fit_order(self):
        with tempfile.TemporaryDirectory() as directory:
            search = load_searches(self._fixture(directory))[0]
            detail = render_search_body(search)

        self.assertEqual(search.jd_fit_order, (self.SECOND, self.PERSON))
        self.assertIn("role='tab' aria-selected='true' data-view-tab='main'>"
                      "Main search</button>", detail)
        self.assertIn("role='tab' aria-selected='false' data-view-tab='jd-fit'>"
                      "JD fit (beta)</button>", detail)
        main, beta = detail.split("<div data-view-panel='jd-fit'", 1)
        self.assertIn("<div data-view-panel='main'", main)
        self.assertTrue(beta.startswith(" role='tabpanel' hidden>"))
        # Main keeps rerank order (0.72 > 0.52); beta follows coverage (0.95 > 0.6).
        self.assertLess(main.index("Jordan Bravo"), main.index("Morgan Echo"))
        self.assertLess(beta.index("Morgan Echo"), beta.index("Jordan Bravo"))
        self.assertNotIn("Casey Delta", beta)
        self.assertEqual(beta.count("class='candidate-person-cell'"), 2)
        self.assertIn(">JD fit 95%<", beta)
        self.assertIn(">JD fit 60%<", beta)
        self.assertNotIn("data-results-toolbar", beta)
        self.assertIn("[data-view-tab]", RESULTS_JS.read_text(encoding="utf-8"))

    def test_tags_persist_per_search_and_export_tagged_results(self):
        with tempfile.TemporaryDirectory() as directory:
            search = load_searches(self._fixture(directory))[0]
            detail = render_search_body(search)

        self.assertIn("data-tag-person='" + self.PERSON + "'", detail)
        self.assertIn("aria-label='Add tag to Jordan Bravo'", detail)
        self.assertIn("data-results-toolbar", detail)
        self.assertIn("data-result-filter='tagged'", detail)
        self.assertIn("data-tag-filters", detail)
        self.assertIn("data-copy-results", detail)
        self.assertIn("data-export-csv", detail)
        self.assertIn("data-person-name='Jordan Bravo'", detail)
        self.assertIn("data-person-linkedin='https://linkedin.com/in/jordan-bravo'", detail)
        self.assertIn("data-person-source='gmail'", detail)
        self.assertIn("data-person-network='Alex Operator'", detail)
        script = RESULTS_JS.read_text(encoding="utf-8")
        self.assertIn("powerset_tagged_", script)
        self.assertIn("powerset_pinned_", script)
        self.assertIn('const LEGACY_PIN_TAG = "Pinned"', script)
        self.assertIn('const TAG_NAME_MAX = 40', script)
        self.assertIn('"Name", "Title", "Company", "Location", "Sources", "Network",', script)
        self.assertNotIn("data-pin-person", detail)
        self.assertNotIn("data-result-filter='pinned'", detail)

    def test_rows_sort_by_score_and_unannotated_rows_are_label_free(self):
        with tempfile.TemporaryDirectory() as directory:
            search = load_searches(self._fixture(directory))[0]
            detail = render_search_body(search)

        # Ungraded Casey (0.45) renders after graded Jordan (0.72), label-free.
        self.assertLess(detail.index("Jordan Bravo"), detail.index("Casey Delta"))
        casey_cell = detail.split("Casey Delta", 1)[1].split(
            "<td class='candidate-indicators'>", 1)[1].split("</td>", 1)[0]
        self.assertNotIn("candidate-badges", casey_cell)
        self.assertNotIn("details-feedback", casey_cell)      # feedback needs a grade
        self.assertIn("person-details", casey_cell)           # details still open
        self.assertIn("Casey has adjacent platform evidence only.", casey_cell)

        # An ungraded row outscoring every graded row renders first.
        source = search.ponds[0].candidates[0]
        top = replace(source, person_id="person-top", name="Robin Topscore",
                      final_score=0.99)
        ponds = (replace(search.ponds[0], candidates=(top, *search.ponds[0].candidates)),)
        reordered = render_search_body(replace(search, ponds=ponds))
        self.assertLess(reordered.index("Robin Topscore"), reordered.index("Jordan Bravo"))

    def test_viewer_renders_every_reranked_row_with_lazy_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            search = load_searches(self._fixture(directory))[0]
            source = search.ponds[0].candidates[0]
            rows = tuple(replace(
                source, person_id=f"person-{index}", name=f"Person {index:03d}")
                for index in range(120))
            ponds = (replace(search.ponds[0], candidates=rows),)
            detail = render_search_body(replace(search, ponds=ponds))

        main = detail.split("data-view-panel='jd-fit'", 1)[0]
        self.assertIn("Person 119", main)
        self.assertEqual(main.count("class='candidate-person-cell'"), 120)
        self.assertEqual(main.count("hidden data-lazy"), 20)   # rows past the first 100
        self.assertEqual(main.count("lazy-sentinel"), 1)

    def test_viewer_marks_the_adaptive_below_threshold_set(self):
        with tempfile.TemporaryDirectory() as directory:
            search = load_searches(self._fixture(directory))[0]
            pond = replace(search.ponds[0], reviewed_count=12, below_threshold=True)
            detail = render_search_body(replace(search, ponds=(pond,)))

        self.assertIn("<strong>12</strong> annotated <span>·</span> 50 retrieved", detail)
        self.assertNotIn("scored ≥", detail)

        empty = render_search_body(replace(search, ponds=(replace(
            search.ponds[0], reviewed_count=0, candidates=()),)))
        self.assertIn("<strong>0</strong> annotated <span>·</span> 50 retrieved", empty)
        self.assertIn("nothing cleared the review threshold", empty)

    def test_explicit_scope_arguments_and_run_dir_query(self):
        run_args = build_parser().parse_args(["--run-dir", "/tmp/jordan-role"])
        root_args = build_parser().parse_args(["--root", "/tmp/deep-search"])
        self.assertEqual(run_args.run_dir, "/tmp/jordan-role")
        self.assertEqual(root_args.root, "/tmp/deep-search")

        with tempfile.TemporaryDirectory() as directory:
            search = load_searches(self._fixture(directory))[0]
            searches = (search, replace(search, run_id="other-role", title="Other Role"))
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(lambda: searches))
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
        self.assertNotIn("search-chevron", scoped)
        self.assertIn("data-search-body='jordan-role'", scoped)
        self.assertIn("Search Results", scoped)
        self.assertNotIn("Saved results", scoped)
        self.assertNotIn("Deep search", scoped)

    def test_details_panel_renders_profile_and_matched_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            search = load_searches(self._fixture(directory))[0]
            detail = render_search_body(search)

        indicator_cell = detail.split("<td class='candidate-indicators'>", 1)[1].split("</td>", 1)[0]
        self.assertIn("details-trigger", indicator_cell)
        self.assertIn("<div class='person-details' hidden>", indicator_cell)
        panel = indicator_cell.split("<div class='person-details' hidden>", 1)[1]
        actions = indicator_cell.split("<span class='person-actions'>", 1)[1].split("</span>", 1)[0]
        self.assertNotIn("data-feedback-run", actions)      # corner holds only the ... trigger
        self.assertIn("flag-icon", panel)                   # feedback lives inside the panel
        self.assertIn("Feedback</button>", panel)
        self.assertIn("data-feedback-person", panel)
        self.assertIn("Why they match", indicator_cell)
        self.assertIn("Jordan is a direct match for the current brief.", detail)
        self.assertIn(">Role</b>", indicator_cell)          # sources chips
        self.assertIn("Oakland, California, United States", indicator_cell)
        self.assertIn("Jordan Bravo leads reliability work", indicator_cell)
        self.assertIn("matched: [0]", indicator_cell)
        self.assertIn("Senior Software Engineer<b class='matched-chip'>Matched</b>", indicator_cell)
        self.assertIn("#0<b class='current-chip'>Current</b>", indicator_cell)
        self.assertIn("href='https://bravo.example.com'", indicator_cell)
        self.assertIn("120 people · SERIES_B · $45M raised", indicator_cell)
        self.assertIn("Jan 2023 – Present", indicator_cell)
        self.assertIn("Feb 2020 – Dec 2022", indicator_cell)
        self.assertIn("Oakland State", indicator_cell)
        self.assertIn("BS in Computer Science", indicator_cell)
        self.assertIn("2014 – 2018", indicator_cell)

    def test_feedback_request_carries_the_full_local_search_context(self):
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
            "jd": "Acme needs a senior backend engineer.\nBuild reliable systems.",
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "person_id": self.PERSON,
            "person_name": "Jordan Bravo",
            "linkedin_url": "https://linkedin.com/in/jordan-bravo",
            "group": "send_worthy",
            "group_label": "Matched",
            "why": "Jordan has direct distributed systems evidence.",
            "found_query": "Distributed systems engineer",
            "found_run": "jordan-role-prior",
            "found_pond": 1,
            "fit_experts": {
                "role_fit": {
                    "label": "strong-fit",
                    "why": "Senior IC scope and systems work match the role.",
                },
                "company_taste": {
                    "label": "strong",
                    "why": "Bravo Systems hires strong reliability engineers.",
                },
                "craft_and_potential": {
                    "label": "strong",
                    "why": "Jordan repeatedly shipped high-quality reliability systems.",
                },
                "move_feasibility": {
                    "label": "plausible",
                    "why": "The role and compensation make a move plausible now.",
                },
            },
            "person_title": "Senior Software Engineer",
            "person_company": "Bravo Systems",
            "person_location": "Oakland, California",
            "reasoning": "Jordan is a direct match for the prior brief.",
            "final_score": 0.88,
            "traits": [
                {"name": "Works across teams", "score": 0.61, "confidence": 0.73,
                 "reason": "Jordan collaborated on the prior system."},
                {"name": "Builds reliable distributed systems", "score": 0.88,
                 "confidence": 0.91, "reason": "Jordan shipped the prior system."},
            ],
        })

        search_body = build_feedback_request(search, "Bad pond", environ={}).body()
        self.assertEqual(search_body["metadata"], {
            "source": "powerpacks-deep-search-results",
            "action": "search",
            "run_id": "jordan-role",
            "queries": ["Software Engineer in Oakland", "Distributed systems engineer"],
            "jd": "Acme needs a senior backend engineer.\nBuild reliable systems.",
            "title": "Senior Backend Engineer",
            "company": "Acme",
        })

    def test_server_renders_and_posts_resolved_candidate_feedback(self):
        sent = []

        def sender(request):
            sent.append(request)
            return {"status": "submitted", "feedback_id": "feedback-1"}

        with tempfile.TemporaryDirectory() as directory:
            searches = load_searches(self._fixture(directory))
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(lambda: searches, sender))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                with urllib.request.urlopen(base + "/", timeout=5) as response:
                    self.assertIn("Search Results", response.read().decode("utf-8"))
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
