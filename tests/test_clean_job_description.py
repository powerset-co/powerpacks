from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from packs.search.primitives.clean_job_description import clean_job_description
from packs.search.primitives.llm_rerank_candidates import llm_rerank_candidates as reranker


def _answer(**changes):
    answer = {
        "company": {
            "what_it_does": "Example Systems builds storage control software.",
            "stage": None,
            "size": "42 employees",
            "source_quotes": {
                "what_it_does": ["Example Systems builds storage control software."],
                "stage": [],
                "size": ["We are a team of 42 employees."],
            },
        },
        "responsibilities": [{
            "text": "Build storage control software.",
            "source_quote": "Build storage control software.",
        }],
        "experience": [{
            "text": "Experience with distributed systems or databases.",
            "source_quote": "Experience with distributed systems or databases.",
        }],
        "nice_to_have": [],
    }
    answer.update(changes)
    return answer


def _response(answer=None, *, content=None, finish_reason="stop", refusal=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason=finish_reason,
            message=SimpleNamespace(
                refusal=refusal,
                content=content if content is not None else json.dumps(answer or _answer()),
            ),
        )],
        model="gpt-5.6-sol",
        service_tier="flex",
        usage=SimpleNamespace(model_dump=lambda: {"prompt_tokens": 500, "completion_tokens": 200}),
    )


def _client(*responses):
    client = mock.Mock()
    client.chat.completions.create.side_effect = list(responses)
    return client


class CleanJobDescriptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output_dir = Path(self.temp.name)
        self.jd = (
            "Example Systems builds storage control software.\n"
            "We are a team of 42 employees.\n"
            "Responsibilities\nBuild storage control software.\n"
            "Experience\nExperience with distributed systems or databases.\n"
            "Benefits\nFree lunch and health insurance."
        )

    def _clean(self, api, **changes):
        kwargs = {
            "jd": self.jd,
            "title": "Storage Engineer",
            "company_name": "Example Systems",
            "output_dir": self.output_dir,
            "client": api,
        }
        kwargs.update(changes)
        return clean_job_description.clean_job_description(**kwargs)

    def test_renders_reviewed_structure_and_marks_unknowns(self):
        api = _client(_response())

        text = self._clean(api)

        self.assertEqual(text, (
            "Title: Storage Engineer\n"
            "Hiring company: Example Systems · What it does: Example Systems builds storage "
            "control software. · Stage: Not stated · Size: 42 employees\n\n"
            "Responsibilities\n- Build storage control software.\n\n"
            "Experience\n- Experience with distributed systems or databases.\n\n"
            "Nice to have\nNot stated.\n"
        ))
        request = api.chat.completions.create.call_args.kwargs
        self.assertEqual(request["model"], "gpt-5.6-sol")
        self.assertEqual(request["reasoning_effort"], "high")
        self.assertEqual(request["service_tier"], "flex")
        self.assertEqual(request["max_completion_tokens"], 8192)
        self.assertIs(request["store"], False)
        self.assertTrue(request["response_format"]["json_schema"]["strict"])
        schema = json.dumps(request["response_format"]["json_schema"]["schema"])
        self.assertNotIn("tech_stack", schema)

    def test_exact_request_cache_avoids_another_call_or_credentials(self):
        api = _client(_response())
        first = self._clean(api)

        with mock.patch.object(clean_job_description, "make_openai_client") as factory:
            second = self._clean(None)

        self.assertEqual(second, first)
        factory.assert_not_called()
        self.assertEqual(api.chat.completions.create.call_count, 1)
        saved = json.loads(next(self.output_dir.glob("*.json")).read_text())
        self.assertEqual(saved["model"], "gpt-5.6-sol")
        self.assertEqual(saved["service_tier"], "flex")
        self.assertEqual(saved["usage"]["prompt_tokens"], 500)
        self.assertEqual(len(saved["request_sha256"]), 64)
        self.assertEqual(next(self.output_dir.glob("*.txt")).read_text(), first)

    def test_missing_title_and_company_are_preserved_as_unknown(self):
        text = self._clean(_client(_response()), title="", company_name="")

        self.assertTrue(text.startswith(
            "Title: Not stated\nHiring company: Not stated · What it does:"))

    def test_checkpoint_is_atomic(self):
        api = _client(_response())
        with mock.patch.object(clean_job_description.os, "replace", wraps=os.replace) as replace:
            self._clean(api)

        replace.assert_called_once()
        self.assertFalse(any(path.name.startswith(".") for path in self.output_dir.iterdir()))

    def test_cache_changes_with_source_title_company_prompt_and_model(self):
        api = _client(*[_response() for _ in range(5)])
        self._clean(api)
        self._clean(api, jd=self.jd + "\nAnother duty.")
        self._clean(api, title="Principal Storage Engineer")
        self._clean(api, company_name="Example Storage")
        with mock.patch.object(clean_job_description, "MODEL", "other-model"):
            self._clean(api)

        self.assertEqual(api.chat.completions.create.call_count, 5)
        self.assertEqual(len(list(self.output_dir.glob("*.json"))), 5)

    def test_source_quotes_are_required_and_must_match_exactly(self):
        cases = [
            _answer(responsibilities=[{"text": "Invented duty", "source_quote": "Invented duty"}]),
            _answer(experience=[{
                "text": "[must] Experience with distributed systems.",
                "source_quote": "Experience with distributed systems or databases.",
            }]),
            _answer(company={
                **_answer()["company"], "stage": "Series B",
                "source_quotes": {**_answer()["company"]["source_quotes"], "stage": []},
            }),
            _answer(tech_stack=["Rust"]),
        ]
        for index, answer in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(RuntimeError):
                self._clean(_client(_response(answer)), output_dir=self.output_dir / str(index))

    def test_malformed_paid_response_is_checkpointed_and_not_retried(self):
        output_dir = self.output_dir / "malformed"
        api = _client(_response(content="not json"), _response())
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "malformed JSON"):
                self._clean(api, output_dir=output_dir)

        self.assertEqual(api.chat.completions.create.call_count, 1)
        self.assertEqual(json.loads(next(output_dir.glob("*.json")).read_text())["content"], "not json")

    def test_refusal_and_incomplete_response_never_fall_back_to_raw_jd(self):
        for name, response in (
            ("refusal", _response(refusal="No", content="")),
            ("length", _response(finish_reason="length")),
            ("empty", SimpleNamespace(choices=[], model="gpt-5.6-sol",
                                      service_tier="flex", usage=None)),
        ):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                self._clean(_client(response), output_dir=self.output_dir / name)

    def test_owned_client_uses_shared_usage_capture_without_retries(self):
        api = _client(_response())
        api.close = mock.Mock()
        with mock.patch.object(clean_job_description, "make_openai_client", return_value=api) as factory:
            self._clean(None, api_key="synthetic-test-key")

        factory.assert_called_once_with(api_key="synthetic-test-key", timeout=600, max_retries=0)
        api.close.assert_called_once_with()

    def test_paid_response_is_checkpointed_before_owned_client_closes(self):
        api = _client(_response())
        api.close = mock.Mock(side_effect=RuntimeError("close failed"))
        with mock.patch.object(clean_job_description, "make_openai_client", return_value=api):
            with self.assertRaisesRegex(RuntimeError, "close failed"):
                self._clean(None, api_key="synthetic-test-key")

        self.assertEqual(len(list(self.output_dir.glob("*.json"))), 1)
        self.assertEqual(self._clean(None), self._clean(None))
        self.assertEqual(api.chat.completions.create.call_count, 1)

    def test_prompt_keeps_technical_qualifications_without_inventing_tiers(self):
        prompt = clean_job_description._system_prompt()

        for text in (
            "No standalone tech-stack", "Do not infer", "routine Git familiarity",
            "WHAT YOU ARE NOT", "compensation", "hiring-location", "source_quote",
        ):
            self.assertIn(text, prompt)
        self.assertNotIn("[must]", prompt.casefold())
        self.assertNotIn("[gate]", prompt.casefold())

    def test_rerank_boundary_cleans_once_and_reuses_exact_request(self):
        cleaner_api = _client(_response())
        native = {"status": "ok", "scores": [{
            "id": "synthetic", "score": 4, "evidence": "Relevant work", "basis": "direct",
        }]}
        item = reranker.RerankItem(position=0, payload={"person_id": "synthetic"})
        with mock.patch.object(clean_job_description, "make_openai_client",
                               return_value=cleaner_api) as factory, \
                mock.patch.object(reranker.terra, "score_candidates",
                                  new_callable=mock.AsyncMock, return_value=native) as scorer:
            for _ in range(2):
                asyncio.run(reranker._rerank_with_terra(
                    [item], jd=self.jd, title="Storage Engineer", company_name="Example Systems",
                    evaluation_query="Prefer recovery work", as_of="2026-09-17",
                    output_dir=self.output_dir / "capability",
                    cleaner_output_dir=self.output_dir / "structured-jd",
                    api_key="synthetic-test-key", concurrency=1))

        factory.assert_called_once()
        self.assertEqual(cleaner_api.chat.completions.create.call_count, 1)
        self.assertEqual(scorer.await_count, 2)
        self.assertEqual(scorer.call_args.kwargs["jd"], (
            "Title: Storage Engineer\n"
            "Hiring company: Example Systems · What it does: Example Systems builds storage "
            "control software. · Stage: Not stated · Size: 42 employees\n\n"
            "Responsibilities\n- Build storage control software.\n\n"
            "Experience\n- Experience with distributed systems or databases.\n\n"
            "Nice to have\nNot stated.\n\n"
            "User-reviewed criteria:\nPrefer recovery work"
        ))


if __name__ == "__main__":
    unittest.main()
