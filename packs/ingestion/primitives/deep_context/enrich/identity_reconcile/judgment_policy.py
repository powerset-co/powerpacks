"""Stored identity verdict reuse policy."""

from __future__ import annotations

import json
from dataclasses import dataclass

from packs.ingestion.primitives.deep_context.db.identity_queries import links
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import (
    IdentityVerdict,
)

# The complete set of answers the judge can give. IdentityVerdict does not
# validate against it — `from_payload` takes whatever string the provider put in
# "verdict", including "" — so anything read back out of the store is checked
# here before it is trusted.
VERDICTS = ("confirmed", "wrong_person", "needs_review")


@dataclass(frozen=True)
class StoredJudgment:
    """A verdict already paid for, with the exact judge input it answered.

    The fingerprint is what makes the verdict reusable rather than merely
    present: it identifies the evidence, prompt, model and effort the judge was
    shown. Equal fingerprint means asking again would ask the same question.
    """

    verdict: IdentityVerdict
    fingerprint: str


def stored_judgments(db: Db) -> dict[str, StoredJudgment]:
    """Every candidate's persisted verdict, keyed by candidate_key.

    Malformed JSON, a non-object payload, or an empty verdict value is skipped
    silently rather than raised — that row simply doesn't appear here, so a
    caller treats it as unjudged (the judge pays for it) instead of acting on a
    verdict nobody can read.
    """
    judgments: dict[str, StoredJudgment] = {}
    for link in links(db):
        try:
            verdict = json.loads(link.judgment_payload_json or "")
        except json.JSONDecodeError:
            continue
        try:
            parsed = IdentityVerdict.from_payload(verdict)
        except (TypeError, ValueError):
            continue
        if parsed.value:
            judgments[link.row_key] = StoredJudgment(parsed, str(link.judgment_fingerprint or ""))
    return judgments


def reuses_stored_verdict(
    stored: StoredJudgment | None,
    current_fingerprint: str,
    *,
    force: bool,
) -> bool:
    """Whether the verdict on file answers exactly this input, so skip paying.

    Three questions, and all three have to be yes:
      1. did the caller demand a fresh judgment (--force)?
      2. is there a verdict on file that means something?
      3. was it produced from this exact input?

    (2) is not paranoia about a shape that cannot occur. A stored verdict is
    whatever a provider once returned, and `IdentityVerdict.from_payload`
    accepts an absent "verdict" key as `value=""`. Reusing one of those would
    pin the row permanently: the fingerprint keeps matching every run, so the
    judge is never asked again and the row never resolves. Unreadable means
    judge, the same rule the rest of this stage follows.

    No check that the stored fingerprint is non-empty is needed — a row that was
    never judged holds "", and "" never equals a sha256 hex digest.
    """
    if force or stored is None:
        return False
    if stored.verdict.value not in VERDICTS:
        return False
    return stored.fingerprint == current_fingerprint
