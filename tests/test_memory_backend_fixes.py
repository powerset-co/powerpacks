"""Regression checks for backend review findings; providers are synthetic."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import unittest
from contextlib import ExitStack, closing, redirect_stdout
from dataclasses import asdict
from unittest.mock import Mock, patch
from urllib.parse import urlencode, urlparse

import httpx

import test_memory_job as jobs
from test_memory import response
from packs.ingestion.primitives.common.jsonio import short_hash
from packs.memory import job, runner
from packs.memory.__main__ import main
from packs.memory.cost import Usage
from packs.memory.discovery import Discover
from packs.memory.discovery_run import DiscoveryReport
from packs.memory.generate import Generate, parse_response
from packs.memory.import_stage import has_changes
from packs.memory.job import Counts, Failure, Job, Status
from packs.memory.progress import Progress, Step
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

    def test_update_availability_tracks_saved_inputs_without_writes(self):
        adapter = V2(self.root / '.powerpacks')
        with closing(Store(job.database(self.root), create=True)) as store:
            store.import_source(adapter)
        with closing(Store(job.database(self.root), read_only=True)) as store:
            self.assertFalse(has_changes(store, adapter))
            with sqlite3.connect(adapter.database) as source:
                source.execute("UPDATE candidates SET display_name=display_name || ' changed' WHERE candidate_id=(SELECT min(candidate_id) FROM candidates)")
            self.assertTrue(has_changes(store, adapter))
        with closing(Store(job.database(self.root))) as store:
            store.import_source(adapter)
            self.assertFalse(has_changes(store, adapter))
            with sqlite3.connect(adapter.database) as source:
                source.execute("DELETE FROM facts WHERE candidate_id=(SELECT min(candidate_id) FROM facts)")
            self.assertTrue(has_changes(store, adapter))

    def test_index_precedes_discovery_with_embeddings_available(self):
        order = []
        embeddings = []
        write = job.write

        def observe(root, record):
            write(root, record)
            if record.status == Status.RUNNING and (not order or order[-1] != record.step):
                order.append(record.step)
            if record.status == Status.RUNNING and record.step == Step.DISCOVER:
                with closing(Store(job.database(root))) as store:
                    embeddings.append(store.conn.execute("SELECT count(*) FROM embeddings").fetchone()[0])

        with patch.object(job, "write", side_effect=observe):
            runner.start(self.root, 1)
            completed = self.wait()
        self.assertEqual(completed.status, Status.DONE)
        expected = [Step.IMPORT, Step.GENERATE, Step.INDEX, Step.DISCOVER]
        self.assertEqual(order, expected)
        self.assertEqual(list(job.STEPS), expected)
        self.assertEqual(list(Step), [Step.IDLE, *expected, Step.DONE])
        self.assertTrue(embeddings)
        self.assertGreater(min(embeddings), 0)

    def test_preconditions_fail_before_start_and_api_returns_error(self):
        for action in ("enable", "update"):
            for key, model, embedding, error in (
                ("", "gpt-6-luna", "text-embedding-3-small", "OPENAI_API_KEY is not set"),
                ("synthetic", "unknown", "text-embedding-3-small", "model pricing unavailable"),
                ("synthetic", "gpt-6-luna", "unknown", "model pricing unavailable"),
            ):
                with self.subTest(action=action, key=bool(key), model=model, embedding=embedding):
                    job.write(self.root, Job(enabled=action == "update"))
                    handler = Mock()
                    body = json.dumps({"approved_spend_usd": 1}).encode()
                    handler.headers = {"Content-Length": str(len(body))}
                    handler.rfile = io.BytesIO(body)
                    with ExitStack() as patches:
                        patches.enter_context(patch.dict(os.environ, {"OPENAI_API_KEY": key}))
                        patches.enter_context(patch.object(self.provider, "model", model))
                        patches.enter_context(patch.object(self.provider, "embedding_key", embedding))
                        thread = patches.enter_context(patch.object(runner.threading, "Thread"))
                        imported = patches.enter_context(patch.object(Store, "import_progress"))
                        send = patches.enter_context(patch("packs.shared.web.memory._send_json"))
                        MemoryApi(self.root).post(handler, urlparse("/api/memory/" + action))
                    thread.assert_not_called()
                    imported.assert_not_called()
                    record = job.read(self.root)
                    self.assertEqual((record.status, record.error, record.failures), (Status.FAILED, error, []))
                    self.assertEqual(send.call_args.args[1]["error"], error)

    def test_discovery_retries_failures_across_pages_before_other_batches(self):
        with closing(sqlite3.connect(V2(self.root / ".powerpacks").database)) as source:
            with source:
                source.execute("UPDATE candidates SET display_name=?",
                               (" ".join(f"Group{number:03d}" for number in range(35)),))
        self.get(MemoryApi(self.root))
        with closing(Store(job.database(self.root))) as store:
            discover = Discover(store, self.provider)
            list(discover.groups())
            groups = list(discover.groups(after=discover.next_after))
            failed_keys = tuple(group.key + ":" + short_hash(json.dumps(
                [person.parent for person in Discover(store, self.provider)._parents(group)]), n=64) for group in groups)
        self.assertEqual(len(failed_keys), 2)
        job.write(self.root, Job(enabled=True, failures=[Failure(Step.DISCOVER, key, "synthetic", "now")
                                                       for key in failed_keys]))
        seen = []
        write = job.write

        def observe(root, record):
            write(root, record)
            if record.step == Step.DISCOVER and record.current_item:
                seen.append(record.current_item)

        with patch.object(job, "write", side_effect=observe):
            runner.update(self.root, 1)
            completed = self.wait()
        self.assertEqual(completed.status, Status.DONE)
        self.assertEqual(completed.failures, [])
        self.assertEqual(seen[:2], list(failed_keys))
        self.assertEqual(seen[2:], sorted(seen[2:], key=lambda key: key.rsplit(":", 1)[0]))
        self.assertEqual((len(seen), len(set(seen))), (102, 102))

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
        with patch.object(runner, "_run", side_effect=lambda root, resume, handle, client: os.close(handle)):
            started = runner.update(self.root, 2)
            self.wait()
        self.assertEqual(started.tokens, asdict(Usage()))
        self.assertEqual(started.spent_usd, 0)
        self.assertEqual(started.approved_spend_usd, 2)
        record = job.read(self.root)
        record.tokens = asdict(Usage(123, 456, 789))
        record.spent_usd = 0.5
        job.write(self.root, record)
        with patch.object(runner, "_run", side_effect=lambda root, resume, handle, client: os.close(handle)):
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
            completed = self.wait()
        self.assertEqual(completed.status, Status.DONE)
        self.assertEqual(len(completed.failures), 5)
        self.assertTrue(all("usage" in failure.error for failure in completed.failures))

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

    def test_search_ranks_people_and_people_pages_a_topic(self):
        api = MemoryApi(self.root)
        self.get(api)
        runner.start(self.root, 1)
        self.wait()
        # The page's search is the agent's search: people with their matching claims, not meanings.
        answer = self.get(api, "/api/memory/search?q=IAM")
        self.assertTrue(answer["people"])
        self.assertEqual(list(answer["layers"]), [])
        first = answer["people"][0]
        self.assertTrue(first["evidence"])
        self.assertIn("IAM", first["evidence"][0]["claim"]["text"])
        selector = {"origin": "topic", "value": first["evidence"][0]["claim"]["topic"]}
        page = self.get(api, "/api/memory/people?" + urlencode({**selector, "limit": 1}))
        self.assertGreaterEqual(page["total"], 1)
        self.assertEqual(len(page["people"]), 1)
        if page["next_after"]:
            second = self.get(api, "/api/memory/people?" + urlencode({**selector, "after": page["next_after"], "limit": 1}))
            self.assertNotEqual(second["people"][0]["parent"], page["people"][0]["parent"])

    def test_every_item_failing_still_runs_all_four_steps(self):
        def events(step, result=None):
            yield Progress(step, 1, 1, str(step), "synthetic " + step + " failure")
            return result

        with ExitStack() as stack:
            imported = stack.enter_context(patch.object(Store, "import_progress", return_value=events(Step.IMPORT)))
            generated = stack.enter_context(patch.object(Generate, "chunks", return_value=events(Step.GENERATE)))
            discovered = stack.enter_context(patch.object(
                Discover, "progress", return_value=events(Step.DISCOVER, DiscoveryReport())))
            indexed = stack.enter_context(patch.object(Generate, "index", return_value=events(Step.INDEX)))
            runner.start(self.root, 1)
            completed = self.wait()
        for operation in (imported, generated, indexed, discovered):
            operation.assert_called_once()
        self.assertEqual((completed.status, completed.step), (Status.DONE, Step.DONE))
        self.assertEqual([failure.step for failure in completed.failures], list(job.STEPS))
        for failure in completed.failures:
            self.assertEqual(completed.steps[failure.step], Counts(1, 1))
            self.assertTrue(failure.at)
        self.assertEqual(completed.error, "")

    def test_claim_exclusions_are_not_chunk_failures_and_index_still_runs(self):
        generate = self.provider.generate

        def with_exclusion(content):
            raw = json.loads(generate(content))
            claims = json.loads(raw["output"][0]["content"][0]["text"])["claims"]
            return response([*claims, {"invalid": "synthetic excluded claim"}])

        with patch.object(self.provider, "generate", side_effect=with_exclusion):
            runner.start(self.root, 1)
            completed = self.wait()
        self.assertEqual(completed.status, Status.DONE)
        self.assertEqual(completed.failures, [])
        self.assertGreater(completed.steps[Step.INDEX].done, 0)
        with closing(Store(job.database(self.root))) as store:
            report = Generate(store, self.provider).run(limit=100)
            self.assertEqual(report.exclusions, 5)
            self.assertEqual(report.failures, ())
            self.assertGreater(report.claims, 0)

    def test_preview_invalidates_on_wal_commit_without_main_file_change(self):
        source = V2(self.root / ".powerpacks").database
        with closing(sqlite3.connect(source)) as writer:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            api = MemoryApi(self.root)
            with patch.object(Generate, "estimate", side_effect=Generate.estimate, autospec=True) as estimate:
                self.get(api)
                self.get(api)
                self.assertEqual(estimate.call_count, 1)
                before = source.stat()
                with writer:
                    writer.execute("UPDATE facts SET facts_json=json_set(facts_json,'$.topics',json('[\"synthetic WAL topic\"]'))")
                after = source.stat()
                self.assertEqual((before.st_mtime_ns, before.st_size), (after.st_mtime_ns, after.st_size))
                self.get(api)
                self.assertEqual(estimate.call_count, 2)
            with closing(Store(job.database(self.root))) as store:
                self.assertGreater(store.conn.execute(
                    "SELECT count(*) FROM active_claims WHERE topic='synthetic wal topic'").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
