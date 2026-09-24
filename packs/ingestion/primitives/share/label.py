"""Build machine labels for each person with deterministic and Jev evidence.

Flow: load evidence -> estimate and gate Jev -> write labels.csv and manifest.

Changelog:
  2026-09-24: created from the share CLI.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import tiktoken

from packs.ingestion.primitives.common.gates import needs_approval_payload
from packs.ingestion.primitives.common.jsonio import now_iso, read_json, write_json
from packs.ingestion.primitives.deep_context.common import load_env
from packs.ingestion.primitives.share.csv_cells import cell_value
from packs.ingestion.primitives.share.evidence import ShareEvidence
from packs.ingestion.primitives.share.labels import deterministic_labels, labels_from_answers, private_reason
from packs.ingestion.primitives.share.models import (
    DETERMINISTIC_COLUMNS, LABEL_COLUMNS, LABELS_FILENAME, MANIFEST_FILENAME, SHARE_DIR,
    DeterministicLabels, JevLabels, PersonEvidence,
)
from packs.ingestion.primitives.share.questions import (
    CHOICE_LABELS, NOUL_LABELS, REQUEST_VERSION, SCORE_LABELS, build_request,
    channel_state, facts_state, profile_state,
)
from packs.ingestion.schemas.share_schema import PRIVATE_SUGGESTED
from packs.search.primitives.llm_rerank_candidates.jev.client import (
    INPUT_PRICE_PER_MILLION, MAX_CONCURRENCY, answer_requests, cache_path, request_digest,
)
from packs.shared.csv_io import CsvIO

USAGE_STAGE = "share_labels"
TOKEN_ENCODING = "o200k_base"


def update_manifest(out_dir: Path, key: str, payload: dict[str, Any]) -> None:
    """Keep label and share counts in one manifest."""
    path = out_dir / MANIFEST_FILENAME
    manifest = read_json(path, {}) or {}
    manifest[key] = payload
    manifest["updated_at"] = payload["updated_at"]
    write_json(path, manifest)


class ShareLabels:
    """Build labels.csv for every people.csv row. Jev is spend-gated."""

    def __init__(
        self,
        *,
        estimate_only: bool = False,
        approve_spend: bool = False,
        limit: int = 0,
        out_dir: Path = SHARE_DIR,
        evidence: ShareEvidence | None = None,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.estimate_only = estimate_only
        self.approve_spend = approve_spend
        self.limit = limit
        self.out_dir = Path(out_dir)
        self.labels_csv = self.out_dir / LABELS_FILENAME
        self.evidence = evidence or ShareEvidence()
        self.api_key = api_key
        self.client = client
        self.reference_date = date.today().isoformat()

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        people = self.evidence.load(limit=self.limit)
        owner = self.evidence.owner_state()

        requests: dict[str, dict] = {}
        request_by_person: dict[str, str] = {}
        for person in people:
            if person.linkedin_only:
                continue
            request = build_request(
                dossier=person.dossier,
                facts=facts_state(person),
                profile=profile_state(person),
                channels=channel_state(person),
                owner=owner,
                reference_date=person.evidence_date,
            )
            digest = request_digest(request)
            requests[digest] = request
            request_by_person[person.person_id] = digest

        uncached = [digest for digest in requests if not cache_path(self.out_dir, digest).exists()]
        estimate = self._estimate(requests, uncached)
        print(
            f"[share] {len(people)} people, {len(requests)} with context, {len(uncached)} uncached, "
            f"~${estimate['cost_usd']:.4f}",
            file=sys.stderr,
        )
        if self.estimate_only:
            return {"primitive": "share_labels", "status": "completed", "mode": "estimate", "estimate": estimate}
        if uncached and not self.approve_spend:
            return {
                "primitive": "share_labels",
                "status": "needs_approval",
                "estimate": estimate,
                "needs_approval": needs_approval_payload(
                    step="label",
                    provider="typesafe",
                    estimated_calls=len(uncached),
                    message=f"Labelling {len(uncached)} people costs about ${estimate['cost_usd']:.4f}.",
                    continue_command="bin/deep-context label --approve-spend",
                ),
            }

        load_env()
        os.environ["POWERPACKS_USAGE_STAGE"] = USAGE_STAGE
        answered = asyncio.run(
            answer_requests(
                requests,
                output_dir=self.out_dir,
                api_key=self.api_key,
                client=self.client,
                concurrency=MAX_CONCURRENCY,
                request_version=REQUEST_VERSION,
                question_version=REQUEST_VERSION,
            )
        )

        updated_at = now_iso()
        rows: list[dict[str, Any]] = []
        counts = {"people": len(people), "jev_called": 0, "cached": 0, "deterministic_only": 0, PRIVATE_SUGGESTED: 0}
        paid = {"pairs": 0, "input_tokens": 0, "output_tokens": 0}
        cached = {"pairs": 0, "input_tokens": 0, "output_tokens": 0}
        for person in people:
            digest = request_by_person.get(person.person_id, "")
            answer = answered[digest] if digest else None
            jev = labels_from_answers(answer.response["answers"]) if answer else None
            if answer is None:
                counts["deterministic_only"] += 1
            else:
                bucket = cached if answer.cached else paid
                counts["cached" if answer.cached else "jev_called"] += 1
                bucket["pairs"] += 1
                bucket["input_tokens"] += answer.response["usage"]["input_tokens"]
                bucket["output_tokens"] += answer.response["usage"]["output_tokens"]
            deterministic = deterministic_labels(person, reference_date=self.reference_date)
            reason = private_reason(deterministic, jev)
            counts[PRIVATE_SUGGESTED] += int(reason is not None)
            rows.append(_label_row(person, deterministic, jev, reason, updated_at))

        CsvIO.write_dict_rows(self.labels_csv, list(LABEL_COLUMNS), rows)
        payload = {
            "counts": counts,
            "paid_usage": paid,
            "cached_usage": cached,
            "question_version": REQUEST_VERSION,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "updated_at": updated_at,
        }
        update_manifest(self.out_dir, "labels", payload)
        return {
            "primitive": "share_labels",
            "status": "completed",
            "labels_csv": str(self.labels_csv),
            "manifest": str(self.out_dir / MANIFEST_FILENAME),
            **payload,
        }

    def _estimate(self, requests: dict[str, dict], uncached: list[str]) -> dict[str, Any]:
        encoder = tiktoken.get_encoding(TOKEN_ENCODING)
        tokens = sum(
            len(encoder.encode(json.dumps(requests[digest], ensure_ascii=False, sort_keys=True)))
            for digest in uncached
        )
        return {
            "people_with_context": len(requests),
            "uncached_calls": len(uncached),
            "input_tokens": tokens,
            "cost_usd": tokens * INPUT_PRICE_PER_MILLION / 1_000_000,
        }


def _label_row(
    person: PersonEvidence,
    deterministic: DeterministicLabels,
    jev: JevLabels | None,
    reason: str | None,
    updated_at: str,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "person_id": person.person_id,
        "public_identifier": person.public_identifier or "",
        "full_name": person.full_name,
        **{name: cell_value(getattr(deterministic, name)) for name in DETERMINISTIC_COLUMNS},
        PRIVATE_SUGGESTED: cell_value(reason is not None),
        "private_reason": reason or "",
        "updated_at": updated_at,
    }
    if jev is not None:
        row.update({name: jev.choices[name] for name in CHOICE_LABELS})
        row.update({f"{name}_p": f"{jev.choice_p[name]:.3f}" for name in CHOICE_LABELS})
        row.update({name: jev.scores[name] for name in SCORE_LABELS})
        row.update({name: f"{jev.probabilities[name]:.3f}" for name in NOUL_LABELS})
    return row
