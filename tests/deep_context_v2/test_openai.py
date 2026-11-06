"""No SDK request leaves the process; exercise the real request/parser boundary."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from openai import RateLimitError

from packs.ingestion.primitives.deep_context_v2 import openai as provider


class OpenAITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic", "OPENAI_BASE_URL": "", "POWERPACKS_OPENAI_SERVICE_TIER": "flex"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock()), close=AsyncMock())
        self.sdk = patch.object(provider, "AsyncOpenAI", return_value=self.client)
        self.factory = self.sdk.start()
        self.addCleanup(self.sdk.stop)
        self.config = provider.OpenAIResponsesConfig("gpt-5", "medium", 2, 17, 3)
        self.caller = provider.OpenAIResponsesCaller(self.config)
        self.kwargs = dict(system_prompt="system", user_prompt="user", schema={"type": "object"}, schema_name="synthetic", context="unit test")

    async def test_request_shape_and_client_lifecycle(self):
        self.client.responses.create.return_value = SimpleNamespace(output_text='{"answer": true}')
        async with self.caller as caller:
            self.assertEqual(await caller.call(**self.kwargs), {"answer": True})
        self.client.close.assert_awaited_once()
        self.factory.assert_called_once_with(api_key="synthetic", base_url=None, timeout=17, max_retries=3)
        request = self.client.responses.create.call_args.kwargs
        self.assertEqual(request["input"], [{"role": "system", "content": "system"}, {"role": "user", "content": "user"}])
        self.assertEqual(request["model"], "gpt-5")
        self.assertTrue(request["text"]["format"]["strict"])
        self.assertFalse(request["store"])
        self.assertEqual(request["reasoning"], {"effort": "medium"})

    def rate_limit(self):
        response = httpx.Response(429, request=httpx.Request("POST", "https://synthetic.invalid"))
        return RateLimitError("no capacity", response=response, body=None)

    async def test_flex_retries_once_on_standard(self):
        requests = []
        async def create(**kwargs):
            requests.append(kwargs.copy())
            if len(requests) == 1:
                raise self.rate_limit()
            return SimpleNamespace(output_text="{}")
        self.client.responses.create.side_effect = create
        with patch.dict(os.environ, {"POWERPACKS_OPENAI_SERVICE_TIER": "flex"}):
            self.assertEqual(await self.caller.call(**self.kwargs), {})
        self.assertEqual([r["service_tier"] for r in requests], ["flex", "default"])

    async def test_standard_rate_limit_is_not_retried(self):
        self.client.responses.create.side_effect = self.rate_limit()
        with patch.dict(os.environ, {"POWERPACKS_OPENAI_SERVICE_TIER": "default"}):
            with self.assertRaises(RateLimitError):
                await self.caller.call(**self.kwargs)
        self.assertEqual(self.client.responses.create.await_count, 1)

    async def test_second_rate_limit_propagates(self):
        self.client.responses.create.side_effect = self.rate_limit()
        with self.assertRaises(RateLimitError):
            await self.caller.call(**self.kwargs)
        self.assertEqual(self.client.responses.create.await_count, 2)


    async def test_missing_output_and_sdk_nested_output(self):
        self.client.responses.create.return_value = SimpleNamespace()
        with self.assertRaisesRegex(ValueError, "unit test: empty response"):
            await self.caller.call(**self.kwargs)
        self.client.responses.create.return_value = SimpleNamespace(output=[SimpleNamespace(content=[SimpleNamespace(text='{"ok":'), SimpleNamespace(text="true}")])])
        self.assertEqual(await self.caller.call(**self.kwargs), {"ok": True})

    async def test_semaphore_bounds_inflight_requests_without_sleep(self):
        active = 0
        maximum = 0
        entered = asyncio.Event()
        release = asyncio.Event()
        async def create(**kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(active, maximum)
            if active == 2:
                entered.set()
            await release.wait()
            active -= 1
            return SimpleNamespace(output_text="{}")
        self.client.responses.create.side_effect = create
        tasks = [asyncio.create_task(self.caller.call(**self.kwargs)) for _ in range(5)]
        await asyncio.wait_for(entered.wait(), timeout=1)
        release.set()
        self.assertEqual(await asyncio.gather(*tasks), [{}] * 5)
        self.assertEqual(maximum, 2)


class ConfigTests(unittest.TestCase):
    def test_resolve_effort_and_concurrency_once(self):
        with patch.object(provider, "load_env") as load, patch.object(provider, "env_or_profile_int", return_value=7), patch.dict(os.environ, {"POWERPACKS_DEEP_CONTEXT_REASONING_EFFORT": " HIGH "}):
            config = provider.OpenAIResponsesConfig.resolve(model="synthetic", effort="medium", timeout=10, max_retries=1)
        load.assert_called_once_with()
        self.assertEqual(config, provider.OpenAIResponsesConfig("synthetic", "high", 7, 10, 1))

    def test_nearest_env_wins_without_overriding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "child"
            child.mkdir()
            (root / ".env").write_text("SYNTHETIC=parent")
            (child / ".env").write_text("SYNTHETIC=child")
            with patch.object(Path, "cwd", return_value=child), patch.object(provider, "load_dotenv") as load:
                provider.load_env()
            load.assert_called_once_with(child / ".env", override=False)

    def test_missing_env_is_noop(self):
        with patch.object(Path, "exists", return_value=False), patch.object(provider, "load_dotenv") as load:
            provider.load_env()
        load.assert_not_called()
