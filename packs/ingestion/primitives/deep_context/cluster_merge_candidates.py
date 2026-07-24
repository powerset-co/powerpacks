"""[4/4] Detect same-person / merge candidates via a high-reasoning LLM judge.

Clustering REQUIRES LLM reasoning — deterministic name/email/phone scoring is only
the recall net, never the decision. Pipeline:

  1. Blocking (composite first/last-initial name keys + shared email/phone) plus a
     name-similarity gate produce candidate pairs cheaply -- so we never LLM every
     record that merely shares a common surname, only genuinely ambiguous
     same/similar-name pairs.
  1.5 A deterministic gate merges the can't-miss pairs in CODE (identical normalized
     name + a shared non-owner phone/email) — identity equality that strong never
     rides on model attention. Every other pair goes to the judge WITH a computed
     SHARED IDENTIFIERS section, so a normalized phone match can't hide in formatting.
  2. A high-reasoning LLM judge decides SAME / DIFFERENT per pair by weighing ALL
     evidence HOLISTICALLY — identity (name/nickname, employer, school, location,
     emails), the role each plays in my life, content & behavior (e.g. forwarding
     household receipts = family behavior), and tone/register where available. No
     single signal dominates; tone is just one input and is skipped when a record
     has no messages from me.
  3. Only judge-confirmed pairs become edges -> connected components -> clusters.

Writes a full verdict log (merge-verdicts.csv) incl. rejections for auditability.
``--no-llm`` falls back to deterministic scoring (offline/tests only).

Outputs: merge-candidates.csv / .md + a "Possible same person" section per dossier.

Changelog:
  2026-07-23 (audit dedup): now_iso, write_json import from common.jsonio instead of deep_context.common (deduped there); no behavior change.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.indexing.lib.openai_stream import drain_pool
from packs.indexing.lib.openai_usage_tiers import env_or_profile_int
from packs.indexing.lib.openai_responses import (
    estimate_cost_usd,
    is_retryable,
    make_async_client,
    parse_json_response,
    reasoning_effort,
    responses_kwargs,
    usage_tokens,
)
from packs.ingestion.primitives.deep_context import compose_dossier as compose
from packs.ingestion.primitives.deep_context.common import (
    DOSSIER_DIR,
    FACTS_DIR,
    INDEX_JSON,
    MERGE_CSV,
    MERGE_MD,
    RAW_DIR,
    emit,
    read_jsonl,
    load_env,
    normalize_name,
    phone_digits,
)
from packs.ingestion.primitives.common.jsonio import now_iso, write_json

DEFAULT_CONFIDENCE = 0.7   # judge must be at least this confident to merge
GATE_NAME_SIM = 0.85       # below this (and no shared contact) a pair isn't worth a call
SECTION_ANCHOR = "## Possible same person"
SAMPLE_PER_DIRECTION = 6
SAMPLE_CHARS = 200

JUDGE_SYSTEM = (
    "You decide whether two contact records (A and B) are the SAME PERSON, so they can be "
    "merged. Reason HOLISTICALLY over ALL the evidence — no single signal dominates. You are "
    "given each contact's name, my relationship to them, key identity facts, what we talk "
    "about, and sample messages (how I talk to them and how they talk to me).\n\n"
    "Weigh these together with careful reasoning:\n"
    "- IDENTITY: name including nicknames/short forms (e.g. Annmay vs Ann), middle initials, "
    "employer, school, location, emails/handles, and any hard CONTRADICTIONS.\n"
    "- ROLE IN MY LIFE: two records that play the same role (romantic partner, specific "
    "coworker, a particular vendor) are more likely the same person.\n"
    "- CONTENT & BEHAVIOR: what we actually do — e.g. forwarding household receipts, "
    "reservations, or logistics to me is intimate/family behavior; coordinating deals is "
    "professional. Behavior that fits the same relationship is strong evidence.\n"
    "- TONE/REGISTER, only WHEN available: consistent register supports same person; a clear "
    "formal-vs-intimate mismatch can indicate different people. If one record has NO messages "
    "from me, you simply cannot use tone — treat its absence as neutral, never as evidence.\n"
    "- SHARED EMAIL SEEN IN MESSAGES: an address marked '[also seen in messages]' was found in "
    "the conversation, not on the contact's own record. It is strong same-person evidence ONLY if "
    "it is plausibly THIS person's own alias — e.g. a near-1:1 thread where they wrote from it, or "
    "it matches their name. If it looks like a co-participant in a GROUP thread (several different "
    "people/addresses), do NOT treat merely sharing it as proof they are the same person.\n"
    "- SHARED PHONE NUMBER: when the prompt carries a SHARED IDENTIFIERS section listing a phone "
    "on BOTH records, the numbers are literally identical after code normalization — never "
    "re-derive or doubt that equality. A personal number belongs to one human: treat the pair as "
    "the same person with confidence ~0.99 even when role, employer, or era differ — people "
    "change careers and keep their number. Hold back ONLY when the evidence says the number is a "
    "shared line rather than personal: a front desk, main office, support/booking line, or a "
    "family landline used by several people.\n\n"
    "A shared or similar NAME ALONE is not enough — but a similar name PLUS aligned "
    "role/identity/behavior is strong. Set same_person=true only when the COMBINED evidence "
    "supports it; otherwise false."
)

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "same_person": {"type": "boolean"},
        "confidence": {"type": "number"},
        "tone_toward_a": {"type": "string", "description": "How I address contact A (e.g. casual, formal)"},
        "tone_toward_b": {"type": "string"},
        "tone_consistent": {"type": "boolean"},
        "reason": {"type": "string", "description": "One-line rationale, citing tone."},
    },
    "required": ["same_person", "confidence", "tone_toward_a", "tone_toward_b", "tone_consistent", "reason"],
}


# --- Jaro-Winkler (stdlib only, recall gate + --no-llm path) ----------------

def jaro(s1: str, s2: str) -> float:
    if s1 == s2:
        return 1.0
    if not s1 or not s2:
        return 0.0
    match_dist = max(len(s1), len(s2)) // 2 - 1
    s1_matches = [False] * len(s1)
    s2_matches = [False] * len(s2)
    matches = 0
    for i, ch in enumerate(s1):
        lo = max(0, i - match_dist)
        hi = min(i + match_dist + 1, len(s2))
        for j in range(lo, hi):
            if not s2_matches[j] and s2[j] == ch:
                s1_matches[i] = s2_matches[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    transpositions = 0
    k = 0
    for i, matched in enumerate(s1_matches):
        if matched:
            while not s2_matches[k]:
                k += 1
            if s1[i] != s2[k]:
                transpositions += 1
            k += 1
    transpositions //= 2
    return (matches / len(s1) + matches / len(s2) + (matches - transpositions) / matches) / 3


def jaro_winkler(s1: str, s2: str, prefix_weight: float = 0.1) -> float:
    base = jaro(s1, s2)
    prefix = 0
    for a, b in zip(s1, s2):
        if a == b and prefix < 4:
            prefix += 1
        else:
            break
    return base + prefix * prefix_weight * (1 - base)


# --- loading (dossier identity + message samples + relationship) ------------

def parse_frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    meta: dict[str, Any] = {}
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        raw = raw.strip()
        try:
            meta[key.strip()] = json.loads(raw)
        except json.JSONDecodeError:
            meta[key.strip()] = raw.strip('"')
    return meta


def _sample(messages: list[dict[str, Any]], direction: str) -> list[str]:
    out: list[str] = []
    for m in sorted(messages, key=lambda m: m.get("at") or "", reverse=True):
        if m.get("direction") != direction:
            continue
        text = (m.get("text") or "").strip()
        if text:
            out.append(text[:SAMPLE_CHARS])
        if len(out) >= SAMPLE_PER_DIRECTION:
            break
    return out


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _profile(facts_path: Path) -> dict[str, Any]:
    """Compact identity view (relationship + key facts + topics) for the judge."""
    if not facts_path.exists():
        return {}
    recs = list(read_jsonl(facts_path))
    fa = compose.merge_facts(recs) if recs else {}
    if not fa:
        return {}
    return {
        "relationship": str(fa.get("relationship_to_owner") or ""),
        "title": str(fa.get("title") or ""),
        "employers": [e.get("name", "") for e in (fa.get("employers") or []) if e.get("name")],
        "school": str(fa.get("school") or ""),
        "location": str(fa.get("location") or ""),
        "topics": list(fa.get("topics") or [])[:8],
        # Emails/phones/URLs the synthesis pulled out of the CONVERSATION — a person may reveal a
        # second address here that never made it onto their contact record (see identifier_contacts).
        "identifiers": [str(i) for i in (fa.get("identifiers") or [])],
    }


def load_people(index: dict[str, Any], dossier_dir: Path, raw_dir: Path, facts_dir: Path) -> list[dict[str, Any]]:
    by_phone = index.get("by_phone", {})
    owner_emails = _owner_emails(dossier_dir.parent)
    owner_phones = _owner_phones(dossier_dir.parent)
    people: list[dict[str, Any]] = []
    for slug, info in index.get("slugs", {}).items():
        path = dossier_dir / f"{slug}.md"
        if not path.exists():
            continue
        meta = parse_frontmatter(path.read_text(encoding="utf-8"))
        pid = info.get("person_id", "")
        bundle = _read_json(raw_dir / f"{pid}.json")
        msgs = bundle.get("messages") or []
        profile = _profile(facts_dir / f"{pid}.jsonl")
        emails = [e.lower() for e in (meta.get("emails") or [])]
        # Emails the synthesis found in the MESSAGES (facts.identifiers), minus this person's own
        # registered ones and the owner's (who is in every thread, so is pure noise). These only
        # WIDEN the candidate net as a full address (never local-parts — a shared first name isn't
        # identity). Whether a shared message-email actually means SAME PERSON (their own alias) vs
        # a co-CC'd third party in a group thread is the LLM judge's call, not the gate's.
        extra_emails = sorted(identifier_emails(profile.get("identifiers") or []) - set(emails) - owner_emails)
        record_phones = [d for d, slugs in by_phone.items() if slug in slugs]
        # Phones the synthesis found in the MESSAGES (signatures, "text me at ..."), minus the
        # record's own and the owner's — same widening role as extra_emails, phone edition.
        extra_phones = sorted(identifier_phones(profile.get("identifiers") or [])
                              - set(record_phones) - owner_phones)
        people.append({
            "slug": slug,
            "person_id": pid,
            "name": meta.get("name") or info.get("name") or "",
            "name_key": normalize_name(meta.get("name") or info.get("name") or ""),
            "emails": emails,
            "extra_emails": extra_emails,
            "phone_digits": record_phones,
            "extra_phones": extra_phones,
            "profile": profile,
            "from_me": _sample(msgs, "from_me"),
            "from_them": _sample(msgs, "from_them"),
        })
    return people


def email_localparts(emails: list[str]) -> set[str]:
    return {e.split("@", 1)[0] for e in emails if "@" in e}


def _looks_like_email(value: str) -> bool:
    return "@" in value and "." in value.rsplit("@", 1)[-1]


def identifier_emails(identifiers: list[str]) -> set[str]:
    """Email addresses the synthesis pulled out of the CONVERSATION (facts.identifiers). URLs and
    handles are dropped — only full emails, and only for FULL-address matching (never local-parts),
    so a linking address a contact used in messages can still pair them with a record that has it
    registered."""
    return {s.lower() for s in (str(i).strip() for i in identifiers or []) if _looks_like_email(s)}


def identifier_phones(identifiers: list[str]) -> set[str]:
    """Phone numbers the synthesis pulled out of the CONVERSATION (facts.identifiers), as
    comparable digit keys. Only phone-shaped strings count — emails and anything domain-like are
    skipped — and normalization is pure code (phone_digits), so a signature's
    '(m)/(c) 914-555-0466' meets a record's '+19145550466' as the same key. Whether a shared
    number is the person's own line or a shared/company one stays the judge's call for
    different-name pairs; identical-name pairs merge deterministically (slam_dunk_verdict)."""
    out: set[str] = set()
    for raw in identifiers or []:
        s = str(raw).strip()
        if not s or "@" in s or re.search(r"[a-z]{2,}\.[a-z]{2,}", s.lower()):
            continue
        digits = phone_digits(s)
        if 7 <= len(digits) <= 15:
            out.add(digits)
    return out


def _owner_emails(base: Path) -> set[str]:
    """The mailbox owner's own addresses — excluded from identifier matching because the owner
    appears in nearly every thread, so their address is noise, not a same-person signal."""
    return {e.strip().lower() for e in (_read_json(base / "owner.json").get("emails") or []) if e.strip()}


def _owner_phones(base: Path) -> set[str]:
    """The owner's own numbers (owner.json `phones`, when present) — excluded exactly like
    owner emails: a number the owner uses can surface in anyone's thread without being identity."""
    return {phone_digits(p) for p in (_read_json(base / "owner.json").get("phones") or []) if phone_digits(p)}


