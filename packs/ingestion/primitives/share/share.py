#!/usr/bin/env python3
"""The share stage: who leaves the laptop.

Joins every `merged/people.csv` row to its deep-context leaves, labels it (a
frozen Jev question set plus deterministic metadata labels), lets the human
override with tags, and derives the share list the upload half reads.

Flow:
  label   evidence.py (people.csv + deep-context leaves -> PersonEvidence)
          -> Jev (spend-gated) -> labels.csv
  tag     name/phone/email or --person-id -> tags.csv -> share.csv
  share   labels.csv + tags.csv + people.csv -> share.csv
  status  manifest.json counts

Changelog:
  2026-09-24: created.
"""

from __future__ import annotations

import argparse
import json
import asyncio
import re
import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import tiktoken

from packs.ingestion.primitives.common.gates import exit_code_for_status, needs_approval_payload
from packs.ingestion.primitives.common.jsonio import now_iso, read_json, write_json
from packs.ingestion.primitives.deep_context.common import DEFAULT_PEOPLE_CSV, emit, load_env, parse_list
from packs.ingestion.primitives.share.evidence import ShareEvidence, cell_text
from packs.ingestion.primitives.share.labels import deterministic_labels, labels_from_answers, private_reason, share_decision
from packs.ingestion.primitives.share.models import (
    DETERMINISTIC_COLUMNS,
    LABEL_COLUMNS,
    LABELS_CSV,
    MANIFEST_JSON,
    SHARE_COLUMNS,
    SHARE_CSV,
    SHARE_DIR,
    TAGS_CSV,
    DeterministicLabels,
    JevLabels,
    LabelRow,
    PersonEvidence,
)
from packs.ingestion.primitives.share.questions import (
    CHOICE_LABELS,
    NOUL_LABELS,
    QUESTION_VERSION,
    REQUEST_VERSION,
    SCORE_LABELS,
    build_request,
)
from packs.ingestion.primitives.share.tags import TAG_VOCABULARY, TagStore, lookup_targets
from packs.search.primitives.llm_rerank_candidates.jev.client import (
    INPUT_PRICE_PER_MILLION,
    MAX_CONCURRENCY,
    answer_requests,
    request_digest,
)
from packs.shared.csv_io import CsvIO

USAGE_STAGE = "share_labels"
TOKEN_ENCODING = "o200k_base"
YES = "yes"

# Exit codes beyond the shared status map: a bad lookup or an unknown tag.
EXIT_BAD_REQUEST = 2

_TAG_TOKEN = re.compile(r"^([+-])(\w+)$")


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return YES if value else ""
    return value


# --- label --------------------------------------------------------------------

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
                facts=person.facts_state(),
                profile=person.profile_state(),
                channels=person.channel_state(),
                owner=owner,
                reference_date=person.evidence_date or "",
            )
            digest = request_digest(request)
            requests[digest] = request
            request_by_person[person.person_id] = digest

        uncached = [digest for digest in requests if not (self.out_dir / "jev" / f"{digest}.json").exists()]
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
                question_version=QUESTION_VERSION,
            )
        )

        updated_at = now_iso()
        rows: list[dict[str, Any]] = []
        counts = {"people": len(people), "jev_called": 0, "cached": 0, "deterministic_only": 0, "private_suggested": 0}
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
            counts["private_suggested"] += int(reason is not None)
            rows.append(_label_row(person, deterministic, jev, reason, updated_at))

        CsvIO.write_dict_rows(self.out_dir / LABELS_CSV.name, list(LABEL_COLUMNS), rows)
        payload = {
            "counts": counts,
            "paid_usage": paid,
            "cached_usage": cached,
            "question_version": QUESTION_VERSION,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "updated_at": updated_at,
        }
        _update_manifest(self.out_dir, "labels", payload)
        return {
            "primitive": "share_labels",
            "status": "completed",
            "labels_csv": str(self.out_dir / LABELS_CSV.name),
            "manifest": str(self.out_dir / MANIFEST_JSON.name),
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
        **{name: _cell(getattr(deterministic, name)) for name in DETERMINISTIC_COLUMNS},
        "private_suggested": YES if reason else "",
        "private_reason": reason or "",
        "updated_at": updated_at,
    }
    if jev is not None:
        row.update({name: jev.choices[name] for name in CHOICE_LABELS})
        row.update({f"{name}_p": f"{jev.choice_p[name]:.3f}" for name in CHOICE_LABELS})
        row.update({name: jev.scores[name] for name in SCORE_LABELS})
        row.update({name: f"{jev.probabilities[name]:.3f}" for name in NOUL_LABELS})
    return row


