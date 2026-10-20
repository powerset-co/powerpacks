"""Round-4 regressions: name phrases from the first token, a store id as the corpus key, refresh after an action."""
from __future__ import annotations

import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from test_memory import source_db
from packs.memory.directory import Directory, Origin, Selector
from packs.memory.sources import V2
from packs.memory.store import Store

PEOPLE = [
    {"member": "c001", "parent": "p001", "name": "SoF Jordan", "claims": []},
    {"member": "c002", "parent": "p002", "name": "Casey Lee SoF", "claims": []},
    {"member": "c003", "parent": "p003", "name": "Morgan Reed", "claims": []},
]


class Round4Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.adapter = V2(self.base / "v2")
        source_db(self.adapter.database, PEOPLE)
        self.store = Store(self.base / "memory.sqlite", create=True)
        self.addCleanup(self.store.close)
        self.store.import_source(self.adapter)

    def test_a_name_phrase_resolves_from_either_end(self) -> None:
        page = Directory(self.store).enumerate(Selector(Origin.SOURCE_NAME, "sof"))
        self.assertEqual(page.total, 2)
        self.assertEqual([person.name for person in page.people], ["SoF Jordan", "Casey Lee SoF"])

    def test_the_corpus_is_the_store_id_so_a_moved_store_is_the_same_corpus(self) -> None:
        namespace = self.adapter.namespace
        self.assertTrue(namespace.startswith("dc:"))
        shutil.move(self.base / "v2", self.base / "elsewhere")
        moved = V2(self.base / "elsewhere")
        self.assertEqual(moved.namespace, namespace)
        report = self.store.import_source(moved)
        self.assertEqual((report.members, report.changed), (3, 0))  # same content, nothing re-projected
        self.assertEqual(Directory(self.store).enumerate(Selector(Origin.SOURCE_NAME, "sof")).total, 2)

    def test_refresh_marks_a_vanished_member_absent_and_rereads_the_rest(self) -> None:
        self.store.refresh(self.adapter, ("c003", "c999"))
        rows = {row[0]: row[1] for row in self.store.conn.execute("SELECT member,present FROM identities")}
        self.assertEqual(rows, {"c001": 1, "c002": 1, "c003": 1})
        self.store.refresh(self.adapter, ("zzz",))
        self.assertEqual(Directory(self.store).enumerate(Selector(Origin.SOURCE_NAME, "sof")).total, 2)


class EmbeddingWidthTests(unittest.TestCase):
    def test_embeddings_are_requested_at_768_and_cached_under_a_width_key(self) -> None:
        import json
        import httpx

        seen: list[dict[str, object]] = []
        self.seen = seen

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"data": [{"embedding": [3.0, 4.0]}], "usage": {"prompt_tokens": 5}})

        client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://example.test/")
        with unittest.mock.patch.dict("os.environ", {"OPENAI_API_KEY": "synthetic"}):
            self._check(client)

    def _check(self, client: object) -> None:
        from packs.memory.cost import Usage
        from packs.memory.provider import EMBEDDING_DIMENSIONS, OpenAIProvider
        import httpx
        assert isinstance(client, httpx.Client)
        seen = self.seen
        provider = OpenAIProvider("gpt-6-luna", "text-embedding-3-small@768", client=client, approve_spend=True)
        self.assertEqual(EMBEDDING_DIMENSIONS, 768)
        self.assertEqual(provider.embedding_key, "text-embedding-3-small@768")
        self.assertEqual(provider.embed("hello"), (0.6, 0.8))
        self.assertEqual(seen[0]["dimensions"], 768)
        self.assertEqual(seen[0]["model"], "text-embedding-3-small")
        self.assertIsNotNone(Usage(embedding=1000).cost("gpt-6-luna", provider.embedding_key))
