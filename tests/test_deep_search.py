"""Unit tests for the $search deep-mode primitives: JD intake, query generation, and optional traits."""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIM = ROOT / "packs" / "search" / "primitives" / "deep_search"
if str(PRIM) not in sys.path:
    sys.path.insert(0, str(PRIM))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, PRIM / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


su = _load("subprocess_utils")
dj = _load("decompose_jd")
bei = _load("extract_jd_traits")
rl = _load("deep_search_loop")
fj = _load("fetch_jd")

_JD = (
    "Design and run experiments on production systems.\n"
    "Own services end-to-end from design to rollout.\n"
    "Prior experience shipping software at a startup.\n"
    "Published research is a plus.\n"
)
_TRAITS = [
    {"trait": "designs and runs experiments on production systems", "kind": "capability",
     "evidence_quote": "Design and run experiments on production systems."},
    {"trait": "owns services end-to-end from design to rollout", "kind": "capability",
     "evidence_quote": "Own services end-to-end from design to rollout."},
    {"trait": "shipped software at a startup", "kind": "background",
     "evidence_quote": "Prior experience shipping software at a startup."},
]
_TRAITS_OBJ = {"traits": _TRAITS}


def _response(content: str) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class TestSubprocessUtils(unittest.TestCase):
    def test_run_checked_raises_on_nonzero(self):
        with self.assertRaises(su.CommandError) as ctx:
            su.run_checked([sys.executable, "-c", "import sys; print('bad', file=sys.stderr); sys.exit(7)"], description="boom")
        self.assertEqual(ctx.exception.returncode, 7)
        self.assertIn("bad", ctx.exception.stderr_tail)

    def test_run_checked_raises_on_missing_expected_path(self):
        missing = Path(tempfile.mkdtemp()) / "missing.txt"
        with self.assertRaises(su.CommandError) as ctx:
            su.run_checked([sys.executable, "-c", "pass"], expected_paths=[missing], description="artifact")
        self.assertEqual(ctx.exception.missing, [missing])


class TestLocalBackendThreading(unittest.TestCase):
    """--backend/--db threading through the deep-search sourcing chain (post search-backend fold)."""

    class _HaltAfterParse(Exception):
        pass

    def test_deep_loop_binds_backend_to_decision_json(self):
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d)
            decision = run_dir / "decision.json"
            decision.write_text(json.dumps({"surface": "people", "backend": "local", "depth": "deep"}))
            backend, used = rl.resolve_backend(run_dir, None, None)
            self.assertEqual((backend, used), ("local", decision))
            with self.assertRaises(ValueError):
                rl.resolve_backend(run_dir, "powerset", None)

    def _parse_with_real_parser(self, mod, argv: list[str]) -> argparse.Namespace:
        """Drive mod.main() only through its real argparse parse, then halt (no execution)."""
        captured: dict[str, argparse.Namespace] = {}
        real_parse_args = argparse.ArgumentParser.parse_args

        def spy(parser, *args, **kwargs):
            captured["args"] = real_parse_args(parser, *args, **kwargs)
            raise TestLocalBackendThreading._HaltAfterParse()

        old_argv = sys.argv
        sys.argv = argv
        try:
            with mock.patch.object(argparse.ArgumentParser, "parse_args", spy):
                with self.assertRaises(TestLocalBackendThreading._HaltAfterParse):
                    mod.main()
        finally:
            sys.argv = old_argv
        return captured["args"]

    def test_deep_search_loop_parser_accepts_local_backend(self):
        args = self._parse_with_real_parser(
            rl,
            ["loop", "--jd-file", "jd.txt", "--run-dir", "run",
             "--backend", "local", "--db", "x.duckdb"],
        )
        self.assertEqual(args.backend, "local")
        self.assertEqual(args.db, "x.duckdb")

    def test_deep_search_loop_parser_accepts_reviewed_queries_file(self):
        args = self._parse_with_real_parser(
            rl,
            ["loop", "--jd-file", "jd.txt", "--run-dir", "run",
             "--queries-file", "edited-queries.json"],
        )
        self.assertEqual(args.queries_file, "edited-queries.json")