# --- share --------------------------------------------------------------------

class ShareList:
    """Rebuild share.csv from labels.csv + tags.csv + people.csv. Free, local."""

    def __init__(self, *, out_dir: Path = SHARE_DIR, people_csv: Path = DEFAULT_PEOPLE_CSV) -> None:
        self.out_dir = Path(out_dir)
        self.people_csv = Path(people_csv)

    def run(self) -> dict[str, Any]:
        labels = _load_label_rows(self.out_dir / LABELS_CSV.name)
        people = _people_order(self.people_csv)
        # share.csv is the whole network or nothing: the upload reconciles the
        # cloud to it, so a list missing people (after `label --limit N`) would
        # un-share everyone it omits.
        unlabeled = [person_id for person_id, _ in people if person_id not in labels]
        if unlabeled:
            return {
                "primitive": "share_list",
                "status": "failed",
                "error": f"{len(unlabeled)} of {len(people)} people have no label row; run `label` for everyone first",
            }
        tags = TagStore(self.out_dir / TAGS_CSV.name).load()
        updated_at = now_iso()
        rows: list[dict[str, Any]] = []
        reasons: dict[str, int] = {}
        counts = {"share_yes": 0, "share_no": 0}
        for person_id, superseded in people:
            # A tag set before a merge is keyed by the id that merged away; the
            # surviving row is the only row that can still carry that decision.
            held = tags.get(person_id) or next((tags[old] for old in superseded if old in tags), None)
            decision = share_decision(labels[person_id], held)
            counts["share_yes" if decision.share == YES else "share_no"] += 1
            reasons[decision.reason] = reasons.get(decision.reason, 0) + 1
            rows.append(
                {
                    "person_id": decision.person_id,
                    "public_identifier": decision.public_identifier or "",
                    "share": decision.share,
                    "reason": decision.reason,
                    "labels": "|".join(decision.labels),
                    "source": decision.source,
                    "updated_at": updated_at,
                }
            )
        CsvIO.write_dict_rows(self.out_dir / SHARE_CSV.name, list(SHARE_COLUMNS), rows)
        payload = {"counts": {**counts, "by_reason": reasons}, "updated_at": updated_at}
        _update_manifest(self.out_dir, "share", payload)
        return {
            "primitive": "share_list",
            "status": "completed",
            "share_csv": str(self.out_dir / SHARE_CSV.name),
            "manifest": str(self.out_dir / MANIFEST_JSON.name),
            **payload,
        }


def _people_order(people_csv: Path) -> list[tuple[str, tuple[str, ...]]]:
    return [
        (str(row.get("id") or "").strip(), tuple(parse_list(row.get("superseded_person_ids"))))
        for row in CsvIO.read_dict_rows(people_csv)
        if str(row.get("id") or "").strip()
    ]


def _load_label_rows(path: Path) -> dict[str, LabelRow]:
    rows: dict[str, LabelRow] = {}
    for row in CsvIO.read_dict_rows_normalized(path):
        person_id = row["person_id"].strip()
        if not person_id:
            continue
        rows[person_id] = LabelRow(
            person_id=person_id,
            public_identifier=cell_text(row.get("public_identifier")),
            linkedin_only=row.get("linkedin_only") == YES,
            is_owner=row.get("is_owner") == YES,
            private_suggested=row.get("private_suggested") == YES,
            private_reason=cell_text(row.get("private_reason")),
            probabilities={name: float(row[name]) for name in NOUL_LABELS if row.get(name)},
        )
    return rows


