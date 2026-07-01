"""Unit tests for the deterministic query router + the routing eval harness.

Locks the Stage-2 routing baseline as a regression floor: Stage 3 (wiring the router into
$search) must not drop strict accuracy below what Stage 2 recorded.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RQ_DIR = ROOT / "packs" / "search" / "primitives" / "route_query"
EVAL_DIR = ROOT / "packs" / "search" / "evals"
for p in (str(RQ_DIR), str(EVAL_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclass field-type resolution needs the module registered
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


rq = _load(RQ_DIR / "route_query.py", "route_query")
rr = _load(EVAL_DIR / "run_routing_eval.py", "run_routing_eval")
CASES = json.loads((EVAL_DIR / "routing" / "cases.json").read_text())

# The Stage-2 recorded baseline — Stage 3 must not regress below these.
BASELINE_STRICT = 0.9375
BASELINE_LENIENT = 0.9792


class TestClassify(unittest.TestCase):
    def test_job_url_routes_recruit(self):
        self.assertEqual(rq.classify("https://job-boards.greenhouse.io/anthropic/jobs/123").route, "recruit")
        self.assertEqual(rq.classify("https://jobs.lever.co/acme/abc").route, "recruit")

    def test_pasted_jd_routes_recruit(self):
        jd = "Senior Engineer\n\nResponsibilities\n- build APIs\n\nQualifications\n- 5+ years Python"
        self.assertEqual(rq.classify(jd).route, "recruit")

    def test_shortlist_intent_routes_recruit(self):
        self.assertEqual(rq.classify("build me a shortlist of candidates for a founding engineer role").route, "recruit")

    def test_similar_person_routes_recruit(self):
        self.assertEqual(rq.classify("find me more people like https://www.linkedin.com/in/janedoe").route, "recruit")

    def test_contacts_noun_beats_company_and_network(self):
        self.assertEqual(rq.classify("search my contacts at OpenAI").route, "contacts")
        self.assertEqual(rq.classify("my contacts with a phone number").route, "contacts")

    def test_relational_predicate_routes_sql(self):
        self.assertEqual(rq.classify("who overlapped with Sam at Stripe").route, "sql")
        self.assertEqual(rq.classify("people who overlapped at Stripe and are now founders").route, "sql")
        self.assertEqual(rq.classify("people with 2+ startup stints").route, "sql")

    def test_company_subject_routes_company(self):
        self.assertEqual(rq.classify("get me the company id for Stripe").route, "company")
        self.assertEqual(rq.classify("which companies are backed by Sequoia").route, "company")

    def test_people_noun_flips_company_query_to_network(self):
        # company signals present ("backed by") but people noun makes it a people search
        self.assertEqual(rq.classify("who are the engineers at companies backed by Sequoia").route, "network")
        self.assertEqual(rq.classify("look up the engineers at Anthropic").route, "network")

    def test_default_is_network(self):
        self.assertEqual(rq.classify("staff backend engineers in San Francisco").route, "network")
        self.assertEqual(rq.classify("who is Jane Doe").route, "network")

    def test_network_subroute_local_vs_turbopuffer(self):
        self.assertEqual(rq.classify("local: senior data engineers in my imported network").subroute, "local")
        self.assertEqual(rq.classify("search my Powerset set for ML researchers").subroute, "turbopuffer")

    def test_explicit_prefix_wins(self):
        self.assertEqual(rq.classify("$search-sql find engineers who became PMs").route, "sql")
        self.assertEqual(rq.classify("$search-company look up Ramp").route, "company")
        # a network prefix carrying a JD still means the deep lane
        self.assertEqual(rq.classify("$search-network https://jobs.lever.co/x/y").route, "recruit")

    def test_documented_seam_shortlist_companies(self):
        # KNOWN baseline miss: 'shortlist' recruit verb + company subject -> mispredicts recruit.
        # If a future change fixes this, update the baseline + this assertion together.
        self.assertEqual(rq.classify("shortlist the fintech companies that raised a Series B").route, "recruit")


class TestRoutingEvalHarness(unittest.TestCase):
    def test_run_computes_accuracy_and_confusion(self):
        tiny = [
            {"query": "staff engineers in SF", "expected": "network"},
            {"query": "https://jobs.lever.co/x/y", "expected": "recruit"},
            {"query": "my contacts at OpenAI", "expected": "contacts"},
        ]
        r = rr.run(tiny)
        self.assertEqual(r["n_cases"], 3)
        self.assertEqual(r["strict_accuracy"], 1.0)
        self.assertEqual(r["confusion"]["network"]["network"], 1)

    def test_acceptable_alternates_count_as_lenient_correct(self):
        cases = [{"query": "who are the investors in Ramp", "expected": "company", "acceptable": ["network"]}]
        r = rr.run(cases)
        # classifier predicts network here; strict miss but lenient hit
        self.assertEqual(r["lenient_correct"], 1)

    def test_baseline_floor_on_real_fixture(self):
        r = rr.run(CASES)
        self.assertGreaterEqual(r["strict_accuracy"], BASELINE_STRICT)
        self.assertGreaterEqual(r["lenient_accuracy"], BASELINE_LENIENT)
        # anti-overfit: every route has >= 2 labeled cases with varied phrasings
        for route in rq.ROUTES:
            n = sum(1 for c in CASES if c["expected"] == route)
            self.assertGreaterEqual(n, 2, f"route {route} has too few cases")


if __name__ == "__main__":
    unittest.main()
