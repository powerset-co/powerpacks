"""Bounded generation uses synthetic providers and the real SQLite checkpoints."""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future, ThreadPoolExecutor, wait
from unittest.mock import patch

import httpx
from pathlib import Path

from test_memory import FixtureProvider, response, source_db
from packs.memory.generate import Generate
from packs.memory.progress import consume
from packs.memory.provider import CHUNK_CHARS, OpenAIProvider, SpendReached
from packs.indexing.lib.openai_responses import responses_kwargs, structured_response_kwargs
from packs.memory.sources import V2
from packs.memory.store import Store


class ParallelGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        concurrency = patch.dict('os.environ', {'POWERPACKS_OPENAI_CONCURRENCY': '64'})
        concurrency.start()
        self.addCleanup(concurrency.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        adapter = V2(base / 'source')
        source_db(adapter.database, ({'member': f'c{i}', 'parent': f'p{i}',
                                     'name': f'Synthetic Person {i}', 'claims': []} for i in range(80)))
        self.store = Store(base / 'memory.sqlite', create=True)
        self.addCleanup(self.store.close)
        self.store.import_source(adapter)

    def test_64_provider_calls_overlap_and_cache_replays_without_calls(self) -> None:
        class Provider(FixtureProvider):
            def __init__(self) -> None:
                super().__init__()
                self.lock = threading.Lock()
                self.active = self.peak = 0
                self.overlap = threading.Event()
                self.threads: set[int] = set()

            def generate(self, content: str) -> str:
                with self.lock:
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                    self.threads.add(threading.get_ident())
                    if self.active == 64:
                        self.overlap.set()
                self.overlap.wait(2)
                try:
                    return super().generate(content)
                finally:
                    with self.lock:
                        self.active -= 1

        provider = Provider()
        report = consume(Generate(self.store, provider).chunks(limit=70))
        self.assertEqual(report.generation_calls, 70)
        self.assertEqual(provider.peak, 64)
        self.assertNotIn(threading.get_ident(), provider.threads)
        saved = self.store.conn.execute('SELECT count(*) FROM chunks').fetchone()[0]
        self.assertEqual(saved, 70)
        resumed = consume(Generate(self.store, provider).chunks(limit=1))
        self.assertEqual((resumed.reused_chunks, resumed.generation_calls), (70, 1))
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM chunks').fetchone()[0], 71)

    def test_fast_completions_replenish_slots_while_first_request_is_pending(self) -> None:
        class Provider(FixtureProvider):
            def __init__(self) -> None:
                super().__init__()
                self.lock = threading.Lock()
                self.started = 0
                self.replenished = threading.Event()

            def generate(self, content: str) -> str:
                with self.lock:
                    self.started += 1
                    first = self.started == 1
                    if self.started > 64:
                        self.replenished.set()
                if first and not self.replenished.wait(2):
                    raise AssertionError('scheduler waited for the slow first request')
                return super().generate(content)

        provider = Provider()
        report = consume(Generate(self.store, provider).chunks(limit=70))
        self.assertTrue(provider.replenished.is_set())
        self.assertEqual(report.failures, ())
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM chunks').fetchone()[0], 70)

    def test_inputs_and_paid_responses_stay_bounded_when_progress_closes(self) -> None:
        generate = Generate(self.store, FixtureProvider())
        inputs = generate._inputs(())
        read = 0

        def counted(first):
            nonlocal read
            for item in inputs:
                read += 1
                yield item

        with patch.object(generate, '_inputs', counted):
            events = generate.chunks(limit=1000)
            next(events)
            self.assertEqual(read, 64)
            events.close()
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM chunks').fetchone()[0], 64)

    def test_completed_responses_are_checkpointed_before_progress_without_waiting_for_slow_call(self) -> None:
        class Provider(FixtureProvider):
            def __init__(self) -> None:
                super().__init__()
                self.lock = threading.Lock()
                self.started = 0
                self.finished = 0
                self.ready = threading.Event()
                self.release = threading.Event()

            def generate(self, content: str) -> str:
                with self.lock:
                    self.started += 1
                    first = self.started == 1
                if first:
                    self.release.wait(2)
                result = super().generate(content)
                with self.lock:
                    self.finished += 1
                    if self.finished >= 62:
                        self.ready.set()
                return result

        provider = Provider()
        generate = Generate(self.store, provider)
        inputs = generate._inputs(())

        def queued(first):
            for number, item in enumerate(inputs, 1):
                if number == 64:
                    self.assertTrue(provider.ready.wait(1))
                yield item

        with patch.object(generate, '_inputs', queued):
            events = generate.chunks(limit=64)
            try:
                next(events)
                self.assertFalse(provider.release.is_set())
                self.assertGreaterEqual(self.store.conn.execute('SELECT count(*) FROM chunks').fetchone()[0], 62)
            finally:
                provider.release.set()
                events.close()
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM chunks').fetchone()[0], 64)

    def test_shared_config_can_lower_generation_concurrency(self) -> None:
        class Provider(FixtureProvider):
            def __init__(self) -> None:
                super().__init__()
                self.lock = threading.Lock()
                self.active = self.peak = 0
                self.ready = threading.Event()

            def generate(self, content: str) -> str:
                with self.lock:
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                    if self.active == 2:
                        self.ready.set()
                self.ready.wait(1)
                try:
                    return super().generate(content)
                finally:
                    with self.lock:
                        self.active -= 1

        provider = Provider()
        with patch.dict('os.environ', {'POWERPACKS_OPENAI_CONCURRENCY': '2'}):
            consume(Generate(self.store, provider).chunks(limit=5))
        self.assertEqual(provider.peak, 2)

    def test_failed_attempts_and_inflight_calls_count_toward_limit(self) -> None:
        class Provider(FixtureProvider):
            def generate(self, content: str) -> str:
                self.generations += 1
                raise ValueError('synthetic failure')

        provider = Provider()
        report = Generate(self.store, provider).run(limit=3)
        self.assertEqual((provider.generations, report.generation_calls), (3, 3))
        self.assertEqual(len(report.failures), 3)
        self.assertEqual(provider.embeddings, 0)

    def test_spend_error_drains_out_of_order_successes_before_propagating(self) -> None:
        class Provider(FixtureProvider):
            def generate(self, content: str) -> str:
                if 'Synthetic Person 0' in content:
                    raise SpendReached('synthetic spend reached')
                time.sleep(0.02)
                return super().generate(content)

        with self.assertRaises(SpendReached):
            consume(Generate(self.store, Provider()).chunks(limit=8))
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM chunks').fetchone()[0], 7)
        again = consume(Generate(self.store, FixtureProvider()).chunks(limit=1))
        self.assertEqual(again.generation_calls, 1)
        self.assertGreaterEqual(again.reused_chunks, 7)

    def test_closing_progress_keeps_already_paid_batch(self) -> None:
        provider = FixtureProvider()
        events = Generate(self.store, provider).chunks(limit=8)
        next(events)
        events.close()
        self.assertEqual(provider.generations, 8)
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM chunks').fetchone()[0], 8)

    def test_repeated_chunk_text_reuses_one_inflight_response(self) -> None:
        source = next(self.store.sources())
        with self.store.conn:
            self.store.conn.execute('UPDATE sources SET present=0 WHERE id<>?', (source.id,))
            self.store.conn.execute('UPDATE revisions SET content=? WHERE source_id=? AND hash=?',
                                    ('x' * CHUNK_CHARS * 4, source.id, source.hash))
        provider = FixtureProvider()
        with patch.object(provider, 'generate', return_value=response([])) as generate:
            report = consume(Generate(self.store, provider).chunks(limit=1))
        self.assertEqual(generate.call_count, 1)
        self.assertEqual((report.generation_calls, report.reused_chunks), (1, 3))
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM chunks').fetchone()[0], 4)

    def test_cancellation_while_waiting_drains_and_checkpoints_all_requests(self) -> None:
        result = Future.result
        calls = 0

        def interrupted(future, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise KeyboardInterrupt()
            return result(future, *args, **kwargs)

        with patch.object(Future, 'result', interrupted):
            with self.assertRaises(KeyboardInterrupt):
                consume(Generate(self.store, FixtureProvider()).chunks(limit=8))
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM chunks').fetchone()[0], 8)

    def test_generation_and_failed_embeddings_share_one_call_limit(self) -> None:
        class Provider(FixtureProvider):
            def generate(self, content: str) -> str:
                self.generations += 1
                return response([{'text': 'Synthetic searchable statement', 'kind': 'statement',
                                  'topic': 'synthetic', 'scope': '', 'object': '', 'start': '', 'end': '',
                                  'quote': content, 'aliases': []}])

        provider = Provider()
        provider.fail_embedding = 1
        report = Generate(self.store, provider).run(limit=65)
        self.assertEqual((report.generation_calls, report.embedding_calls), (64, 1))
        self.assertEqual((provider.generations, provider.embeddings), (64, 1))
        self.assertEqual(report.failures[0][1], 'synthetic embedding interruption')


class SharedRequestTests(unittest.TestCase):
    def test_shared_schema_options_preserve_deep_context_policy_and_memory_cache_key(self) -> None:
        schema = {'type': 'object'}
        options = structured_response_kwargs(schema, 'synthetic')
        self.assertEqual(options, {'store': False, 'text': {'format': {
            'type': 'json_schema', 'name': 'synthetic', 'strict': True, 'schema': schema}}})
        with patch.dict('os.environ', {'POWERPACKS_DEEP_CONTEXT_MAX_OUTPUT_TOKENS': '1234',
                                       'POWERPACKS_DEEP_CONTEXT_REASONING_EFFORT': 'high',
                                       'POWERPACKS_OPENAI_SERVICE_TIER': 'flex'}):
            self.assertEqual(responses_kwargs('gpt-6-luna', schema=schema, schema_name='synthetic'),
                             {**options, 'max_output_tokens': 1234, 'reasoning': {'effort': 'high'},
                              'service_tier': 'flex'})
        self.assertEqual(structured_response_kwargs(None, 'unused'), {'store': False})
        provider = OpenAIProvider('gpt-6-luna', 'text-embedding-3-small')
        self.addCleanup(provider.close)
        self.assertEqual(provider.generation_key,
                         '8cddebae0f8c098b25523bb4a2d5032dee16fbed1a4b0b0ce0a229dfc759bceb')

    def test_sdk_retries_transient_errors_and_preserves_raw_response_and_caps(self) -> None:
        requests = []
        raw = '{"usage":{"input_tokens":2,"output_tokens":3},"status":"incomplete","output":[]}'

        def respond(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(429, headers={'retry-after-ms': '1'}, json={'error': {'message': 'synthetic'}})
            return httpx.Response(200, text=raw)

        client = httpx.Client(transport=httpx.MockTransport(respond))
        provider = OpenAIProvider('gpt-6-luna', 'text-embedding-3-small', client=client, approve_spend=True)
        self.addCleanup(provider.close)
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'synthetic',
                                       'POWERPACKS_DEEP_CONTEXT_MAX_OUTPUT_TOKENS': '1'}):
            self.assertEqual(provider.generate('Synthetic input'), raw)
        self.assertEqual(len(requests), 2)
        body = json.loads(requests[1].content)
        self.assertEqual((body['store'], body['max_output_tokens']), (False, 6000))
        self.assertNotIn('reasoning', body)
        self.assertNotIn('service_tier', body)
        self.assertEqual((provider.usage.input, provider.usage.output), (2, 3))

    def test_sdk_errors_do_not_expose_input(self) -> None:
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
            400, json={'error': {'message': 'Synthetic private input'}})))
        provider = OpenAIProvider('gpt-6-luna', 'text-embedding-3-small', client=client, approve_spend=True)
        self.addCleanup(provider.close)
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'synthetic'}):
            with self.assertRaisesRegex(ValueError, '^provider responses: HTTP 400$'):
                provider.generate('Synthetic private input')


class ProviderConcurrencyTests(unittest.TestCase):
    def test_spend_guard_reserves_inflight_requests_and_records_actual_usage(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def respond(request):
            calls.append(request)
            entered.set()
            release.wait(2)
            return httpx.Response(200, json={'usage': {'input_tokens': 100, 'output_tokens': 20}})

        transport = httpx.Client(base_url='https://example.test/', transport=httpx.MockTransport(respond))
        self.addCleanup(transport.close)
        provider = OpenAIProvider('gpt-6-luna', 'text-embedding-3-small', client=transport,
                                  approve_spend=True, approved_spend_usd=0.005)
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'synthetic'}), ThreadPoolExecutor(max_workers=8) as workers:
            futures = [workers.submit(provider.generate, 'Synthetic input') for _ in range(8)]
            try:
                self.assertTrue(entered.wait(1))
                done, _ = wait(futures, timeout=0.2)
                self.assertEqual(len(done), 7)
                self.assertTrue(all(isinstance(future.exception(), SpendReached) for future in done))
            finally:
                release.set()
            wait(futures)
        self.assertEqual(len(calls), 1)
        self.assertEqual((provider.usage.input, provider.usage.output), (100, 20))
        self.assertAlmostEqual(provider._reserved_usd, 0)
