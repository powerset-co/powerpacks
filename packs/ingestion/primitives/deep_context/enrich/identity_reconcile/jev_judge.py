"""Two cached JEV evidence views accept matches or send them to comparison.

The frozen logistic model and both evidence views must support acceptance.
Disagreement escalates, never automatically rejects a contact.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os

import httpx
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Mapping, Sequence

from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import (
    IdentityJudgeResult, IdentityTask, IdentityUsage, IdentityVerdict,
)
from packs.ingestion.schemas.people_schema import normalize_linkedin_url
from packs.ingestion.primitives.deep_context.shared.common import load_env
from packs.ingestion.primitives.imports.common import write_manifest
from packs.search.primitives.llm_rerank_candidates.jev.client import answer_requests, request_digest, TIMEOUT_SECONDS
from packs.search.primitives.llm_rerank_candidates.jev.model import MODEL_ID

_MODEL_BYTES = Path(__file__).with_name('jev_model.json').read_bytes()


@dataclass(frozen=True)
class _Calibration:
    features: tuple[str, ...]
    mean: tuple[float, ...]
    scale: tuple[float, ...]
    coefficients: tuple[float, ...]
    intercept: float
    threshold: float

    @classmethod
    def from_payload(cls, raw: Mapping) -> _Calibration:
        return cls(tuple(raw['features']), tuple(raw['mean']), tuple(raw['scale']),
                   tuple(raw['coefficients']), float(raw['intercept']), float(raw['threshold']))


@dataclass(frozen=True)
class _QuestionSet:
    policy: str
    questions: dict


_MODEL = _Calibration.from_payload(json.loads(_MODEL_BYTES))
_QUESTIONS = {name: _QuestionSet(**value) for name, value in
              json.loads(Path(__file__).with_name('jev_questions.json').read_text()).items()}
_MODEL_DIGEST = hashlib.sha256(_MODEL_BYTES).hexdigest()
_CHUNK_SIZE = 128
_CONCURRENCY = 16


def _probability(network: Mapping, association: Mapping) -> float:
    """Parse the validated provider answers in the frozen model's feature order."""
    values = []
    for feature in _MODEL.features:
        source, question, *choice = feature.split(':')
        answer = (network if source == 'network' else association)[question]
        values.append(float(answer['probabilities'][choice[0]] if choice else answer['noul']))
    score = _MODEL.intercept + sum(
        (value - mean) / scale * coefficient
        for value, mean, scale, coefficient in zip(
            values, _MODEL.mean, _MODEL.scale, _MODEL.coefficients, strict=True,
        )
    )
    return 1 / (1 + math.exp(-score))


def _classify(probability: float, network: Mapping, association: Mapping) -> tuple[str, str]:
    """Accept only when the model and both provider views support the match."""
    identity = network['identity']['probabilities']
    attachment = association['association']['probabilities']
    if max(identity, key=identity.get) != 'confirmed' or max(attachment, key=attachment.get) != 'yes':
        return 'needs_review', 'JEV evidence views do not jointly support acceptance; compare the saved evidence.'
    if probability < _MODEL.threshold:
        return 'needs_review', 'The combined identity model does not support acceptance; compare the saved evidence.'
    return 'confirmed', 'Both identity evidence views and the combined model support this match.'


def _requests(task: IdentityTask, known_urls: tuple[str, ...], reference_date: str) -> dict[str, dict]:
    known = sorted({normalize_linkedin_url(url) for url in known_urls})
    context = {
        'proposed_url_in_imported_linkedin_record': normalize_linkedin_url(task.linkedin.linkedin_url) in known,
        'same_parent_imported_urls': known,
        'provenance': 'Merged imported_people row with linkedin_csv source; original CSV URL not independently verified.',
    }
    return {name: {
        'model': MODEL_ID,
        'state': {
            'dossier': json.dumps({'contact': asdict(task.evidence), 'network_context': context}, ensure_ascii=False),
            'profile': task.linkedin.as_judge_dict(), 'facts': {}, 'channels': {}, 'owner': {},
            'reference_date': reference_date, 'evidence_policy': questions.policy,
        },
        'questions': questions.questions,
    } for name, questions in _QUESTIONS.items()}