def all_emails(p: dict[str, Any]) -> set[str]:
    """Record emails + message-discovered ones (owner already excluded at load)."""
    return set(p["emails"]) | set(p.get("extra_emails") or [])


def all_phones(p: dict[str, Any]) -> set[str]:
    """Record phone digit keys + message-discovered ones (owner already excluded at load)."""
    return set(p["phone_digits"]) | set(p.get("extra_phones") or [])


def fmt_phone(digits: str) -> str:
    """One display shape per normalized key, so the judge sees identical strings on both sides
    ('9145550466' -> '+1 (914) 555-0466'; non-US keys keep their full digits)."""
    if len(digits) == 10:
        return f"+1 ({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return f"+{digits}"


# --- blocking + recall gate -------------------------------------------------

def _blocking_tokens(name_key: str) -> list[str]:
    """Name tokens for the blocking keys. Collapses hyphens/periods/apostrophes so
    'de-luca' == 'deluca', and KEEPS single-letter tokens so an initial-only
    surname ('Casey S.') still contributes a usable last-initial."""
    joined = re.sub(r"[.\-']+", "", name_key)
    cleaned = re.sub(r"[^a-z ]+", " ", joined)
    return [t for t in cleaned.split() if t]


def blocking_name_keys(name_key: str) -> set[str]:
    """Two composite name-blocking keys per person (examples are synthetic):
      <first>|<last-initial>   catches truncated/variant SURNAMES  (Jordan Bravado / Jordan B)
      <first-initial>|<last>   catches nickname/short FIRST names   (Sam / Samuel)
    A pair blocks on names iff they share either key. Splitting by first-initial keeps large
    common-surname buckets from enumerating O(n^2) and from hitting the 200-cap recall cliff,
    while retaining real duplicates. It also stops surfacing the same-first-name /
    different-surname pairs that invite false merges (Jordan Alpha / Jordan Bravo).

    A record with NO real surname (a single token, or an initial-only surname) additionally
    emits a first-name key, so two such sparse records ('Robin' / 'Robin F') can still meet;
    records WITH a real surname never emit it, so common first names do not re-explode."""
    toks = _blocking_tokens(name_key)
    if not toks:
        return set()
    first, last = toks[0], toks[-1]
    keys = {f"fnli:{first}|{last[0]}", f"filn:{first[0]}|{last}"}
    if len(toks) == 1 or len(last) == 1:
        keys.add(f"fn:{first}")
    return keys


