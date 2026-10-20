"""Regression checks for backend review findings; providers are synthetic."""
from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import closing, redirect_stdout
from dataclasses import asdict
from unittest.mock import Mock, patch
from urllib.parse import urlencode, urlparse

import httpx

import test_memory_job as jobs
from packs.memory import job, runner
from packs.memory.__main__ import main
from packs.memory.cost import Usage
from packs.memory.discovery import Discover
from packs.memory.generate import Generate, parse_response
from packs.memory.job import Job, Status
from packs.memory.progress import Step
from packs.memory.sources import V2
from packs.memory.provider import OpenAIProvider
from packs.memory.store import Store
from packs.shared.web.memory import MemoryApi


class BackendFixTests(unittest.TestCase):
    setUp = jobs.MemoryJobTests.setUp
    wait = jobs.MemoryJobTests.wait

    def get(self, api, path="/api/memory"):
        with patch("packs.shared.web.memory._send_json") as send:
            self.assertTrue(api.get(Mock(), urlparse(path)))
            return send.call_args.args[1]

    def test_status_projects_once_into_real_database_and_caches_estimate(self):
        api = MemoryApi(self.root)
        with patch.object(Generate, "estimate", return_value={
            "generation_input_tokens": 1, "generation_output_token_cap": 1, "embedding_input_tokens": 1,
        }) as estimate, patch.object(Store, "import_source", autospec=True, side_effect=Store.import_source) as imported:
            first = self.get(api)
            self.assertEqual(self.get(api), first)
            self.assertEqual(estimate.call_count, 1)
            self.assertEqual(imported.call_count, 1)
        self.assertTrue(job.database(self.root).exists())
        # A source change invalidates both inventory and estimate.
        source = V2(self.root / ".powerpacks").database
        os.utime(source, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns + 1))
        with patch.object(Generate, "estimate", return_value={
            "generation_input_tokens": 1, "generation_output_token_cap": 1, "embedding_input_tokens": 1,
        }) as estimate:
            self.get(api)
            estimate.assert_called_once()

    def test_get_resumes_dead_record(self):
        job.write(self.root, Job(enabled=True, step=Step.IMPORT, status=Status.RUNNING, approved_spend_usd=1))
        self.get(MemoryApi(self.root))
        self.assertEqual(self.wait().status, Status.DONE)
        self.assertEqual(job.read(self.root).pid, os.getpid())

    def test_update_resets_spend_but_resume_preserves_it(self):
        runner.start(self.root, 1)
        self.wait()
        record = job.read(self.root)
        record.tokens = asdict(Usage(123, 456, 789))
        record.spent_usd = 0.5
        job.write(self.root, record)
        with patch.object(runner, "_run", side_effect=lambda root, resume, handle: os.close(handle)):
            started = runner.update(self.root, 2)
            self.wait()
        self.assertEqual(started.tokens, asdict(Usage()))
        self.assertEqual(started.spent_usd, 0)
        self.assertEqual(started.approved_spend_usd, 2)
        record = job.read(self.root)
        record.tokens = asdict(Usage(123, 456, 789))
        record.spent_usd = 0.5
        job.write(self.root, record)
        with patch.object(runner, "_run", side_effect=lambda root, resume, handle: os.close(handle)):
            resumed = runner.start(self.root, 2)
            self.wait()
        self.assertEqual(resumed.tokens, record.tokens)
        self.assertEqual(resumed.spent_usd, 0.5)

    def test_budget_stops_once_and_next_update_continues(self):
        transport = httpx.Client(transport=httpx.MockTransport(lambda request: self.fail("no paid calls")))
        client = OpenAIProvider("gpt-6-luna", "text-embedding-3-small", client=transport,
                                approve_spend=True)
        with patch.object(runner, "provider", return_value=client):
            runner.start(self.root, 0)
            stopped = self.wait()
        self.assertEqual(stopped.status, Status.DONE)
        self.assertEqual(stopped.stopped, "approved spend reached")
        self.assertEqual(stopped.failures, [])
        self.assertLess(stopped.steps["generate"].done, stopped.steps["generate"].total)
        runner.update(self.root, 1)
        completed = self.wait()
        self.assertEqual(completed.stopped, "")
        self.assertGreater(len(self.provider.inputs), 0)

    def test_missing_usage_is_item_failure(self):
        for endpoint, usage in (("responses", None), ("responses", {"input_tokens": 1}),
                                ("embeddings", {})):
            with self.subTest(endpoint=endpoint, usage=usage):
                payload = {} if usage is None else {"usage": usage}
                transport = httpx.Client(base_url="https://example.com/", transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json=payload)))
                client = OpenAIProvider("gpt-6-luna", "text-embedding-3-small", client=transport, approve_spend=True)
                self.addCleanup(client.close)
                with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic"}):
                    with self.assertRaisesRegex(ValueError, "usage"):
                        client.generate("synthetic") if endpoint == "responses" else client.embed("synthetic")
                self.assertEqual(client.usage, Usage())

    def test_missing_usage_reaches_job_failures(self):
        transport = httpx.Client(base_url="https://example.com/", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={})))
        client = OpenAIProvider("gpt-6-luna", "text-embedding-3-small", client=transport, approve_spend=True)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic"}), patch.object(runner, "provider", return_value=client):
            runner.start(self.root, 1)
            failed = self.wait()
        self.assertEqual(failed.status, Status.FAILED)
        self.assertEqual(len(failed.failures), 5)
        self.assertTrue(all("usage" in failure.error for failure in failed.failures))

    def test_job_retries_cached_parse_failure_first(self):
        generate = self.provider.generate
        calls = 0

        def malformed(content):
            nonlocal calls
            calls += 1
            return "invalid" if calls == 2 else generate(content)

        with patch.object(self.provider, "generate", side_effect=malformed):
            runner.start(self.root, 1)
            completed = self.wait()
        self.assertEqual(completed.status, Status.DONE)
        self.assertEqual(len(completed.failures), 1)
        self.assertIn("invalid", completed.failures[0].error)
        with patch("packs.memory.generate.parse_response", wraps=parse_response) as parsed, patch.object(
                self.provider, "generate", side_effect=AssertionError("reuse cached response")):
            runner.update(self.root, 1)
            retried = self.wait()
        self.assertEqual(parsed.call_args_list[0].args[0], "invalid")
        self.assertEqual(retried.failures[0].key, completed.failures[0].key)
        self.assertEqual(retried.status, Status.DONE)

    def test_whole_run_reports_provider_failures(self):
        self.get(MemoryApi(self.root))
        with closing(Store(job.database(self.root))) as store:
            with patch.object(self.provider, "generate", side_effect=ValueError("synthetic provider failure")):
                report = Generate(store, self.provider).run(limit=100)
            self.assertEqual(len(report.failures), 5)
            store.conn.execute("DELETE FROM phrases")
            store.conn.execute("INSERT INTO phrases SELECT id,'synthetic shared name' FROM sources")
            store.conn.commit()
            with patch.object(self.provider, "discover", side_effect=ValueError("synthetic provider failure")):
                self.assertTrue(Discover(store, self.provider).run(limit=100, min_people=2).failures)

    def test_whole_run_reports_parse_failures_without_rebuying(self):
        api = MemoryApi(self.root)
        self.get(api)
        store = Store(job.database(self.root))
        self.addCleanup(store.close)
        with patch.object(self.provider, "generate", return_value="invalid") as generated:
            report = Generate(store, self.provider).run(limit=100)
            self.assertTrue(report.failures)
            self.assertEqual(len(report.failures), 5)
            Generate(store, self.provider).run(limit=100)
            self.assertEqual(generated.call_count, 5)
        # Shared saved names nominate discovery without relying on generated claims.
        store.conn.execute("DELETE FROM phrases")
        store.conn.execute("INSERT INTO phrases SELECT id,'synthetic shared name' FROM sources")
        store.conn.commit()
        with patch.object(self.provider, "discover", return_value="invalid") as discovered:
            report = Discover(store, self.provider).run(limit=100, min_people=2)
            self.assertTrue(report.failures)
            Discover(store, self.provider).run(limit=100, min_people=2)
            self.assertEqual(discovered.call_count, report.calls)
        with patch("packs.memory.__main__.OpenAIProvider", return_value=self.provider), redirect_stdout(io.StringIO()) as output:
            code = main(["--db", str(job.database(self.root)), "generate", "--model", "synthetic",
                         "--embedding-model", "fixture", "--limit", "100"])
        self.assertEqual(code, 1)
        self.assertTrue(json.loads(output.getvalue())["failures"])

    def test_search_and_people_use_lexical_directory(self):
        api = MemoryApi(self.root)
        self.get(api)
        runner.start(self.root, 1)
        self.wait()
        with patch.object(OpenAIProvider, "embed", side_effect=AssertionError("lexical only")):
            meanings = self.get(api, "/api/memory/search?q=IAM")
            self.assertTrue(meanings)
            selector = meanings[0]["selector"]
            page = self.get(api, "/api/memory/people?" + urlencode({**selector, "limit": 1}))
            self.assertEqual(page["total"], meanings[0]["count"])
            self.assertEqual(len(page["people"]), 1)
            if page["next_after"]:
                second = self.get(api, "/api/memory/people?" + urlencode({**selector, "after": page["next_after"], "limit": 1}))
                self.assertNotEqual(second["people"][0]["parent"], page["people"][0]["parent"])


if __name__ == "__main__":
    unittest.main()
