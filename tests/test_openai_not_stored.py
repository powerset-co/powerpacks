"""Every OpenAI request asks OpenAI not to store it."""
import asyncio
import unittest
from types import SimpleNamespace

from packs.indexing.lib import llm_config, openai_responses as index_responses
from packs.ingestion.primitives.deep_context.shared import openai_responses

ANSWER = SimpleNamespace(status="completed", output_text='{"ok": true}', usage=None)


class NotStoredTests(unittest.TestCase):
    def test_deep_context_responses_are_not_stored(self):
        sent = []

        async def create(**kwargs):
            sent.append(kwargs)
            return ANSWER

        config = openai_responses.OpenAIResponsesConfig("gpt-6.1-sol", "low", 1, 30, 0)
        caller = openai_responses.OpenAIResponsesCaller(config, client=SimpleNamespace(
            responses=SimpleNamespace(create=create)))
        asyncio.run(caller.call(system_prompt="fixture", user_prompt="fixture",
                                schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
                                schema_name="fixture", context="fixture"))
        self.assertIs(sent[0]["store"], False)

    def test_indexing_responses_and_chat_completions_are_not_stored(self):
        self.assertIs(index_responses.responses_kwargs("gpt-5.1")["store"], False)
        self.assertIs(llm_config.api_call_kwargs("gpt-5.1")["store"], False)


if __name__ == "__main__":
    unittest.main()