def generate_pairs(people: list[dict[str, Any]]) -> set[tuple[int, int]]:
    """Blocked pairs that pass a recall gate: shared phone/email/email-localpart,
    or Jaro-Winkler name >= GATE_NAME_SIM. Names block on composite first/last-initial
    keys (see `blocking_name_keys`) so common surnames don't enumerate O(n^2). Keeps
    LLM calls to genuinely ambiguous pairs."""
    buckets: dict[str, list[int]] = {}
    for idx, p in enumerate(people):
        # Full-address keys include message-discovered `extra_emails`; local-part keys do NOT
        # (a shared first-name local-part is not evidence two people are the same).
        # Phone keys include message-discovered `extra_phones` — a signature number must be able
        # to pair a record with the same number registered, whatever the names look like.
        keys = {f"email:{e}" for e in all_emails(p)}
        keys |= {f"local:{lp}" for lp in email_localparts(p["emails"])}
        keys |= {f"phone:{d}" for d in all_phones(p)}
        keys |= {f"nm:{k}" for k in blocking_name_keys(p["name_key"])}
        for key in keys:
            buckets.setdefault(key, []).append(idx)
    cand: set[tuple[int, int]] = set()
    for members in buckets.values():
        if len(members) < 2 or len(members) > 200:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                cand.add((min(members[i], members[j]), max(members[i], members[j])))
    gated: set[tuple[int, int]] = set()
    for a, b in cand:
        pa, pb = people[a], people[b]
        if (all_emails(pa) & all_emails(pb)
                or email_localparts(pa["emails"]) & email_localparts(pb["emails"])
                or all_phones(pa) & all_phones(pb)
                or jaro_winkler(pa["name_key"], pb["name_key"]) >= GATE_NAME_SIM):
            gated.add((a, b))
    return gated


