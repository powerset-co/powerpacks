"""A request Flex cannot take is sent again on the standard tier."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

import httpx
from openai import RateLimitError

from packs.ingestion.primitives.deep_context.shared import openai_responses

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


def _flex_unavailable() -> RateLimitError:
    response = httpx.Response(429, request=httpx.Request("POST", "https://api.openai.test/v1/responses"))
    return RateLimitError("Flex processing is temporarily unavailable", response=response, body=None)


class _Client:
    """Answers each `responses.create` with the next outcome and keeps the tier it was asked on."""

    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.tiers: list[str | None] = []
        self.responses = SimpleNamespace(create=self.create)

    async def create(self, **kwargs: object) -> object:
        self.tiers.append(kwargs.get("service_tier"))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


ANSWER = SimpleNamespace(status="completed", output_text='{"ok": true}', usage=None)


class FlexUnavailableTests(unittest.TestCase):
    def call(self, client: _Client, *, tier: str = "flex") -> dict:
        config = openai_responses.OpenAIResponsesConfig("gpt-6.1-sol", "low", 1, 30, 0)
        caller = openai_responses.OpenAIResponsesCaller(config, client=client)
        with mock.patch.dict("os.environ", {"POWERPACKS_OPENAI_SERVICE_TIER": tier}):
            return asyncio.run(caller.call(
                system_prompt="fixture system", user_prompt="fixture prompt",
                schema=SCHEMA, schema_name="fixture", context="fixture",
            )).payload

    def test_a_request_flex_cannot_take_is_answered_on_the_standard_tier(self) -> None:
        client = _Client(_flex_unavailable(), ANSWER)
        self.assertEqual(self.call(client), {"ok": True})
        self.assertEqual(client.tiers, ["flex", "default"])

    def test_a_request_the_standard_tier_also_refuses_fails(self) -> None:
        client = _Client(_flex_unavailable(), _flex_unavailable())
        with self.assertRaises(RateLimitError):
            self.call(client)
        self.assertEqual(client.tiers, ["flex", "default"])

    def test_a_refused_standard_tier_request_is_not_sent_again(self) -> None:
        client = _Client(_flex_unavailable())
        with self.assertRaises(RateLimitError):
            self.call(client, tier="default")
        self.assertEqual(client.tiers, ["default"])


if __name__ == "__main__":
    unittest.main()
