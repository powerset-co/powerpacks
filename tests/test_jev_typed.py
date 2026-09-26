"""Worth inputs preserve cache bytes while policy uses typed values."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from packs.ingestion.primitives.deep_context.db.models import OwnerProfile
from packs.ingestion.primitives.deep_context.jev_worth import questions, runner
from packs.ingestion.primitives.deep_context.jev_worth.models import WorthFacts, WorthResult
from packs.ingestion.primitives.deep_context.synthesis.models import NetworkWorthFact


class TypedWorthTests(unittest.TestCase):
    def test_request_preserves_fact_order_and_unknown_fields(self):
        payload = {'title': 'Engineer', 'historical_fact': 'keep', 'canonical_name': 'Casey Bravo'}
        request = questions.build_request(facts=WorthFacts.from_payload(payload), bundle=None,
                                          owner=OwnerProfile('Owner'), reference_date='2026-01-01')
        self.assertEqual(request['state']['facts'], payload)
        self.assertEqual(request['state']['dossier'], 'title: "Engineer"\nhistorical_fact: "keep"\ncanonical_name: "Casey Bravo"')

    def test_classify_returns_typed_worth_and_usage(self):
        async def answer(requests, **kwargs):
            return {key: SimpleNamespace(response={'answers': {'noise': {'type': 'noul', 'noul': .1}},
                'usage': {'input_tokens': 100, 'output_tokens': 2}}, cached=False) for key in requests}
        with tempfile.TemporaryDirectory() as directory, patch.object(runner, 'answer_requests', answer), \
                patch.object(runner, 'predict', return_value='yes'), patch.object(runner, '_reason', return_value='Context'):
            result = asyncio.run(runner.classify(facts=WorthFacts.from_payload({'canonical_name': 'Casey Bravo'}),
                bundle=None, owner=OwnerProfile('Owner'), reference_date='2026-01-01', output_dir=Path(directory)))
        self.assertIsInstance(result, WorthResult)
        self.assertEqual(result.worth, NetworkWorthFact('yes', 'Context'))
        self.assertEqual(result.usage.people, 1)
        self.assertEqual(result.labels, {'noise': .1})