# --- LLM judge --------------------------------------------------------------

def _render_side(label: str, p: dict[str, Any]) -> str:
    pr = p.get("profile") or {}
    facts = []
    if pr.get("relationship"):
        facts.append(f"relationship: {pr['relationship']}")
    if pr.get("title") or pr.get("employers"):
        facts.append(f"work: {pr.get('title', '')} {('@ ' + ', '.join(pr['employers'])) if pr.get('employers') else ''}".strip())
    if pr.get("school"):
        facts.append(f"school: {pr['school']}")
    if pr.get("location"):
        facts.append(f"location: {pr['location']}")
    if pr.get("topics"):
        facts.append(f"we discuss: {', '.join(pr['topics'])}")
    facts_block = "\n".join(f"  {f}" for f in facts) or "  (no extracted facts)"
    me = "\n".join(f"  me→them: {t}" for t in p["from_me"]) or "  (no messages from me — tone unavailable)"
    them = "\n".join(f"  them→me: {t}" for t in p["from_them"]) or "  (no messages from them)"
    emails = ", ".join(p["emails"]) or "none"
    # Addresses seen only in the conversation (identifiers) — surface them so the judge can weigh a
    # shared one, but label them so a shared address isn't mistaken for the person's own contact.
    extra = ", ".join(p.get("extra_emails") or [])
    extra_line = f"  [also seen in messages: {extra}]\n" if extra else ""
    return (f"CONTACT {label} — {p['name']}  [emails: {emails}]\n{extra_line}"
            f"{facts_block}\nMessages:\n{me}\n{them}")


