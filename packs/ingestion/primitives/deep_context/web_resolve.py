"""[tend, $0-metered] Find correct LinkedIns for detaches via websearch agents.

The free sibling of ``reconcile_deep_research``: the SAME research queue
(high-confidence wrong_person detaches, plus opted-in candidates), but instead
of submitting to paid Parallel.ai, each person is handed to a local ``codex
exec`` subprocess with the web-search tool enabled (ChatGPT-subscription auth,
no per-token spend; a Claude-CLI variant runs through ``--command-template``).
The agent searches on bio signals (employers, schools, locations from the
dossier facts — not just the name), and returns a strict-JSON research profile,
instructed to say ``not_found`` rather than guess: a wrong LinkedIn is worse
than none.

Two modes, deliberately separate so the $0 pass never leaves half-judged
state behind:

  research (default, $0): eligible_subset/candidate_subset -> build_queue
    (same bio + owner-context block the paid path sends) -> skip handles
    already resolved on disk (found is final; not_found retries after
    --retry-days) -> bounded codex fan-out -> per-person profile JSON.
    Nothing touches review.csv.
  --propose (spends OpenAI, ~cents per find): the finds on disk go through
    ``propose_retargets_from_output`` with the REAL identity judge — the same
    judged, fingerprint-cached, sticky upsert the Parallel path uses; the
    offline stub is never used here because its can't-confirm verdict would
    stamp the evidence fingerprint and permanently block the real judge.
    Proposed people then drop out of the paid Parallel queue automatically
    (eligible_subset skips live retargets), so the $-gated escalation only
    ever sees the tail this pass could not crack.

Outputs (fixed dir):
  .powerpacks/deep-context/reconcile/web-resolve/<handle>/01_research_web.json
  .powerpacks/deep-context/reconcile/web-resolve/manifest.json

Changelog:
  2026-08-02: new primitive for the `bin/deep-context tend` weekly self-heal
    pass. Engine spawn shape follows deep_search/codex_judge.py; prompt ports
    the resolve-linkedin bio-first method and the identity reviewer's
    "uncertain over wrong" rule from the pre-powerpacks research pipeline.
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.common import (
    DEFAULT_PEOPLE_CSV,
    emit,
    FACTS_DIR,
    FACTS_TEMPLATE,
    INDEX_JSON,
    LINKEDIN_OVERRIDES_CSV,
    load_owner,
    owner_background_block,
    OWNER_JSON,
    RAW_BUNDLE_TEMPLATE,
    RAW_DIR,
    read_jsonl,
    RECONCILE_DIR,
    VERDICTS_JSONL,
)
from packs.ingestion.primitives.deep_context.reconcile_deep_research import (
    build_queue,
    candidate_subset,
    eligible_subset,
    load_people_rows,
    propose_retargets_from_output,
)
from packs.ingestion.primitives.deep_context.reconcile_linkedin import (
    load_override_rows,
)
from packs.ingestion.primitives.deep_context.review_store import (
    RESEARCH_CONFIRM_THRESHOLD,
)
from packs.ingestion.primitives.pipeline.contract import Artifact, Node, StageManifest

WR_OUT_DIR = RECONCILE_DIR / "web-resolve"
WR_MANIFEST = WR_OUT_DIR / "manifest.json"
WEB_PROFILE_NAME = "01_research_web.json"
WEB_PROFILE_TEMPLATE = str(WR_OUT_DIR / "{handle}" / WEB_PROFILE_NAME)

DEFAULT_CONCURRENCY = 4
DEFAULT_TIMEOUT = 300
DEFAULT_RETRY_DAYS = 30
DEFAULT_EFFORT = "medium"

# The agent's whole contract. Bio signals over bare names (the queue bio is the
# dossier-facts digest, the strongest disambiguator we hold), and the reviewer
# rule: not_found beats a guess. `research_notes` must confess an unverified
# email/phone linkage in words — reconcile_deep_research._research_unverified
# reads that confession, and the retarget judge treats it as a soft strike.
INSTRUCTIONS = """\
You are resolving the identity of ONE contact from a private mailbox network.
Use your web search tool. Your job: find this person's correct LinkedIn
profile URL, or conclude that you cannot confidently identify one.