class TestDecomposeJd(unittest.TestCase):

    def test_parse_seeds_strings_and_objects(self):
        self.assertEqual(dj.parse_seeds({"seeds": ["a", "b"]}),
                         [{"key": "q00", "query": "a"}, {"key": "q01", "query": "b"}])
        self.assertEqual(dj.parse_seeds({"seeds": [{"query": "x"}, {"seed": "y"}]}),
                         [{"key": "q00", "query": "x"}, {"key": "q01", "query": "y"}])

    def test_parse_seeds_skips_empty(self):
        out = dj.parse_seeds({"seeds": ["a", "", "b", "c"]})
        self.assertEqual([s["query"] for s in out], ["a", "b", "c"])

    def test_parse_seeds_raises_on_empty(self):
        with self.assertRaises(ValueError):
            dj.parse_seeds({"seeds": []})

    def test_build_messages_includes_jd(self):
        msgs = dj.build_messages("Build RAG systems")
        self.assertIn("Build RAG systems", msgs[-1]["content"])

    def test_build_messages_accepts_reviewed_system_prompt(self):
        msgs = dj.build_messages("Build RAG systems", system_prompt="custom prompt")
        self.assertEqual(msgs[0]["content"], "custom prompt")


_sb_spec = importlib.util.spec_from_file_location(
    "seniority_bands", ROOT / "packs" / "search" / "primitives" / "shared" / "seniority_bands.py"
)
sb = importlib.util.module_from_spec(_sb_spec)
_sb_spec.loader.exec_module(sb)  # type: ignore[union-attr]


class TestPreserveSemanticQuery(unittest.TestCase):
    def test_preserves_raw_query_and_keeps_bm25_and_filters(self):
        payload = {"role_search_filters": {
            "semantic_query": "Engineers specializing in distributed systems design and implementation",
            "bm25_queries": ["distributed systems engineer", "scheduler engineer"],
            "seniority_bands": ["staff"], "cities": ["San Francisco"],
        }}
        raw = "Distributed systems engineer who built admission control and bin packing for a GPU cluster"
        out = sb.pin_payload_semantic_query(payload, raw)
        f = out["role_search_filters"]
        self.assertEqual(f["semantic_query"], raw)          # raw query becomes the vector
        self.assertTrue(f["semantic_query_preserved"])
        self.assertEqual(f["bm25_queries"], ["distributed systems engineer", "scheduler engineer"])  # bm25 kept
        self.assertEqual(f["seniority_bands"], ["staff"])   # filters kept
        self.assertEqual(f["cities"], ["San Francisco"])
        self.assertTrue(any("semantic_query preserved" in n for n in out["notes"]))

    def test_does_not_mutate_input(self):
        payload = {"role_search_filters": {"semantic_query": "orig", "bm25_queries": ["x"]}}
        sb.pin_payload_semantic_query(payload, "new")
        self.assertEqual(payload["role_search_filters"]["semantic_query"], "orig")


