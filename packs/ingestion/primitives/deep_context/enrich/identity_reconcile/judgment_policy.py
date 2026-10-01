"""Stored identity verdict reuse policy."""

from __future__ import annotations

from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import StoredJudgment

# The complete set of answers the judge can give. IdentityVerdict does not
# validate against it — `from_payload` takes whatever string the provider put in
# "verdict", including "" — so anything read back out of the store is checked
# here before it is trusted.
VERDICTS = ("confirmed", "wrong_person", "needs_review")


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