def _update_manifest(out_dir: Path, key: str, payload: dict[str, Any]) -> None:
    """One manifest, one writer per key — `label` owns `labels`, `share` owns `share`."""
    path = out_dir / MANIFEST_JSON.name
    manifest = read_json(path, {}) or {}
    manifest[key] = payload
    manifest["updated_at"] = payload["updated_at"]
    write_json(path, manifest)


# --- CLI ----------------------------------------------------------------------

def _split_tag_args(argv: list[str]) -> tuple[list[str], set[str], set[str], set[str]]:
    """Pull `+tag` / `-tag` words out of argv before argparse sees them — argparse
    cannot tell `-friend` from an option. A `+word` outside the vocabulary is
    reported; a `-word` outside it stays an argparse error."""
    rest: list[str] = []
    add: set[str] = set()
    remove: set[str] = set()
    unknown: set[str] = set()
    for token in argv:
        match = _TAG_TOKEN.fullmatch(token)
        if match and match[2] in TAG_VOCABULARY:
            (add if match[1] == "+" else remove).add(match[2])
        elif token.startswith("+"):
            unknown.add(token[1:])
        else:
            rest.append(token)
    return rest, add, remove, unknown


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Share stage: machine labels, human tags, the share list.")
    sub = parser.add_subparsers(dest="command", required=True)

    label = sub.add_parser("label", help="build labels.csv (Jev; spend-gated)")
    label.add_argument("--estimate", action="store_true", help="print the cost and write nothing")
    label.add_argument("--approve-spend", action="store_true")
    label.add_argument("--limit", type=int, default=0)

    tag = sub.add_parser("tag", help="set human tags on one person: tag --name X +private -friend")
    tag.add_argument("--person-id", default="")
    tag.add_argument("--name", default="")
    tag.add_argument("--phone", default="")
    tag.add_argument("--email", default="")
    tag.add_argument("--note", default=None)

    sub.add_parser("share", help="rebuild share.csv from labels.csv + tags.csv")
    sub.add_parser("status", help="print the stage manifest")
    return parser


def main(argv: list[str] | None = None) -> int:
    rest, add, remove, unknown = _split_tag_args(list(sys.argv[1:] if argv is None else argv))
    args = build_parser().parse_args(rest)

    if args.command == "label":
        payload = ShareLabels(
            estimate_only=args.estimate, approve_spend=args.approve_spend, limit=args.limit
        ).run()
        emit(payload)
        return exit_code_for_status(payload["status"])

    if args.command == "tag":
        if unknown:
            print(f"unknown tags: {', '.join(sorted(unknown))}", file=sys.stderr)
            print(f"vocabulary: {', '.join(sorted(TAG_VOCABULARY))}", file=sys.stderr)
            return EXIT_BAD_REQUEST
        person_id = args.person_id.strip()
        if not person_id:
            targets = lookup_targets(name=args.name, phone=args.phone, email=args.email)
            if len(targets) != 1:
                for target in targets:
                    print(f"- {target.name} [{target.slug}] {target.person_id}", file=sys.stderr)
                print(f"{len(targets)} matches; pass --person-id.", file=sys.stderr)
                return EXIT_BAD_REQUEST
            person_id = targets[0].person_id
        tags = TagStore().apply(person_id, add=add, remove=remove, note=args.note)
        rebuilt = ShareList().run()
        if rebuilt["status"] != "completed":
            emit(rebuilt)
            return exit_code_for_status(rebuilt["status"])
        row = next(
            (row for row in CsvIO.read_dict_rows_normalized(SHARE_CSV) if row["person_id"] == person_id),
            None,
        )
        emit(
            {
                "primitive": "share_tag",
                "status": "completed",
                "person_id": person_id,
                "tags": sorted(tags.tags),
                "note": tags.note,
                "share": row,
            }
        )
        return 0

    if args.command == "share":
        payload = ShareList().run()
        emit(payload)
        return exit_code_for_status(payload["status"])

    emit({"primitive": "share_status", "status": "completed", "manifest": read_json(MANIFEST_JSON, {})})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