def judgment_fingerprint(task: IdentityTask, known_urls: tuple[str, ...], reference_date: str) -> str:
    """The frozen model and exact provider requests, shared with verdict reuse."""
    return hashlib.sha256(json.dumps({
        'model': _MODEL_DIGEST,
        'requests': {name: request_digest(request) for name, request in _requests(task, known_urls, reference_date).items()},
    }, sort_keys=True).encode()).hexdigest()


def judge_batch(
    tasks: Sequence[IdentityTask], *, imported_urls: Sequence[tuple[str, ...]],
    output_dir: Path, on_done: Callable[[int, int], None] | None = None,
    reference_date: str | None = None,
) -> list[IdentityJudgeResult]:
    """Bound pending requests in memory; the shared client persists each answer."""
    load_env()
    day = reference_date or date.today().isoformat()

    async def run() -> list[IdentityJudgeResult]:
        results = []
        semaphore = asyncio.Semaphore(_CONCURRENCY)
        client = httpx.AsyncClient(timeout=TIMEOUT_SECONDS) if os.environ.get('TYPESAFE_API_KEY') else None

        async def answer(name: str, digest: str, request: dict):
            async with semaphore:
                try:
                    replies = await answer_requests(
                        {digest: request}, output_dir=output_dir / name, api_key=None, client=client,
                        concurrency=1, request_version='identity-association-v1',
                        question_version='identity-association-v1',
                    )
                    return replies[digest]
                except Exception as exc:
                    return exc

        try:
            for start in range(0, len(tasks), _CHUNK_SIZE):
                batch = [
                    _requests(task, urls, day)
                    for task, urls in zip(tasks[start:start + _CHUNK_SIZE],
                                          imported_urls[start:start + _CHUNK_SIZE], strict=True)
                ]
                replies = {}
                for name in _QUESTIONS:
                    requests = {request_digest(pair[name]): pair[name] for pair in batch}
                    rows = await asyncio.gather(*(answer(name, digest, request)
                                                 for digest, request in requests.items()))
                    replies[name] = dict(zip(requests, rows, strict=True))
                for index, pair in enumerate(batch):
                    answers = {name: replies[name][request_digest(request)] for name, request in pair.items()}
                    errors = [f'{name}: {reply}' for name, reply in answers.items() if isinstance(reply, Exception)]
                    if errors:
                        usage = IdentityUsage(input_tokens=sum(
                            reply.response['usage']['input_tokens'] for reply in answers.values()
                            if not isinstance(reply, Exception) and not reply.cached
                        ))
                        results.append(IdentityJudgeResult(None, usage, '; '.join(errors), ''))
                        if on_done:
                            on_done(len(results), len(tasks))
                        continue
                    probability = _probability(answers['network'].response['answers'],
                                               answers['association'].response['answers'])
                    classification, reason = _classify(probability,
                        answers['network'].response['answers'], answers['association'].response['answers'])
                    verdict = IdentityVerdict.from_payload({
                        'verdict': classification, 'confidence': probability, 'reason': reason,
                        'judge': MODEL_ID, 'match_probability': probability,
                        'answers': {name: reply.response['answers'] for name, reply in answers.items()},
                    })
                    fingerprint = judgment_fingerprint(tasks[start + index], imported_urls[start + index], day)
                    usage = IdentityUsage(input_tokens=sum(
                        reply.response['usage']['input_tokens'] for reply in answers.values() if not reply.cached
                    ))
                    results.append(IdentityJudgeResult(verdict, usage, '', fingerprint))
                    if on_done:
                        on_done(len(results), len(tasks))
        finally:
            if client is not None:
                await client.aclose()
        errors = [{'linkedin_url': task.linkedin.linkedin_url, 'error': result.error}
                  for task, result in zip(tasks, results, strict=True) if result.error]
        write_manifest(output_dir.name, {
            'status': 'partial' if errors else 'completed',
            'counts': {'judged': len(results) - len(errors), 'errors': len(errors)},
            'errors': errors,
        }, import_dir=output_dir.parent)
        return results

    return asyncio.run(run())