class TestExtractJdTraits(unittest.TestCase):


    def test_trait_quotes_restore_original_jd_whitespace_without_changing_words(self):
        jd = "Build client relationships.\u00a0 Plan onboarding.\nDeliver projects."
        quote = "client relationships.  Plan onboarding. Deliver projects."
        trait = {"trait": "Client implementation", "kind": "capability", "evidence_quote": quote}
        parsed = bei._traits({"traits": [trait]}, jd)
        self.assertEqual(parsed[0]["evidence_quote"], jd[len("Build "):])
        self.assertEqual(trait["evidence_quote"], quote)
        self.assertEqual(bei._traits({"traits": [{**trait, "evidence_quote": "invented words"}]}, jd), [])


    def test_extract_traits_uses_pond_context_and_checkpoints_the_response(self):
        client = mock.Mock()
        client.chat.completions.create.return_value = _response(json.dumps(_TRAITS_OBJ))
        pond_traits = [{
            "value": "Design Engineer", "temporal": "current", "meaning": "role",
        }]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jd = root / "jd.txt"
            raw = root / "traits.raw.json"
            jd.write_text(_JD, encoding="utf-8")

            traits = bei.extract_traits(
                jd_file=jd,
                brief={
                    "job_title": "Design Engineer",
                    "normalized_archetype": "design engineer",
                    "target_level": "staff_ic",
                    "pond_prompt_family": "design",
                },
                pond_traits=pond_traits,
                model="gpt-5.6-sol",
                api_key="test",
                reasoning_effort="high",
                client=client,
                raw_response_path=raw,
            )

            self.assertEqual(traits, _TRAITS)
            self.assertEqual(json.loads(raw.read_text()), _TRAITS_OBJ)

        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["model"], "gpt-5.6-sol")
        self.assertEqual(request["reasoning_effort"], "high")
        self.assertEqual(
            request["messages"][0]["content"],
            bei.load_pond_prompt({"pond_prompt_family": "design"}, "traits"),
        )
        self.assertIn(json.dumps(pond_traits, indent=2), request["messages"][1]["content"])

    def test_extract_traits_reuses_its_checkpoint(self):
        client = mock.Mock()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jd = root / "jd.txt"
            raw = root / "traits.raw.json"
            jd.write_text(_JD, encoding="utf-8")
            raw.write_text(json.dumps(_TRAITS_OBJ), encoding="utf-8")

            traits = bei.extract_traits(
                jd_file=jd,
                brief={
                    "job_title": "Design Engineer",
                    "normalized_archetype": "design engineer",
                    "target_level": "staff_ic",
                    "pond_prompt_family": "design",
                },
                pond_traits=[],
                model="gpt-5.6-sol",
                api_key="test",
                client=client,
                raw_response_path=raw,
            )

        self.assertEqual(traits, _TRAITS)
        client.chat.completions.create.assert_not_called()

    def test_traits_request_passes_the_complete_pond_traits_and_excludes_repeats(self):
        pond_traits = [{
            "value": "software engineering",
            "temporal": "all",
            "meaning": "core",
            "evidence": ["current role"],
        }]

        request = bei.traits_request(
            jd=_JD,
            brief={
                "job_title": "Software Engineer",
                "normalized_archetype": "software engineer",
                "target_level": "senior_ic",
            },
            model="gpt-5.6-sol",
            system_prompt="extract traits",
            pond_traits=pond_traits,
        )

        user = request["messages"][1]["content"]
        self.assertIn(json.dumps(pond_traits, indent=2), user)
        self.assertIn(
            "Return only additional qualifications beyond what these pond traits explicitly cover. "
            "Use the JD to identify and group the experience that would distinguish better-fit candidates.", user)
        self.assertNotIn("Do not restate, narrow, broaden, or split", user)


class TestDeepSearchLoop(unittest.TestCase):


    def test_url_source_binding_rejects_missing_or_different_metadata(self):
        directory = Path(tempfile.mkdtemp())
        source = directory / "source.json"
        with self.assertRaisesRegex(ValueError, "cannot verify"):
            rl.validate_bound_jd_source(source, "https://example.test/job")
        source.write_text(json.dumps({
            "requested_url": "https://example.test/job",
            "source_url": "https://redirect.test/final",
        }))
        self.assertEqual(
            rl.validate_bound_jd_source(source, "https://EXAMPLE.test/job#apply")["source_url"],
            "https://redirect.test/final",
        )
        with self.assertRaisesRegex(ValueError, "conflicts with the URL bound"):
            rl.validate_bound_jd_source(source, "https://example.test/other")