def shared_identifier_note(pa: dict[str, Any], pb: dict[str, Any]) -> str:
    """Computed identifier overlap for the judge prompt. The normalization already happened in
    code (phone_digits / lowercased emails), so the judge is TOLD the values are identical —
    a match can never hide behind '(914) 555-0466' vs '+19145550466' formatting again."""
    def phone_prov(p: dict[str, Any], d: str) -> str:
        return "contact record" if d in set(p["phone_digits"]) else "seen in messages"

    def email_prov(p: dict[str, Any], e: str) -> str:
        return "contact record" if e in set(p["emails"]) else "seen in messages"

    lines = [f"- phone {fmt_phone(d)} is in BOTH records "
             f"(A: {phone_prov(pa, d)}; B: {phone_prov(pb, d)})"
             for d in sorted(all_phones(pa) & all_phones(pb))]
    lines += [f"- email {e} is in BOTH records "
              f"(A: {email_prov(pa, e)}; B: {email_prov(pb, e)})"
              for e in sorted(all_emails(pa) & all_emails(pb))]
    if not lines:
        return ""
    return ("SHARED IDENTIFIERS (computed by code from normalized values — literally identical "
            "on both sides; formatting differences were already resolved):\n" + "\n".join(lines))


def judge_prompt(pa: dict[str, Any], pb: dict[str, Any]) -> str:
    shared = shared_identifier_note(pa, pb)
    shared_block = f"\n\n{shared}" if shared else ""
    return f"{_render_side('A', pa)}\n\n{_render_side('B', pb)}{shared_block}\n\nAre A and B the same person?"


def slam_dunk_verdict(pa: dict[str, Any], pb: dict[str, Any]) -> dict[str, Any] | None:
    """CODE-decided merge for the can't-miss case: identical normalized name PLUS a shared
    non-owner identifier (phone or full email). Equality this strong must never ride on model
    attention — a judge once kept two same-name records apart at 0.77 while both carried the
    same mobile number, reasoning from career drift. Different-name pairs sharing an identifier
    (couples, front desks, role inboxes) still go to the judge."""
    if not pa["name_key"] or pa["name_key"] != pb["name_key"]:
        return None
    phones = sorted(all_phones(pa) & all_phones(pb))
    emails = sorted(all_emails(pa) & all_emails(pb))
    if not phones and not emails:
        return None
    shared = ", ".join([fmt_phone(d) for d in phones] + emails)
    return {"same_person": True, "confidence": 0.99,
            "tone_toward_a": "", "tone_toward_b": "", "tone_consistent": True,
            "reason": f"deterministic: identical name + shared {shared}"}


async def judge_pair(client: Any, pa: dict[str, Any], pb: dict[str, Any], *, model: str,
                     effort: str, semaphore: asyncio.Semaphore, max_retries: int) -> dict[str, Any]:
    kwargs = responses_kwargs(model, effort=effort, schema=JUDGE_SCHEMA, schema_name="same_person")
    async with semaphore:
        attempt = 0
        while True:
            try:
                response = await client.responses.create(
                    model=model,
                    input=[{"role": "system", "content": JUDGE_SYSTEM},
                           {"role": "user", "content": judge_prompt(pa, pb)}],
                    **kwargs,
                )
                return {"verdict": parse_json_response(response, "judge"), "usage": usage_tokens(response), "error": ""}
            except Exception as exc:  # noqa: BLE001
                attempt += 1
                if is_retryable(exc) and attempt <= max_retries:
                    await asyncio.sleep(min(2 ** attempt, 30))
                    continue
                return {"verdict": {}, "usage": {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0},
                        "error": f"{type(exc).__name__}: {exc}"[:200]}


# --- clustering + output ----------------------------------------------------

def connected_components(n: int, edges: list[tuple[int, int]]) -> list[list[int]]:
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        parent[find(a)] = find(b)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return [g for g in groups.values() if len(g) > 1]


def inject_section(path: Path, body: str) -> None:
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    head = text.split(SECTION_ANCHOR)[0].rstrip()
    path.write_text(f"{head}\n\n{SECTION_ANCHOR}\n\n{body}\n", encoding="utf-8")


def deterministic_verdict(pa: dict[str, Any], pb: dict[str, Any]) -> dict[str, Any]:
    """Offline/tests fallback (--no-llm): shared contact or near-exact name."""
    shared = bool(all_emails(pa) & all_emails(pb)) or bool(all_phones(pa) & all_phones(pb))
    nsim = jaro_winkler(pa["name_key"], pb["name_key"])
    same = shared or nsim >= 0.97
    return {"same_person": same, "confidence": 0.95 if shared else round(nsim, 2),
            "tone_toward_a": "", "tone_toward_b": "", "tone_consistent": same,
            "reason": "shared contact" if shared else f"name similarity {nsim:.2f}"}


def _person_sig(p: dict[str, Any]) -> str:
    """Fingerprint of the judge-relevant IDENTITY fields for one person (deterministic; message
    tone samples are excluded so a rerun over unchanged facts produces the same value)."""
    prof = p.get("profile") or {}
    return "\x1f".join([
        p.get("name_key", ""),
        "|".join(sorted(set(p.get("emails") or []) | set(p.get("extra_emails") or []))),
        "|".join(sorted(all_phones(p))),
        prof.get("relationship", ""), prof.get("title", ""),
        "|".join(sorted(prof.get("employers") or [])),
        prof.get("school", ""), prof.get("location", ""),
        "|".join(sorted(prof.get("topics") or [])),
    ])