Method — search on bio signals, not just the name:
- Combine the name with employers, schools, projects, locations, and era from
  the bio. Try several combinations. A common name alone is never enough.
- Open the best candidate profiles and check their work history and education
  against the bio facts. Corroboration must go beyond the name: at least one
  independent match (employer, school, location + field, or a linked handle).
- If a previously-attached LinkedIn is named as WRONG below, never propose it.

Decisiveness rule: if you are not confident, return status "not_found" rather
than guessing. A wrong LinkedIn attached to a person is worse than none.
Confidence above 0.85 requires non-name corroboration you actually verified.
If you could not tie the person's email or phone to the profile, say so
explicitly in research_notes (e.g. "could not verify the email linkage").

Reply with ONLY this JSON object, no prose before or after:
{
  "status": "found" | "not_found",
  "linkedin_url": "https://www.linkedin.com/in/... or empty",
  "person": {"real_name": "", "confidence": 0.0, "notes": ""},
  "research_notes": "how you identified them / why you could not",
  "evidence_urls": ["..."]
}
"""

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["found", "not_found"]},
        "linkedin_url": {"type": "string"},
        "person": {
            "type": "object",
            "properties": {
                "real_name": {"type": "string"},
                "confidence": {"type": "number"},
                "notes": {"type": "string"},
            },
            "required": ["real_name", "confidence", "notes"],
            "additionalProperties": False,
        },
        "research_notes": {"type": "string"},
        "evidence_urls": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "linkedin_url", "person", "research_notes", "evidence_urls"],
    "additionalProperties": False,
}


def render_prompt(row: dict[str, str]) -> str:
    """One queue row -> the full agent prompt (instructions + the person block)."""
    lines = [INSTRUCTIONS, "", "THE CONTACT:", f"Display name: {row.get('display_name', '')}"]
    if row.get("primary_email"):
        lines.append(f"Email: {row['primary_email']}")
    if row.get("phone_e164"):
        lines.append(f"Phone: {row['phone_e164']}")
    if row.get("retarget_hint"):
        lines.append(f"Situation: {row['retarget_hint']}")
    if row.get("bio"):
        lines += ["", "BIO (synthesized from correspondence):", row["bio"]]
    if row.get("known_info"):
        lines += ["", "CONTEXT:", row["known_info"]]
    return "\n".join(lines)


def _parse_response(text: str) -> dict[str, Any]:
    """codex --output-schema guarantees clean JSON; the brace-span fallback covers
    --command-template engines. (The rich fence-tolerant extractor stays in
    deep_search/codex_judge.py, where rubric prose demands it.)"""
    text = (text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return {}
    return {}


@dataclass(frozen=True)
class Resolution:
    handle: str
    profile: dict[str, Any]
    error: str | None


def resolve_one(prompt: str, *, model: str, effort: str, timeout: int,
                command_template: str = "") -> tuple[dict[str, Any], str | None]:
    """Spawn one websearch agent for one person; return (parsed profile, error-or-None)."""
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=True) as out, \
            tempfile.NamedTemporaryFile("w+", suffix=".json", delete=True) as schema, \
            tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=True) as prompt_file:
        if command_template:
            prompt_file.write(prompt)
            prompt_file.flush()
            cmd = shlex.split(command_template.format(prompt_path=prompt_file.name))
            stdin_text = None
        else:
            json.dump(OUTPUT_SCHEMA, schema)
            schema.flush()
            cmd = ["codex", "exec", "-s", "read-only", "--skip-git-repo-check", "--ephemeral",
                   "-c", "tools.web_search=true", "--output-schema", schema.name,
                   "-o", out.name, "-c", f'model_reasoning_effort="{effort}"']
            if model:
                cmd += ["-m", model]
            stdin_text = prompt
        try:
            cp = subprocess.run(cmd, input=stdin_text, text=True, capture_output=True,
                                timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return ({}, "timeout")
        except OSError as e:
            return ({}, str(e))
        if cp.returncode != 0:
            detail = (cp.stderr or cp.stdout or "").strip()[-500:]
            return ({}, f"engine_exit_{cp.returncode}" + (f": {detail}" if detail else ""))
        if command_template:
            parsed = _parse_response(cp.stdout)
        else:
            out.seek(0)
            parsed = _parse_response(out.read())
        return (parsed, None if parsed else "empty_or_unparsable")


def _skip_reason(profile_path: Path, retry_days: int) -> str:
    """'' = attempt; 'resolved' = a found profile stands; 'recent_not_found' =
    a not_found younger than the retry TTL. A corrupt file is re-attempted."""
    if not profile_path.exists():
        return ""
    try:
        prior = json.loads(profile_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    if str(prior.get("status") or "") == "found" and prior.get("linkedin_url"):
        return "resolved"
    age_days = max(0.0, (time.time() - profile_path.stat().st_mtime) / 86400)
    return "recent_not_found" if age_days < retry_days else ""


class WebResolveManifest(StageManifest):
    source: str | None = None
    stage: str = "web-resolve"
    counts: dict[str, int] | None = None
    engine: str | None = None
    mode: str | None = None
    retarget_upsert: dict[str, Any] | None = None
    errors: list[str] | None = None
    error: str | None = None


class WebResolve(Node):
    """Websearch identity resolution for detaches, $0 metered. Same selection
    and queue as the paid deep-research node; per-person output profiles in its
    own dir; pending retargets through the shared sticky upsert (whose review.csv
    column families are declared by deep_reconcile/deep_synthesize — the same
    convention deep_research follows)."""

    name = "deep_web_resolve"
    inputs = (
        Artifact(path=str(VERDICTS_JSONL), required=False),
        Artifact(path=str(LINKEDIN_OVERRIDES_CSV), required=False),
        Artifact(path=str(DEFAULT_PEOPLE_CSV), required=False),
        Artifact(path=FACTS_TEMPLATE, required=False),
        Artifact(path=RAW_BUNDLE_TEMPLATE, required=False),
        Artifact(path=str(INDEX_JSON), required=False),
        Artifact(path=str(OWNER_JSON), required=False),
        # Resume reads this node's own prior output — a self-edge the graph drops.
        Artifact(path=WEB_PROFILE_TEMPLATE, required=False),
    )
    outputs = (
        Artifact(path=WEB_PROFILE_TEMPLATE, writes="upsert", required=False),
    )
    payload = WebResolveManifest
    manifest = str(WR_MANIFEST)

    def __init__(
        self,
        *,
        verdicts_jsonl: Path | None = None,
        overrides_csv: Path | None = None,
        people_csv: Path | None = None,
        facts_dir: Path | None = None,
        index_json: Path | None = None,
        raw_dir: Path | None = None,
        out_dir: Path | None = None,
        confirm_threshold: float = RESEARCH_CONFIRM_THRESHOLD,
        include_candidates: bool = False,
        limit: int = 0,
        concurrency: int = DEFAULT_CONCURRENCY,
        timeout: int = DEFAULT_TIMEOUT,
        retry_days: int = DEFAULT_RETRY_DAYS,
        model: str = "",
        reasoning_effort: str = DEFAULT_EFFORT,
        command_template: str = "",
        propose: bool = False,
    ) -> None:
        self.verdicts_jsonl = Path(verdicts_jsonl or VERDICTS_JSONL)
        self.overrides_csv = Path(overrides_csv or LINKEDIN_OVERRIDES_CSV)
        self.people_csv = Path(people_csv or DEFAULT_PEOPLE_CSV)
        self.facts_dir = Path(facts_dir or FACTS_DIR)
        self.index_json = Path(index_json or INDEX_JSON)
        self.raw_dir = Path(raw_dir or RAW_DIR)
        self.out_dir = Path(out_dir or WR_OUT_DIR)
        self.confirm_threshold = confirm_threshold
        self.include_candidates = include_candidates
        self.limit = limit
        self.concurrency = concurrency
        self.timeout = timeout
        self.retry_days = retry_days
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.command_template = command_template
        self.propose = propose
        self.result: dict[str, Any] = {}

    def bindings(self) -> dict[str, str]:
        return {
            str(VERDICTS_JSONL): str(self.verdicts_jsonl),
            str(LINKEDIN_OVERRIDES_CSV): str(self.overrides_csv),
            str(DEFAULT_PEOPLE_CSV): str(self.people_csv),
            FACTS_TEMPLATE: str(self.facts_dir / "{person_id}.jsonl"),
            RAW_BUNDLE_TEMPLATE: str(self.raw_dir / "{person_id}.json"),
            str(INDEX_JSON): str(self.index_json),
            str(OWNER_JSON): str(OWNER_JSON),
            WEB_PROFILE_TEMPLATE: str(self.out_dir / "{handle}" / WEB_PROFILE_NAME),
            self.manifest: str(self.out_dir / "manifest.json"),
        }

    def _select(self) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, str]]:
        """(subset, queue, handle->skip_reason). Selection mirrors the paid node;
        skip reasons come from this node's own prior output files."""
        verdicts = list(read_jsonl(self.verdicts_jsonl))
        overrides = load_override_rows(self.overrides_csv)
        subset = eligible_subset(verdicts, self.confirm_threshold, overrides)
        if self.include_candidates:
            subset += candidate_subset(self.facts_dir, overrides, index_json=self.index_json)
        people = load_people_rows(self.people_csv)
        queue = build_queue(subset, people, self.facts_dir, self.raw_dir)
        skips = {
            row["handle"]: reason
            for row in queue
            if (reason := _skip_reason(self.out_dir / row["handle"] / WEB_PROFILE_NAME,
                                       self.retry_days))
        }
        return subset, queue, skips

    def _base(self, subset: list[dict[str, Any]], skips: dict[str, str],
              pending: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "source": "web_resolve",
            "eligible": len(subset),
            "skipped_resolved": sum(1 for r in skips.values() if r == "resolved"),
            "skipped_recent_not_found": sum(1 for r in skips.values() if r == "recent_not_found"),
            "would_attempt": len(pending),
            "engine": "template" if self.command_template else "codex",
            "updated_at": now_iso(),
        }

    def estimate(self) -> dict[str, Any]:
        """The --dry-run path: selection + counts, no spawns, and deliberately
        NOT execute() — a free listing must never overwrite a completed pass's
        manifest (same rule as synthesize's estimate)."""
        subset, queue, skips = self._select()
        pending = [row for row in queue if row["handle"] not in skips]
        if self.limit:
            pending = pending[: self.limit]
        self.result = {**self._base(subset, skips, pending), "status": "dry_run",
                       "handles": [row["handle"] for row in pending]}
        return self.result

    def execute(self) -> WebResolveManifest:
        subset, queue, skips = self._select()
        if self.propose:
            return self._execute_propose(subset, skips)
        pending = [row for row in queue if row["handle"] not in skips]
        if self.limit:
            pending = pending[: self.limit]
        base = self._base(subset, skips, pending)

        self.out_dir.mkdir(parents=True, exist_ok=True)

        def work(row: dict[str, str]) -> Resolution:
            profile, err = resolve_one(
                render_prompt(row), model=self.model, effort=self.reasoning_effort,
                timeout=self.timeout, command_template=self.command_template)
            return Resolution(handle=row["handle"], profile=profile, error=err)

        resolutions: list[Resolution] = []
        if pending:
            with ThreadPoolExecutor(max_workers=min(self.concurrency, len(pending))) as pool:
                for res in pool.map(work, pending):
                    resolutions.append(res)
                    state = "error" if res.error else str(res.profile.get("status") or "?")
                    print(f"web-resolve: {res.handle}: {state}", file=sys.stderr)

        found = not_found = failed = 0
        for res in resolutions:
            if res.error:
                failed += 1
                continue
            profile = {**res.profile, "resolved_at": now_iso(), "source": "web_resolve"}
            if profile.get("status") == "found" and profile.get("linkedin_url"):
                found += 1
            else:
                not_found += 1
            person_dir = self.out_dir / res.handle
            person_dir.mkdir(parents=True, exist_ok=True)
            (person_dir / WEB_PROFILE_NAME).write_text(
                json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")

        errors = sorted({res.error for res in resolutions if res.error})
        status = "failed" if (pending and failed == len(pending)) else "completed"
        counts = {
            **{k: v for k, v in base.items() if isinstance(v, int)},
            "attempted": len(resolutions), "found": found,
            "not_found": not_found, "failed": failed,
            # Finds awaiting the --propose judge: this run's plus prior runs'.
            "proposable": found + base["skipped_resolved"],
        }
        self.result = {**base, "status": status, "mode": "research", **counts,
                       "errors": errors}
        return WebResolveManifest(
            status=status, source="web_resolve", engine=base["engine"],
            mode="research", counts=counts, errors=errors or None,
            error="all websearch agents failed" if status == "failed" else None,
        )

    def _execute_propose(self, subset: list[dict[str, Any]],
                         skips: dict[str, str]) -> WebResolveManifest:
        """--propose: judge the on-disk finds and upsert pending retargets.
        Spends OpenAI (~cents per find, fingerprint-cached so re-runs are $0);
        spawns nothing. Invoking --propose is the approval, like refresh."""
        proposable = sum(1 for r in skips.values() if r == "resolved")
        print(f"web-resolve: judging {proposable} find(s) with the identity judge…",
              file=sys.stderr)
        owner = load_owner()
        upsert = propose_retargets_from_output(
            self.out_dir, subset, self.overrides_csv,
            facts_dir=self.facts_dir, raw_dir=self.raw_dir,
            use_llm=True, owner_block=owner_background_block(owner) if owner else "",
            confirm_threshold=self.confirm_threshold,
            profile_name=WEB_PROFILE_NAME,
        )
        counts = {"eligible": len(subset), "proposable": proposable,
                  "proposed": int(upsert.get("proposed") or 0),
                  "judge_calls": int(upsert.get("judge_calls") or 0),
                  "cached_verdicts": int(upsert.get("cached_verdicts") or 0)}
        self.result = {"source": "web_resolve", "status": "completed",
                       "mode": "propose", **counts, "retargets": upsert,
                       "updated_at": now_iso()}
        return WebResolveManifest(
            status="completed", source="web_resolve", mode="propose",
            counts=counts, retarget_upsert=upsert,
        )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Websearch identity resolution for LinkedIn detaches ($0 metered; codex subscription)")
    p.add_argument("--dry-run", action="store_true", help="list who would be attempted; spawn nothing")
    p.add_argument("--limit", type=int, default=0, help="attempt at most N people (0 = all)")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="per-person engine timeout (s)")
    p.add_argument("--retry-days", type=int, default=DEFAULT_RETRY_DAYS,
                   help="re-attempt a not_found after this many days")
    p.add_argument("--include-candidates", action="store_true",
                   help="also research Added import candidates with no LinkedIn")
    p.add_argument("--model", default="", help="engine model (default: codex config default)")
    p.add_argument("--reasoning-effort", default=DEFAULT_EFFORT)
    p.add_argument("--command-template", default="",
                   help="alternate engine: a command with a {prompt_path} placeholder")
    p.add_argument("--propose", action="store_true",
                   help="judge the on-disk finds (paid OpenAI identity judge, ~cents per find) "
                        "and upsert pending retargets; spawns no websearch agents")
    p.add_argument("--confirm-threshold", type=float, default=RESEARCH_CONFIRM_THRESHOLD)
    args = p.parse_args(argv)

    node = WebResolve(
        limit=args.limit, concurrency=args.concurrency,
        timeout=args.timeout, retry_days=args.retry_days,
        include_candidates=args.include_candidates, model=args.model,
        reasoning_effort=args.reasoning_effort, command_template=args.command_template,
        propose=args.propose, confirm_threshold=args.confirm_threshold,
    )
    if args.dry_run:
        emit(node.estimate())
        return 0
    node.run()
    emit(node.result)
    return 1 if node.result.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