class TestFetchJd(unittest.TestCase):
    """URL->JD front-end that lets $search deep mode accept a job-posting URL."""

    def test_extract_drops_chrome_keeps_content_and_title(self):
        html = (
            "<html><head><title> Senior Backend Engineer - Acme </title><style>.x{}</style></head>"
            "<body><nav>Home About</nav><h1>Senior Backend Engineer</h1>"
            "<p>Build production APIs.</p><ul><li>5+ years Python</li><li>Postgres</li></ul>"
            "<script>var x=1;</script><footer>copyright 2026</footer></body></html>"
        )
        text, title = fj.extract(html)
        self.assertEqual(title, "Senior Backend Engineer - Acme")
        self.assertIn("Senior Backend Engineer", text)
        self.assertIn("5+ years Python", text)
        self.assertIn("Postgres", text)
        # script/style/nav/footer chrome is dropped
        for junk in ("var x=1", "copyright", "Home About"):
            self.assertNotIn(junk, text)

    def test_extract_separates_block_elements(self):
        text, _ = fj.extract("<p>one</p><p>two</p><li>three</li>")
        # block boundaries prevent words running together
        self.assertNotIn("onetwo", text)
        self.assertEqual([ln for ln in text.splitlines() if ln], ["one", "two", "three"])

    def test_extracts_hiring_company_and_website_from_json_ld(self):
        html = '''<script type="application/ld+json">{
          "@type":"JobPosting","hiringOrganization":{"name":"Firecrawl",
          "sameAs":"https://www.firecrawl.dev/"}}</script>'''
        metadata = fj.extract_company_metadata(html, "https://jobs.ashbyhq.com/firecrawl/id")
        self.assertEqual(metadata["company_name"], "Firecrawl")
        self.assertEqual(metadata["company_website_url"], "https://www.firecrawl.dev/")

    def test_company_owned_careers_url_beats_embedded_job_board_link(self):
        html = '<a href="https://jobs.ashbyhq.com/lovable/id">Apply</a>'
        metadata = fj.extract_company_metadata(
            html, "https://lovable.dev/careers/design-engineer")

        self.assertEqual(metadata["company_website_url"], "https://lovable.dev")
        self.assertNotIn("https://jobs.ashbyhq.com/lovable/id",
                         metadata["company_website_urls"])

    def test_extracts_only_linkedin_company_slug(self):
        html = ('<a href="https://www.linkedin.com/in/person">Person</a>'
                '<a href="https://linkedin.com/company/lovable-dev/about">Company</a>')
        self.assertEqual(
            fj.extract_linkedin_company_slug(html, "https://lovable.dev"), "lovable-dev")

    def test_main_writes_jd_and_source_json(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "jd.txt"
            html = "<html><head><title>Role X</title></head><body><p>" + ("do the work " * 100) + "</p></body></html>"
            argv = sys.argv
            sys.argv = ["fetch_jd", "--url", "https://example.test/job", "--out", str(out)]
            try:
                with mock.patch.object(fj, "fetch", return_value=(html, "https://example.test/job")):
                    fj.main()  # status ok -> no SystemExit
            finally:
                sys.argv = argv
            self.assertTrue(out.exists())
            self.assertIn("do the work", out.read_text())
            src = json.loads((Path(d) / "source.json").read_text())
            self.assertEqual(src["requested_url"], "https://example.test/job")
            self.assertEqual(src["source_url"], "https://example.test/job")
            self.assertEqual(src["source_title"], "Role X")
            self.assertIn("company_website_urls", src)
            self.assertIn("fetched_at", src)

    def test_main_thin_content_still_writes_and_warns(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "jd.txt"
            argv = sys.argv
            sys.argv = ["fetch_jd", "--url", "https://example.test/js", "--out", str(out)]
            try:
                with mock.patch.object(fj, "fetch", return_value=("<html><body>App</body></html>", "https://example.test/js")):
                    fj.main()  # thin is not a failure -> no SystemExit
            finally:
                sys.argv = argv
            self.assertTrue(out.exists())  # thin content is still written

    def test_main_uses_ashby_api_when_page_fetch_fails(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "jd.txt"
            url = "https://jobs.ashbyhq.com/acme/2e718684-4f75-4a99-8d6b-3b6bd44e4228"
            argv = sys.argv
            sys.argv = ["fetch_jd", "--url", url, "--out", str(out)]
            try:
                with mock.patch.object(fj, "fetch_ashby",
                                       return_value=(("Role X\n\n" + "work " * 100), "Role X", {})), \
                     mock.patch.object(fj, "fetch", side_effect=fj.urllib.error.URLError("blocked")):
                    fj.main()
            finally:
                sys.argv = argv

            self.assertIn("Role X", out.read_text())
            source = json.loads((Path(d) / "source.json").read_text())
            self.assertEqual(source["source_url"], url)
            self.assertEqual(source["via"], "ashby_posting_api")

    def test_deep_search_loop_requires_exactly_one_jd_input(self):
        with tempfile.TemporaryDirectory() as d:
            argv = sys.argv
            # neither jd-file nor jd-url
            sys.argv = ["loop", "--run-dir", str(Path(d) / "r")]
            try:
                with self.assertRaises(SystemExit) as ctx:
                    rl.main()
                self.assertEqual(ctx.exception.code, 2)
                # both jd-file and jd-url
                sys.argv = ["loop", "--jd-file", "x.txt", "--jd-url", "http://y", "--run-dir", str(Path(d) / "r2")]
                with self.assertRaises(SystemExit) as ctx2:
                    rl.main()
                self.assertEqual(ctx2.exception.code, 2)
            finally:
                sys.argv = argv

    def test_deep_search_loop_jd_url_fetches_before_loop(self):
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d) / "run"
            argv = sys.argv
            sys.argv = ["loop", "--jd-url", "https://example.test/job", "--run-dir", str(run_dir)]
            try:
                def fake_fetch(cmd, *, expected_paths=None, description=None):
                    self.assertEqual(description, "fetch_jd URL->JD")
                    (run_dir / "jd.txt").write_text(
                        "Senior Backend Engineer\n\n" + ("Build high-throughput APIs. " * 20))
                    (run_dir / "source.json").write_text(json.dumps({
                        "requested_url": "https://example.test/job",
                        "source_url": "https://example.test/job",
                    }))

                from packs.search.primitives.deep_search import decompose_jd
                with mock.patch.object(rl, "run", side_effect=fake_fetch), \
                     mock.patch.object(decompose_jd, "generate_queries", return_value=[
                         {"key": "q00", "query": "Backend engineer"}]) as generate, \
                     contextlib.redirect_stdout(io.StringIO()):
                    rl.main()
            finally:
                sys.argv = argv
            self.assertTrue((run_dir / "jd.txt").exists())  # URL was fetched to jd.txt before the loop
            self.assertIn("Build high-throughput APIs", generate.call_args.kwargs["jd"])
            self.assertFalse((run_dir / "epoch0").exists())

    def test_deep_search_loop_rejects_thin_fetched_jd(self):
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d) / "run"
            argv = sys.argv
            sys.argv = ["loop", "--jd-url", "https://example.test/js-job", "--run-dir", str(run_dir)]
            try:
                # a JS-rendered page fetches to near-empty text: the loop must stop, not build a garbage plan
                def fake_run(cmd, **kw):
                    (run_dir / "jd.txt").write_text("Apply now\n")
                    (run_dir / "source.json").write_text(json.dumps({
                        "requested_url": "https://example.test/js-job",
                        "source_url": "https://example.test/js-job",
                    }))
                with mock.patch.object(rl, "run", side_effect=fake_run):
                    with self.assertRaises(SystemExit) as ctx:
                        rl.main()
                self.assertEqual(ctx.exception.code, 1)  # thin JD -> hard fail before sourcing
            finally:
                sys.argv = argv


class TestFetchJDAshby(unittest.TestCase):
    """fetch_ashby early-outs (no network in either case)."""

    def test_non_ashby_host_returns_none(self):
        fj = _load("fetch_jd")
        self.assertIsNone(fj.fetch_ashby("https://jobs.lever.co/acme/2e718684-4f75-4a99-8d6b-3b6bd44e4228"))

    def test_ashby_url_without_job_uuid_returns_none(self):
        fj = _load("fetch_jd")
        self.assertIsNone(fj.fetch_ashby("https://jobs.ashbyhq.com/supabase"))


class TestLoopParserDefaults(unittest.TestCase):
    def test_loop_parser_defaults_pond_models(self):
        dsl = _load("deep_search_loop")
        # parse defaults directly via a fresh parser run
        parser_defaults = None
        real_parse = argparse.ArgumentParser.parse_args

        def spy(self, *a, **k):
            nonlocal parser_defaults
            parser_defaults = real_parse(self, ["--jd-file", "x", "--run-dir", "y"])
            raise SystemExit(0)

        with unittest.mock.patch.object(argparse.ArgumentParser, "parse_args", spy):
            with self.assertRaises(SystemExit):
                dsl.main()
        self.assertEqual(parser_defaults.query_model, "gpt-5.6-luna")
        self.assertEqual(parser_defaults.query_reasoning_effort, "medium")
        self.assertEqual(parser_defaults.expand_model, "gpt-5.6-luna")
        self.assertEqual(parser_defaults.expand_reasoning_effort, "medium")
        self.assertEqual(parser_defaults.filter_model, "gpt-5.6-luna")
        self.assertEqual(parser_defaults.filter_reasoning_effort, "none")
        self.assertEqual(parser_defaults.rerank_model, "gpt-5.6-luna")
        self.assertEqual(parser_defaults.rerank_reasoning_effort, "medium")


if __name__ == "__main__":
    unittest.main()