# Bumps automatically when the judge's system prompt changes, so a prompt edit re-judges everyone.
_JUDGE_VERSION = hashlib.sha1(JUDGE_SYSTEM.encode("utf-8")).hexdigest()[:8]


def pair_sig(pa: dict[str, Any], pb: dict[str, Any]) -> str:
    """Order-independent fingerprint of a pair's judge inputs + the prompt version. Same value on
    the next run -> reuse the cached verdict; a changed identity (new email, employer, name) or an
    updated JUDGE_SYSTEM changes it -> re-judge the pair."""
    a, b = sorted([_person_sig(pa), _person_sig(pb)])
    return hashlib.sha1(f"{_JUDGE_VERSION}\x1e{a}\x1e{b}".encode("utf-8")).hexdigest()[:16]


def load_cached_verdicts(path: Path) -> dict[frozenset, tuple[str, dict[str, Any]]]:
    """Prior merge-verdicts.csv, keyed by the {slug_a, slug_b} pair -> (sig, verdict). Rows from an
    older file that predates the slug/sig columns are skipped (they simply re-judge once)."""
    cache: dict[frozenset, tuple[str, dict[str, Any]]] = {}
    if not path.exists():
        return cache
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            sa, sb, sig = row.get("slug_a") or "", row.get("slug_b") or "", row.get("sig") or ""
            if not sa or not sb or not sig:
                continue
            cache[frozenset({sa, sb})] = (sig, {
                "same_person": (row.get("same_person") or "").strip().lower() == "true",
                "confidence": float(row.get("confidence") or 0),
                "tone_consistent": (row.get("tone_consistent") or "").strip().lower() == "true",
                "reason": row.get("reason", ""),
            })
    return cache


def load_legacy_verdicts(path: Path, people: list[dict[str, Any]]) -> dict[frozenset, dict[str, Any]]:
    """Adopt verdicts from a PRE-sig merge-verdicts.csv (name-only rows, written before this file
    gained the slug/sig columns) by matching each row's name_a/name_b back to the current people.
    A verdict is reused only when BOTH names resolve to exactly one current person (ambiguous or
    unknown names re-judge). This lets a network that was resolved before the cache existed reuse
    its already-paid verdicts on the first run instead of re-judging the whole set."""
    legacy: dict[frozenset, dict[str, Any]] = {}
    if not path.exists():
        return legacy
    by_name: dict[str, list[str]] = {}
    for p in people:
        by_name.setdefault(p["name_key"], []).append(p["slug"])

    def resolve(name: str) -> str | None:
        slugs = by_name.get(normalize_name(name or ""))
        return slugs[0] if slugs and len(slugs) == 1 else None

    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if (row.get("slug_a") or "") and (row.get("sig") or ""):
                continue  # already a sig-keyed row; the precise cache owns it, don't second-guess here
            sa, sb = resolve(row.get("name_a", "")), resolve(row.get("name_b", ""))
            if not sa or not sb or sa == sb:
                continue
            legacy[frozenset({sa, sb})] = {
                "same_person": (row.get("same_person") or "").strip().lower() == "true",
                "confidence": float(row.get("confidence") or 0),
                "tone_consistent": (row.get("tone_consistent") or "").strip().lower() == "true",
                "reason": row.get("reason", ""),
            }
    return legacy


def split_cached_pairs(pairs: list[tuple[int, int]], people: list[dict[str, Any]],
                       cache: dict[frozenset, tuple[str, dict[str, Any]]],
                       legacy: dict[frozenset, dict[str, Any]] | None = None,
                       ) -> tuple[list[tuple[int, int, str, dict[str, Any]]], list[tuple[int, int, str]]]:
    """Partition candidate pairs into (reused, to_judge): a pair whose {slugs} are cached with a
    MATCHING sig reuses that verdict; failing that, a name-adopted legacy verdict is reused (and
    stamped with the current sig so it upgrades in place); everything else is judged fresh."""
    legacy = legacy or {}
    reused: list[tuple[int, int, str, dict[str, Any]]] = []
    to_judge: list[tuple[int, int, str]] = []
    for a, b in pairs:
        sig = pair_sig(people[a], people[b])
        key = frozenset({people[a]["slug"], people[b]["slug"]})
        hit = cache.get(key)
        if hit and hit[0] == sig:
            reused.append((a, b, sig, hit[1]))
        elif key in legacy:
            reused.append((a, b, sig, legacy[key]))
        else:
            to_judge.append((a, b, sig))
    return reused, to_judge


