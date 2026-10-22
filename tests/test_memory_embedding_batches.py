"""Embedding batches use mocked HTTP and real, main-thread SQLite checkpoints."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from unittest.mock import patch

import httpx
import tiktoken

from test_memory import FixtureProvider, source_db
from packs.memory.generate import EMBEDDING_BATCH_TOKENS, Generate, GenerationReport
from packs.memory.models import Claim, Kind
from packs.memory.progress import consume
from packs.memory.provider import OpenAIProvider, SpendReached
from packs.memory.sources import V2
from packs.memory.store import Store


class EmbeddingBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        adapter = V2(base / 'source')
        source_db(adapter.database, [{'member': 'c', 'parent': 'p', 'name': 'Synthetic', 'claims': []}])
        self.store = Store(base / 'memory.sqlite', create=True)
        self.addCleanup(self.store.close)
        self.store.import_source(adapter)
        self.source = next(self.store.sources())
        capacity = patch('packs.memory.generate.concurrency_limit', side_effect=lambda maximum: maximum)
        capacity.start()
        self.addCleanup(capacity.stop)

    def claims(self, count: int, extra: str = '') -> int:
        with self.store.conn:
            for number in range(count):
                self.store.add_claim(self.source.id, self.source.hash, None, Claim(
                    f'Synthetic claim {number} {extra}', Kind.STATEMENT, 'topic', '', '', '', '', 'quote'), human=True)
        return self.store.conn.execute('SELECT count(DISTINCT document_hash) FROM active_claims').fetchone()[0]

    def saved(self) -> int:
        return self.store.conn.execute('SELECT count(*) FROM embeddings').fetchone()[0]

    def test_batches_replenish_eight_slots_and_resume_uses_cache(self) -> None:
        total = self.claims(640)
        class Provider(FixtureProvider):
            def __init__(self):
                super().__init__()
                self.lock = threading.Lock()
                self.requests = []
                self.active = self.peak = 0
                self.full = threading.Event()
                self.replenished = threading.Event()

            def embed_many(self, contents):
                with self.lock:
                    self.requests.append(contents)
                    first = len(self.requests) == 1
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                    if self.active == 8:
                        self.full.set()
                    if len(self.requests) > 8:
                        self.replenished.set()
                try:
                    if first:
                        if not self.replenished.wait(2):
                            raise AssertionError('slow first request blocked replenishment')
                    elif not self.full.wait(2):
                        raise AssertionError('eight requests did not overlap')
                    return super().embed_many(contents)
                finally:
                    with self.lock:
                        self.active -= 1

        provider = Provider()
        report = consume(Generate(self.store, provider).index(limit=100))
        self.assertEqual(report.failures, ())
        self.assertEqual(provider.peak, 8)
        self.assertTrue(provider.replenished.is_set())
        self.assertEqual(report.embedding_calls, (total + 63) // 64)
        self.assertEqual(self.saved(), total)
        self.assertTrue(all(1 <= len(batch) <= 64 for batch in provider.requests))
        report = consume(Generate(self.store, provider).index(limit=100))
        self.assertEqual(report.embedding_calls, 0)
        self.assertEqual(self.saved(), total)

    def test_completed_batches_commit_before_progress_and_close_drains_paid_calls(self) -> None:
        self.claims(640)
        provider = FixtureProvider()
        events = Generate(self.store, provider).index(limit=100)
        first = next(events)
        self.assertGreaterEqual(self.saved(), 64)
        self.assertEqual(first.done, 1)
        events.close()
        self.assertEqual(provider.embeddings, 8)
        self.assertEqual(self.saved(), 512)
        report = consume(Generate(self.store, provider).index(limit=100))
        self.assertEqual(report.embedding_calls, 2)

    def test_failed_batch_and_inflight_requests_share_generation_call_limit(self) -> None:
        self.claims(640)
        provider = FixtureProvider()
        provider.fail_embedding = 1
        report = GenerationReport(generation_calls=2)
        consume(Generate(self.store, provider).index(limit=5, report=report))
        self.assertEqual((report.generation_calls, report.embedding_calls), (2, 3))
        self.assertEqual(provider.embeddings, 3)
        self.assertEqual(len(report.failures), 64)
        self.assertEqual(self.saved(), 128)
        resumed = consume(Generate(self.store, provider).index(limit=1))
        self.assertEqual(resumed.embedding_calls, 1)
        self.assertEqual(self.saved(), 192)

    def test_spend_failure_drains_other_paid_batches(self) -> None:
        self.claims(640)
        class Provider(FixtureProvider):
            def __init__(self):
                super().__init__()
                self.lock = threading.Lock()
                self.requests = 0

            def embed_many(self, contents):
                with self.lock:
                    self.requests += 1
                    first = self.requests == 1
                if first:
                    raise SpendReached('synthetic spend reached')
                return super().embed_many(contents)

        provider = Provider()
        with self.assertRaises(SpendReached):
            consume(Generate(self.store, provider).index(limit=8))
        self.assertEqual(provider.requests, 8)
        self.assertEqual(self.saved(), 7 * 64)

    def test_batches_obey_token_limit_as_well_as_document_limit(self) -> None:
        self.claims(10, ' token' * 2000)
        provider = FixtureProvider()
        encoder = tiktoken.get_encoding('cl100k_base')
        with patch.object(provider, 'embed_many', wraps=provider.embed_many) as requests:
            report = consume(Generate(self.store, provider).index(limit=100))
        self.assertEqual(report.failures, ())
        self.assertGreater(report.embedding_calls, 1)
        for request in requests.call_args_list:
            self.assertLessEqual(sum(len(encoder.encode(text)) for text in request.args[0]), EMBEDDING_BATCH_TOKENS)


class EmbeddingHttpTests(unittest.TestCase):
    def provider(self, respond, *, budget=None):
        client = httpx.Client(transport=httpx.MockTransport(respond))
        provider = OpenAIProvider('gpt-6-luna', 'text-embedding-3-small@768', client=client,
                                  approve_spend=True, approved_spend_usd=budget)
        self.addCleanup(provider.close)
        env = patch.dict('os.environ', {'OPENAI_API_KEY': 'synthetic'})
        env.start()
        self.addCleanup(env.stop)
        return provider

    def test_real_request_batches_and_matches_unordered_response_indexes(self) -> None:
        requests = []
        def respond(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={'usage': {'prompt_tokens': 3}, 'data': [
                {'index': 1, 'embedding': [0, 1] + [0] * 766},
                {'index': 0, 'embedding': [1, 0] + [0] * 766}]})
        provider = self.provider(respond)
        result = provider.embed_many(('first', 'second'))
        self.assertEqual((result[0][:2], result[1][:2]), ((1, 0), (0, 1)))
        self.assertEqual(requests, [{'model': 'text-embedding-3-small', 'input': ['first', 'second'],
                                     'dimensions': 768, 'encoding_format': 'float'}])
        self.assertEqual(provider.usage.embedding, 3)

    def test_rejects_entire_invalid_response(self) -> None:
        good = {'index': 0, 'embedding': [1] + [0] * 767}
        cases = [[good], [good, good], [good, {'index': 2, 'embedding': [1] * 768}],
                 [good, {'index': 1, 'embedding': [1, 0]}],
                 [good, {'index': 1, 'embedding': [0] * 768}],
                 [good, {'index': 1, 'embedding': [True] * 768}]]
        for data in cases:
            with self.subTest(data_length=len(data)):
                provider = self.provider(lambda request: httpx.Response(200, json={
                    'usage': {'prompt_tokens': 3}, 'data': data}))
                with self.assertRaises(ValueError):
                    provider.embed_many(('first', 'second'))
                self.assertEqual(provider.usage.embedding, 3)

    def test_embedding_budget_reserves_whole_inflight_batch(self) -> None:
        entered, release = threading.Event(), threading.Event()
        calls = []
        def respond(request):
            calls.append(request)
            entered.set()
            release.wait(2)
            return httpx.Response(200, json={'usage': {'prompt_tokens': 10}, 'data': [
                {'index': i, 'embedding': [1] + [0] * 767} for i in range(2)]})
        provider = self.provider(respond, budget=0.00003)
        with ThreadPoolExecutor(max_workers=8) as workers:
            futures = [workers.submit(provider.embed_many, ('first', 'second')) for _ in range(8)]
            try:
                self.assertTrue(entered.wait(1))
                done, _ = wait(futures, timeout=0.2)
                self.assertEqual(len(done), 7)
                self.assertTrue(all(isinstance(item.exception(), SpendReached) for item in done))
            finally:
                release.set()
            wait(futures)
        self.assertEqual(len(calls), 1)
        self.assertEqual(provider.usage.embedding, 10)
        self.assertAlmostEqual(provider._reserved_usd, 0)