def run(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    dossier_dir = Path(args.dossier_dir)
    index = _read_json(Path(args.index_json))
    people = load_people(index, dossier_dir, Path(args.raw_dir), Path(args.facts_dir))
    pairs = sorted(generate_pairs(people))

    # Deterministic gate first: identical name + shared identifier merges in code — free, and
    # immune to cache staleness and judge attention alike.
    slam: list[tuple[int, int, dict[str, Any]]] = []
    rest: list[tuple[int, int]] = []
    for a, b in pairs:
        verdict = slam_dunk_verdict(people[a], people[b])
        if verdict:
            slam.append((a, b, verdict))
        else:
            rest.append((a, b))

    # Incremental cache: reuse verdicts from the prior merge-verdicts.csv for unchanged pairs, so a
    # rerun only spends on NEW or changed pairs. --refresh ignores the cache and re-judges all.
    cache_path = Path(args.out_csv).with_name("merge-verdicts.csv")
    refresh = getattr(args, "refresh", False)
    cache = {} if refresh else load_cached_verdicts(cache_path)
    legacy = {} if refresh else load_legacy_verdicts(cache_path, people)
    reused, to_judge = split_cached_pairs(rest, people, cache, legacy)
    adopted = len({frozenset({people[a]["slug"], people[b]["slug"]}) for a, b, _s, _v in reused} & set(legacy))

    if getattr(args, "dry_run", False):
        # Blocking + cache lookup are free; only the NEW pairs below would be judged (small spend).
        per_lo, per_hi = 0.004, 0.02
        return {
            "source": "cluster_merge_candidates", "status": "dry_run",
            "people": len(people), "candidate_pairs": len(pairs),
            "pairs_deterministic": len(slam),
            "cached_reused": len(reused), "legacy_adopted": adopted,
            "candidate_pairs_to_judge": len(to_judge),
            "estimated_cost_usd_low": round(len(to_judge) * per_lo, 2),
            "estimated_cost_usd_high": round(len(to_judge) * per_hi, 2),
            "model": args.model, "reasoning_effort": args.reasoning_effort,
            "elapsed_ms": int((time.monotonic() - started) * 1000), "updated_at": now_iso(),
        }

    verdicts: list[dict[str, Any]] = [
        {"a": a, "b": b, "sig": pair_sig(people[a], people[b]), **v} for a, b, v in slam
    ] + [{"a": a, "b": b, "sig": sig, **v} for a, b, sig, v in reused]
    usage_total = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
    use_llm = not getattr(args, "no_llm", False)

    if use_llm and to_judge:
        load_env()
        # Wall-time is bound by per-call high-reasoning latency, not local CPU — parallelize hard.
        concurrency = args.concurrency or env_or_profile_int("POWERPACKS_OPENAI_CONCURRENCY", "openai_concurrency", fallback=64)
        effort = reasoning_effort(args.reasoning_effort)

        async def driver() -> None:
            client = make_async_client(timeout=args.timeout)
            semaphore = asyncio.Semaphore(max(1, concurrency))
            results: dict[int, dict[str, Any]] = {}

            def on_result(item: tuple[int, dict[str, Any]]) -> None:
                results[item[0]] = item[1]

            async def one(i: int, a: int, b: int) -> tuple[int, dict[str, Any]]:
                return i, await judge_pair(client, people[a], people[b], model=args.model,
                                           effort=effort, semaphore=semaphore, max_retries=args.max_retries)
            try:
                await drain_pool([one(i, a, b) for i, (a, b, _sig) in enumerate(to_judge)], on_result)
            finally:
                await client.close()
            for i, (a, b, sig) in enumerate(to_judge):
                res = results.get(i, {"verdict": {}, "usage": {}})
                for k in usage_total:
                    usage_total[k] += res.get("usage", {}).get(k, 0)
                verdicts.append({"a": a, "b": b, "sig": sig, **(res["verdict"] or {})})

        asyncio.run(driver())
    else:
        for a, b, sig in to_judge:
            verdicts.append({"a": a, "b": b, "sig": sig, **deterministic_verdict(people[a], people[b])})

    edges: list[tuple[int, int]] = []
    confirmed: list[dict[str, Any]] = []
    for v in verdicts:
        if v.get("same_person") and float(v.get("confidence") or 0) >= args.confidence:
            a, b = v["a"], v["b"]
            edges.append((a, b))
            confirmed.append({
                "slug_a": people[a]["slug"], "name_a": people[a]["name"],
                "slug_b": people[b]["slug"], "name_b": people[b]["name"],
                "confidence": round(float(v.get("confidence") or 0), 3),
                "tone_consistent": v.get("tone_consistent"),
                "reason": v.get("reason", ""),
            })

    confirmed.sort(key=lambda r: r["confidence"], reverse=True)
    _write_pairs_csv(Path(args.out_csv), confirmed)
    # Full audit log: every judged pair incl. rejections (why a duplicate was NOT merged).
    _write_verdicts_csv(Path(args.out_csv).with_name("merge-verdicts.csv"), people, verdicts)
    clusters = connected_components(len(people), edges)
    _write_clusters_md(Path(args.out_md), people, clusters, confirmed)

    neighbors: dict[str, list[tuple[str, str, float, str]]] = {}
    for r in confirmed:
        neighbors.setdefault(r["slug_a"], []).append((r["slug_b"], r["name_b"], r["confidence"], r["reason"]))
        neighbors.setdefault(r["slug_b"], []).append((r["slug_a"], r["name_a"], r["confidence"], r["reason"]))
    for person in people:
        matches = sorted(neighbors.get(person["slug"], []), key=lambda m: m[2], reverse=True)
        body = "\n".join(f"- [[{s}]] **{n}** (confidence {c:.2f}) — _{why}_" for s, n, c, why in matches) if matches else "_None detected._"
        inject_section(dossier_dir / f"{person['slug']}.md", body)

    billed_output = usage_total["output_tokens"] + usage_total["reasoning_tokens"]
    manifest = {
        "source": "cluster_merge_candidates",
        "status": "completed",
        "judge": "llm" if use_llm else "deterministic",
        "people": len(people),
        "pairs_total": len(pairs),
        "pairs_deterministic": len(slam),  # merged in code (identical name + shared identifier)
        "pairs_judged": len(to_judge),   # actually sent to the judge this run (rest reused from cache)
        "pairs_reused": len(reused),
        "pairs_legacy_adopted": adopted,  # reused from a pre-sig file by name match (upgraded in place)
        "candidate_pairs": len(confirmed),
        "clusters": len(clusters),
        "confidence_threshold": args.confidence,
        "tokens": usage_total,
        "estimated_cost_usd": estimate_cost_usd(usage_total["input_tokens"], billed_output, args.model),
        "out_csv": str(args.out_csv),
        "out_md": str(args.out_md),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "updated_at": now_iso(),
    }
    write_json(dossier_dir / "merge_manifest.json", manifest)
    return manifest


def _write_pairs_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["slug_a", "name_a", "slug_b", "name_b", "confidence", "tone_consistent", "reason"])
        writer.writeheader()
        writer.writerows(rows)


def _write_verdicts_csv(path: Path, people: list[dict[str, Any]], verdicts: list[dict[str, Any]]) -> None:
    # slug_a/slug_b/sig make this file double as the incremental cache (see load_cached_verdicts).
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["slug_a", "slug_b", "name_a", "name_b", "same_person",
                                           "confidence", "tone_consistent", "reason", "sig"])
        w.writeheader()
        for v in sorted(verdicts, key=lambda v: float(v.get("confidence") or 0), reverse=True):
            w.writerow({
                "slug_a": people[v["a"]]["slug"], "slug_b": people[v["b"]]["slug"],
                "name_a": people[v["a"]]["name"], "name_b": people[v["b"]]["name"],
                "same_person": v.get("same_person"), "confidence": v.get("confidence"),
                "tone_consistent": v.get("tone_consistent"), "reason": v.get("reason", ""),
                "sig": v.get("sig", ""),
            })


def _write_clusters_md(path: Path, people: list[dict[str, Any]], clusters: list[list[int]], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Merge candidates ({len(clusters)} clusters, {len(rows)} pairs)", "",
             f"_Generated {now_iso()}. LLM-judged on tone + identity. Confirm before merging._", ""]
    for i, group in enumerate(clusters, 1):
        lines.append(f"## Cluster {i}")
        for idx in group:
            lines.append(f"- [[{people[idx]['slug']}]] **{people[idx]['name']}**")
        lines.append("")
    if not clusters:
        lines.append("_No merge candidates confirmed._")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Detect same-person / merge candidates via an LLM tone-aware judge.")
    p.add_argument("--dossier-dir", default=str(DOSSIER_DIR))
    p.add_argument("--index-json", default=str(INDEX_JSON))
    p.add_argument("--raw-dir", default=str(RAW_DIR))
    p.add_argument("--facts-dir", default=str(FACTS_DIR))
    p.add_argument("--out-csv", default=str(MERGE_CSV))
    p.add_argument("--out-md", default=str(MERGE_MD))
    p.add_argument("--confidence", type=float, default=DEFAULT_CONFIDENCE, help="Min judge confidence to merge")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--reasoning-effort", default="high", choices=["minimal", "low", "medium", "high"])
    p.add_argument("--concurrency", type=int, default=0)
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--max-retries", type=int, default=6)
    p.add_argument("--dry-run", action="store_true", help="Count candidate pairs + estimate cost; no spend")
    p.add_argument("--no-llm", action="store_true", help="Deterministic fallback (offline/tests only)")
    p.add_argument("--refresh", action="store_true",
                   help="Ignore the cached merge-verdicts.csv and re-judge every pair from scratch")
    return p


def main(argv: list[str] | None = None) -> int:
    emit(run(build_parser().parse_args(argv)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
