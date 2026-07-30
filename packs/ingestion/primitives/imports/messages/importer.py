#!/usr/bin/env python3
"""Import matched Messages contacts + research candidates (contacts-direct).

Consumes the match-annotated `.powerpacks/messages/contacts.csv` — the upstream
`match_local_candidates.py match` step tiers each contact against the local
people catalog (unique phone/email, or unique exact name, or same-last-name
unique first-name-prefix / high-fuzzy -> `matched`; ambiguous or
first-name-only -> `suggested`; else `unmatched`) — and materializes it, with no
LLM, no research queue, and no enrichment call, into ONE output:
`import/messages/people.csv`, which carries both halves (#339 folded the separate
`candidates.csv` into it; `common/legacy.py` deletes the leftover file):

- `matched` contacts, keyed to the existing network person (message activity
  attaches to that person at fan-in).
- `unmatched` + `suggested` contacts passing the deterministic "worth
  researching" floor (real phone, plausibly-real saved name, message-count
  minimum), carried as `candidate:` rows. A `suggested` match is PARKED in
  candidate evidence, never auto-attached — the deep-context cluster judge
  decides. Identity resolution happens later in deep-context with cross-channel
  context.

Known gap: the tier-0 approval gate reads a retired review CSV that has had no
producer since #315 retired the research-review flow, so on a fresh install
every identifier match demotes to `suggested` until deep-context ships the
replacement approval surface.

Declared contract (`MessagesImport`, node `messages_import`):

  reads   `.powerpacks/messages/contacts.csv` (match-annotated) + the matcher's
          `*.match.manifest.json` gate
  writes  `import/messages/people.csv`, and the `messages` ROW SLICE of the
          shared `directory.csv` (`Artifact.owns_rows_where`) — every column of
          its own rows, no column of anyone else's

`directory.csv` is declared an OUTPUT only, never an input, even though
`replace_messages_directory_rows` reads it: that read is the read half of a
read-modify-write of a file this node already declares it owns a slice of, the
same rule #340 applied to a node's own manifest. Gmail's import, by contrast,
reads OTHER sources' directory rows to decide its own resolutions, so there it
is a real input.

Per-row policy lives in `util.py`, not here: `classify_contact` decides what one
contacts.csv row becomes (matched person / research candidate / dropped, plus the
skip counters it contributes) and `selected_contacts_people` materializes those
verdicts, owning only the run-scoped dedup counters that are not a property of
any single row.

Changelog:
  2026-07-30 (visible decision / one legal home for old-install cope):
    - The per-row selection rules moved to `util.classify_contact`. They used to
      be spelled inline in `selected_contacts_people`'s loop, interleaved with
      three accumulators, so reading "what happens to a suggested row that fails
      the floor" meant simulating the loop. The loop now reads a verdict and
      materializes it; counts, ordering, and manifest bytes are unchanged.
    - `candidate_to_messages_person` and `contact_row_to_candidate` take the
      channel list from their caller. The former used to JSON-decode `evidence`
      to recover a list the latter had encoded moments earlier, then
      `isinstance`-guard the result.
    - The two old-install scrubs left this file for `common/legacy.py`, dated
      with removal conditions: `people_csv_schema_stale` (now
      `messages_people_csv_predates_interaction_counts`) and the three unlinks
      of retired `import/messages/` artifacts, which became one
      `scrub_messages_import_dir` call at the same point in the materialize path.
  2026-07-26 (dead minting branch deleted): the `legacy_message_linkedin_id`
    fallback in `contact_row_to_messages_people` was unreachable — the only
    caller (`selected_contacts_people`) guards on a non-empty
    `matched_person_id`, so the `or` always short-circuited — and its comment
    described a case the guard makes impossible. Deleted; the matched-row id is
    `matched_person_id or generate_person_id(pub)`. The recipe's definition in
    `people_schema` and worth_view's folding of already-stranded ids stay.
  2026-07-25 (declared contract): `MessagesImport` is a `pipeline/contract.py`
    `Node` — it DECLARES its two inputs and two outputs instead of only opening
    them, the gate sequence moved from `run()` to `execute()` (`run()` is the
    inherited template), and the manifest payload the orchestrator used to
    assemble as `**fields` is the typed `MessagesImportManifest`. The manifest is
    still written by the import-stage `imports/common.py:write_manifest`, not the
    Node template — see `MessagesImport.manifest`. people.csv and the directory
    slice are byte-identical.
  2026-07-23 (dead accounts.json registry): removed the `read_accounts(self.args.
    accounts)` no-op (its return value was discarded) along with the `read_accounts`/
    `DEFAULT_ACCOUNTS` imports and the `--accounts` CLI arg. Nothing in this import
    reads the `accounts.json` registry.
  2026-07-23 (oop): the `run()` flow (fixed import dir, people.csv/candidates.csv
    outputs, floor knobs, manifest input, and the gate sequence) was folded into
    a `MessagesImport` orchestrator; run() is now a thin `MessagesImport(args).run()`
    wrapper. The pure row/floor/diff/directory helpers stay module-level (the
    package __init__ re-exports them). Still stateless — no run-state store, one
    fixed output dir + one manifest; behavior, CLI, and manifest payloads unchanged.
  2026-07-23 (audit):
    - One upfront repo-root path bootstrap replaced the duplicated try/except
      import block.
    - Matched contacts take the durable pub-derived person id on first sight;
      the ephemeral message-linkedin:* keys (which stranded facts/review rows
      when a later run re-keyed them) are retired except as the no-pub
      fallback.
    - Suggested matches are no longer auto-attached by the review gate; the
      deep-context cluster judge decides.
    - Review-era artifacts (people.input.csv, enrichment/) left the stage
      contract; run() deletes leftovers.
  2026-07-23 (audit batch 21): directory helpers import updated from
    discover.directory → imports.directory (the module moved to this stage).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Repo-root bootstrap so packs.* imports work in module AND script mode
# (uv run .../importer.py); must be in-file because script-mode never imports
# the package __init__.
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.schemas.people_schema import (  # noqa: E402
    PEOPLE_SCHEMA_COLUMNS,
    extract_public_identifier,
    generate_person_id,
    latest_interaction,
    merge_interaction_counts,
    normalize_linkedin_url,
    normalize_people_row,
    parse_jsonish,
)
from packs.ingestion.schemas.candidates_schema import (  # noqa: E402
    candidate_key_for,
    normalize_candidate_row,
)
from packs.ingestion.primitives.common.contact_fields import phones_from_value  # noqa: E402
from packs.ingestion.primitives.common.jsonio import emit, unique_strings  # noqa: E402
from packs.ingestion.primitives.common.legacy import (  # noqa: E402
    messages_people_csv_predates_interaction_counts,
    scrub_messages_import_dir,
)
from packs.ingestion.primitives.common.paths import (  # noqa: E402
    DEFAULT_BASE_DIR,
    DEFAULT_DIRECTORY_CSV,
    DEFAULT_IMPORT_DIR,
)
from packs.ingestion.primitives.discover.common import (  # noqa: E402
    read_csv_rows,
    write_csv_rows,
)
# The declared row shape of `.powerpacks/messages/contacts.csv`, imported from the
# DISCOVERY module that owns the file. `graph.check_graph` compares row models by
# IDENTITY, so every writer of that file must name THIS object — not an equal
# copy, and not a second module that re-exports it.
from packs.ingestion.primitives.discover.messages.models import (  # noqa: E402
    MessageContactRow,
)
from packs.ingestion.primitives.imports.directory import (  # noqa: E402
    DIRECTORY_COLUMNS,
    MESSAGES_DIRECTORY_ROWS,
    DirectoryRow,
    directory_rows_from_people_csv,
    merge_directory_rows,
    normalized_directory_row,
)
from packs.ingestion.primitives.pipeline.contract import (  # noqa: E402
    Artifact,
    Node,
    PeopleRow,
    StageManifest,
)
from packs.ingestion.primitives.imports.common import (  # noqa: E402
    csv_count,
    directory_row_matches_source,
    directory_source_account_quality,
    import_manifest_current,
    normalize_directory_source_accounts,
    write_manifest,
)
from packs.ingestion.primitives.imports.messages.util import (  # noqa: E402
    DEFAULT_MIN_MESSAGE_COUNT,
    DROPPED,
    MATCHED,
    classify_contact,
    contact_interaction_counts,
    contact_last_interaction,
    messages_source_channels,
    normalize_bool,
    parse_int_field,
    split_full_name,
)

MESSAGES_IMPORT_CONTRACT = "messages-contacts-direct-v6"
WORKING_CONTACTS_CSV = Path(".powerpacks/messages/contacts.csv")
MATCH_MANIFEST_JSON = Path(".powerpacks/messages/contacts.csv.match.manifest.json")


def contact_row_to_messages_people(
    row: dict[str, str],
    contacts_csv: Path,
) -> dict[str, str]:
    """Map a MATCHED contacts.csv row onto the canonical people schema."""
    linkedin_url = normalize_linkedin_url(row.get("matched_linkedin_url") or "")
    public_identifier = extract_public_identifier(linkedin_url)
    full_name = (row.get("matched_name") or "").strip() or (row.get("name") or "").strip()
    first_name, last_name = split_full_name(full_name)
    phone = (row.get("phone") or "").strip()
    is_email_handle = "@" in phone
    summary_parts = ["selection=matched"]
    if row.get("match_method"):
        summary_parts.append(f"match_method={row.get('match_method')}")
    interaction_counts = contact_interaction_counts(row)
    people = {
        # The durable directory id is a pure function of the pub, so a matched
        # contact gets its FINAL key on first sight. The fallback is for direct
        # callers only: the one production caller (`selected_contacts_people`)
        # guards on a non-empty matched_person_id, so it always short-circuits
        # there — which is also why the retired `message-linkedin:` minting
        # branch that used to sit behind it was unreachable and was deleted
        # (2026-07-26; `people_schema.legacy_message_linkedin_id` survives
        # solely so worth_view can FOLD already-stranded ids).
        "id": (row.get("matched_person_id") or "").strip()
        or generate_person_id(public_identifier),
        "public_identifier": public_identifier,
        "linkedin_url": linkedin_url,
        "first_name": first_name,
        "last_name": last_name,
        "full_name": full_name,
        "summary": "; ".join(summary_parts),
        # Deliberately blank so the fan-in merge keeps the enriched source
        # row's provider (including the `synthetic` keep-gate token).
        "enrichment_provider": "",
        "primary_email": phone if is_email_handle else "",
        "all_emails": json.dumps([phone], ensure_ascii=False) if is_email_handle else "",
        "primary_phone": "" if is_email_handle else phone,
        "all_phones": (
            "" if is_email_handle or not phone else json.dumps([phone], ensure_ascii=False)
        ),
        "source_channels": ",".join(messages_source_channels(row)),
        "source_artifacts": str(contacts_csv),
        # The candidate identity an earlier run minted for this SAME contact
        # row (candidate_key_for on the same phone field — kept in lockstep
        # with contact_row_to_candidate). Import is the only witness that the
        # phone-axis candidate and this matched person are one human; emitting
        # the equivalence here lets parent-building fold the old identity in.
        "superseded_person_ids": (
            json.dumps([f"candidate:{candidate_key_for('', phone)}"], ensure_ascii=False)
            if candidate_key_for("", phone) else ""
        ),
        "interaction_counts": (
            json.dumps(interaction_counts, ensure_ascii=False) if interaction_counts else ""
        ),
        "last_interaction": contact_last_interaction(row),
    }
    return normalize_people_row(people)


def contact_row_to_candidate(
    row: dict[str, str],
    contacts_csv: Path,
    *,
    channels: list[str],
) -> dict[str, str]:
    """Map a floor-passing UNMATCHED contacts.csv row onto the candidates schema.

    `channels` is passed in rather than re-derived so the candidate row and the
    people row this contact becomes are built from ONE reading of its source
    columns."""
    phone = (row.get("phone") or "").strip()
    counts = contact_interaction_counts(row)
    # Single primary channel by DM volume (ties -> first listed channel).
    source = max(counts, key=lambda ch: counts[ch]) if counts else channels[0]
    evidence: dict[str, Any] = {
        "channels": channels,
        "message_count": parse_int_field(row.get("message_count")),
        "is_in_group_chats": normalize_bool(row.get("is_in_group_chats", "")) is True,
        "source_artifacts": str(contacts_csv),
    }
    if (row.get("match_status") or "").strip().lower() == "suggested":
        evidence["suggested_person_id"] = (row.get("matched_person_id") or "").strip()
        evidence["suggested_name"] = (row.get("matched_name") or "").strip()
        evidence["suggested_linkedin_url"] = normalize_linkedin_url(
            row.get("matched_linkedin_url") or ""
        )
        if row.get("match_confidence"):
            evidence["match_confidence"] = row.get("match_confidence")
    candidate = {
        "candidate_key": candidate_key_for("", phone),
        "source": source,
        "full_name": (row.get("name") or "").strip(),
        "primary_phone": phone,
        "all_phones": json.dumps([phone], ensure_ascii=False) if phone else "",
        "interaction_counts": json.dumps(counts, ensure_ascii=False) if counts else "",
        "last_interaction": contact_last_interaction(row),
        "evidence": evidence,
    }
    return normalize_candidate_row(candidate)


def candidate_to_messages_person(
    candidate: dict[str, str],
    contacts_csv: Path,
    *,
    channels: list[str],
) -> dict[str, str]:
    """Represent an unresolved, floor-passing contact in the sole people schema.

    An absent LinkedIn identifier is data, not a separate admission lane.  The
    fan-in preserves this stable candidate id until directory evidence later
    promotes the row to a LinkedIn key.

    `channels` comes from the caller for the same reason `contact_row_to_candidate`
    takes it: this used to re-read the channel list back out of the candidate
    row's `evidence` — JSON-encoding a list this function's own caller had just
    computed, then decoding it and `isinstance`-guarding the result to prove it
    was still a dict.
    """
    key = candidate.get("candidate_key", "")
    return normalize_people_row({
        "id": f"candidate:{key}",
        "full_name": candidate.get("full_name", ""),
        "primary_email": candidate.get("primary_email", ""),
        "all_emails": candidate.get("all_emails", ""),
        "primary_phone": candidate.get("primary_phone", ""),
        "all_phones": candidate.get("all_phones", ""),
        "summary": "selection=unresolved",
        "source_channels": ",".join(channels),
        "source_artifacts": str(contacts_csv),
        "interaction_counts": candidate.get("interaction_counts", ""),
        "last_interaction": candidate.get("last_interaction", ""),
    })


def merge_matched_people_rows(
    existing: dict[str, str],
    incoming: dict[str, str],
) -> dict[str, str]:
    """Union two people rows for the SAME matched person (several contact rows
    resolved to one network person, e.g. two phones): first non-empty value per
    identity field, union of phones/channels/superseded ids, summed interaction
    counts, latest activity wins."""
    merged = dict(existing)
    for key in (
        "primary_phone",
        "primary_email",
        "full_name",
        "first_name",
        "last_name",
        "headline",
        "summary",
        "city",
        "country",
        "current_title",
        "current_company",
    ):
        if not merged.get(key) and incoming.get(key):
            merged[key] = incoming[key]
    phones = unique_strings([
        *phones_from_value(merged.get("all_phones", "")),
        *phones_from_value(merged.get("primary_phone", "")),
        *phones_from_value(incoming.get("all_phones", "")),
        *phones_from_value(incoming.get("primary_phone", "")),
    ])
    if phones:
        merged["primary_phone"] = merged.get("primary_phone") or phones[0]
        merged["all_phones"] = json.dumps(phones, ensure_ascii=False)
    channels = unique_strings(
        (merged.get("source_channels", "").split(",") if merged.get("source_channels") else [])
        + (incoming.get("source_channels", "").split(",") if incoming.get("source_channels") else [])
    )
    if channels:
        merged["source_channels"] = ",".join(channels)
    providers = unique_strings(
        [merged.get("enrichment_provider", ""), incoming.get("enrichment_provider", "")]
    )
    if providers:
        merged["enrichment_provider"] = ",".join(providers)
    counts = merge_interaction_counts(
        merged.get("interaction_counts"),
        incoming.get("interaction_counts"),
    )
    merged["interaction_counts"] = json.dumps(counts, ensure_ascii=False) if counts else ""
    merged["last_interaction"] = latest_interaction(
        merged.get("last_interaction"), incoming.get("last_interaction")
    )
    superseded = unique_strings([
        *parse_jsonish(merged.get("superseded_person_ids"), []),
        *parse_jsonish(incoming.get("superseded_person_ids"), []),
    ])
    merged["superseded_person_ids"] = (
        json.dumps(superseded, ensure_ascii=False) if superseded else ""
    )
    return normalize_people_row(merged)


def selected_contacts_people(
    contacts_csv: Path,
    *,
    min_message_count: int = DEFAULT_MIN_MESSAGE_COUNT,
    include_group_only: bool = False,
) -> tuple[dict[str, Any], list[dict[str, str]], list[dict[str, str]]]:
    """Split matched contacts into people rows and floor-passing unmatched
    contacts into candidate rows."""
    if not contacts_csv.exists():
        return ({
            "contacts_csv": str(contacts_csv),
            "total_rows": 0,
            "people_rows": 0,
            "candidate_rows": 0,
            "selection_counts": {},
            "skipped": {"missing_contacts_csv": 1},
        }, [], [])
    _fields, rows = read_csv_rows(contacts_csv)
    people_by_key: dict[str, dict[str, str]] = {}
    candidates_by_key: dict[str, dict[str, str]] = {}
    selection_counts: dict[str, int] = {}
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for row in rows:
        # The per-row policy is one function (util.classify_contact); this loop
        # only materializes its verdict and owns the run-scoped dedup counters.
        selection = classify_contact(
            row,
            min_message_count=min_message_count,
            include_group_only=include_group_only,
        )
        for reason in selection.skips:
            skip(reason)
        if selection.outcome == MATCHED:
            person = contact_row_to_messages_people(row, contacts_csv)
            key = person.get("public_identifier") or person.get("id", "")
            if key in people_by_key:
                skip("duplicate_matched_person")
                people_by_key[key] = merge_matched_people_rows(
                    people_by_key[key], person
                )
            else:
                people_by_key[key] = person
                selection_counts["matched"] = selection_counts.get("matched", 0) + 1
            continue
        if selection.outcome == DROPPED:
            continue
        channels = messages_source_channels(row)
        candidate_row = contact_row_to_candidate(row, contacts_csv, channels=channels)
        key = candidate_row.get("candidate_key", "")
        if not key:
            skip("short_code_or_invalid_phone")
            continue
        if key in candidates_by_key:
            skip("duplicate_phone")
            continue
        candidates_by_key[key] = candidate_to_messages_person(
            candidate_row, contacts_csv, channels=channels)
        selection_counts["phone_only"] = selection_counts.get("phone_only", 0) + 1

    people_rows = [people_by_key[key] for key in sorted(people_by_key)]
    candidate_rows = [candidates_by_key[key] for key in sorted(candidates_by_key)]
    summary = {
        "contacts_csv": str(contacts_csv),
        "total_rows": len(rows),
        "people_rows": len(people_rows),
        "candidate_rows": len(candidate_rows),
        "selection_counts": selection_counts,
        "skipped": skipped,
    }
    return summary, people_rows, candidate_rows


def existing_csv_column(path: Path, column: str) -> set[str]:
    """Non-empty values of one column in an existing CSV (empty set if absent)."""
    if not path.exists():
        return set()
    return {
        (row.get(column) or "").strip()
        for row in read_csv_rows(path)[1]
        if (row.get(column) or "").strip()
    }


def messages_import_diff(
    contacts_csv: Path,
    import_dir: Path,
    *,
    min_message_count: int = DEFAULT_MIN_MESSAGE_COUNT,
    include_group_only: bool = False,
) -> dict[str, Any]:
    """What a run WOULD write vs the existing outputs — powers the
    --confirm-import approval prompt (new people/candidates counts)."""
    materialized, people_rows, candidate_rows = selected_contacts_people(
        contacts_csv,
        min_message_count=min_message_count,
        include_group_only=include_group_only,
    )
    people_ids = {row.get("id", "") for row in people_rows if row.get("id")}
    candidate_ids = {row.get("id", "") for row in candidate_rows if row.get("id")}
    existing_people_ids = existing_csv_column(import_dir / "people.csv", "id")
    new_people = len(people_ids - existing_people_ids)
    new_candidates = len(candidate_ids - existing_people_ids)
    return {
        "materialized": materialized,
        "people_rows": len(people_ids),
        "candidate_rows": len(candidate_ids),
        "new_people": new_people,
        "new_candidates": new_candidates,
        "new_rows": new_people + new_candidates,
    }


def replace_messages_directory_rows(
    people_csv: Path,
    directory_csv: Path | None = None,
) -> dict[str, Any]:
    """Replace the messages-sourced rows of the shared directory.csv with rows
    derived from this run (other sources retained verbatim) — the import owns
    exactly its own slice of the directory."""
    directory_csv = directory_csv or DEFAULT_DIRECTORY_CSV
    retained: dict[str, dict[str, str]] = {}
    existing_rows = read_csv_rows(directory_csv)[1] if directory_csv.exists() else []
    removed_rows = 0
    for row in existing_rows:
        normalized = normalized_directory_row(row, source="directory")
        if not normalized:
            continue
        if directory_row_matches_source(normalized, "messages") or normalized.get(
            "source_key", ""
        ).startswith("messages:"):
            removed_rows += 1
            continue
        retained[normalized["source_key"]] = normalized
    incoming = directory_rows_from_people_csv(people_csv, source="messages")
    merged = merge_directory_rows(incoming, retained)
    write_csv_rows(directory_csv, DIRECTORY_COLUMNS, merged)
    return {
        "path": str(directory_csv),
        "existing_rows": len(existing_rows),
        "removed_messages_rows": removed_rows,
        "imported_messages_rows": len(incoming),
        "rows": len(merged),
    }


class MessagesImportManifest(StageManifest):
    """This stage's typed manifest payload — the pydantic successor to the
    `**fields` dict `_manifest()` used to splat into `write_manifest`. Field order
    IS the completed manifest's key order; the optional fields are dropped when
    None exactly as the old per-path dicts omitted them. `reason` is `""` (not
    None) on a completed run, as before."""

    # No `stage` field: the import-stage writer already stamps `source`.
    status: str = ""
    reason: str | None = None
    approval_type: str | None = None
    message: str | None = None
    blocked: dict[str, Any] | None = None
    input: dict[str, Any] = {}
    outputs: dict[str, Any] = {}
    stats: dict[str, Any] = {}
    diff: dict[str, Any] | None = None
    materialized: dict[str, Any] | None = None
    directory: dict[str, Any] | None = None


class MessagesImport(Node):
    """Orchestrates the contacts-direct Messages import.

    Owns the fixed import dir, its `people.csv` output, the floor knobs, the
    manifest input, and the gate sequence (schema/manifest no-op -> contacts-present
    and matched prerequisites -> the --confirm-import approval when the diff adds
    rows -> materialize people.csv -> replace the directory messages slice -> the
    import manifest). Stateless: no run-state store, just the one fixed output dir
    and one manifest. The pure row/floor/diff/directory transforms stay
    module-level functions the orchestrator calls.

    `execute()` is the gate sequence; `run()` is the inherited Node template."""

    source = "messages"

    name = "messages_import"
    inputs = (
        # required=False on BOTH: absence is a handled, actionable state this node
        # reports itself. Missing contacts.csv -> a `messages_contacts_missing`
        # manifest naming the discover command to run; a missing match manifest ->
        # `messages_contacts_not_matched` naming the matcher (or --allow-unmatched).
        # A bare `not_ready` payload would throw both messages away.
        Artifact(path=str(WORKING_CONTACTS_CSV), row_model=MessageContactRow, required=False),
        Artifact(path=str(MATCH_MANIFEST_JSON), required=False),
    )
    outputs = (
        Artifact(path=str(DEFAULT_IMPORT_DIR / source / "people.csv"), row_model=PeopleRow, writes="full_rewrite"),
        # The messages ROW SLICE of the shared aggregate. `full_rewrite` is
        # literal: replace_messages_directory_rows DELETES every `messages:`-keyed
        # row and rewrites that slice from this run. `owns_rows_where` is what
        # keeps that honest next to gmail's writer — see imports/directory.py.
        Artifact(
            path=str(DEFAULT_DIRECTORY_CSV),
            row_model=DirectoryRow,
            writes="full_rewrite",
            owns_rows_where=MESSAGES_DIRECTORY_ROWS,
        ),
    )
    payload = MessagesImportManifest
    # "" is deliberate. This stage's manifest is written by the IMPORT-stage
    # writer (`imports/common.py:write_manifest`), whose fingerprint chain
    # `import_manifest_current` reads for the no-op gate and which
    # `common/manifests.py` documents as divergent from `write_stage_manifest` on
    # purpose. `execute()` writes it and parks the result on `self.written`; the
    # Node template must not put a second, differently-fingerprinted manifest.json
    # on top of it.
    manifest = ""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.import_dir = DEFAULT_IMPORT_DIR / self.source
        self.people_csv = self.import_dir / "people.csv"
        self.contacts_csv = WORKING_CONTACTS_CSV
        self.directory_csv = DEFAULT_DIRECTORY_CSV
        self.match_manifest_json = MATCH_MANIFEST_JSON
        # The manifest dict `write_manifest` actually produced (it may return the
        # unchanged existing one) — what the CLI emits, like the gmail discovery
        # channel's `record`.
        self.written: dict[str, Any] = {}
        self.min_message_count = int(getattr(args, "min_message_count", DEFAULT_MIN_MESSAGE_COUNT))
        self.include_group_only = bool(getattr(args, "include_group_only", False))
        self.expected_input = {
            "pipeline_contract": MESSAGES_IMPORT_CONTRACT,
            "mode": "contacts-direct",
            "min_message_count": self.min_message_count,
            "include_group_only": self.include_group_only,
        }
        self.manifest_input = {
            **self.expected_input,
            "contacts_csv": str(self.contacts_csv),
            "match_manifest": str(MATCH_MANIFEST_JSON),
            "discovery_manifest": str(DEFAULT_BASE_DIR / "discover" / self.source / "manifest.json"),
        }

    def bindings(self) -> dict[str, str]:
        """Declared path -> this instance's path. Every declared path is a module
        default the tests patch (DEFAULT_IMPORT_DIR, WORKING_CONTACTS_CSV,
        DEFAULT_DIRECTORY_CSV, MATCH_MANIFEST_JSON), so the binding is what makes
        a temp-dir run validate against the declaration. Keys come from the
        declaration itself, never a second read of the default."""
        contacts_declared, match_declared = (item.path for item in self.inputs)
        people_declared, directory_declared = (item.path for item in self.outputs)
        return {
            contacts_declared: str(self.contacts_csv),
            match_declared: str(self.match_manifest_json),
            people_declared: str(self.people_csv),
            directory_declared: str(self.directory_csv),
        }

    def _manifest(self, payload: MessagesImportManifest) -> MessagesImportManifest:
        """Write this stage's single import manifest, parking the writer's result
        (which may be the unchanged existing manifest) on `self.written`."""
        self.written = write_manifest(self.source, payload.to_payload(), import_dir=DEFAULT_IMPORT_DIR)
        return payload

    def execute(self) -> MessagesImportManifest:
        """Gate sequence -> materialization. Returns the typed manifest payload."""
        # An existing people.csv predating the interaction-count columns is a code
        # change, not a data change, so the fingerprint no-op cannot catch it — a
        # stale schema forces a re-run. Old-install cope, so it lives in
        # common/legacy.py and runs first, before any gate reads the manifest.
        schema_stale = messages_people_csv_predates_interaction_counts(self.people_csv)
        current = None if schema_stale else import_manifest_current(
            self.source,
            self.expected_input,
            import_dir=DEFAULT_IMPORT_DIR,
        )
        if current:
            # A no-op writes nothing, so there is no manifest body to type — the
            # previous run's manifest IS the answer. Mirror only its status so the
            # template still verifies the outputs it says are current.
            self.written = current
            return MessagesImportManifest(status=str(current.get("status") or ""))
        if not self.contacts_csv.exists():
            return self._manifest(MessagesImportManifest(
                status="failed",
                reason="messages_contacts_missing",
                message=(
                    f"Discover Messages contacts before import: {self.contacts_csv}. "
                    "Run: uv run --project . python packs/ingestion/primitives/"
                    "discover/messages/discover.py discover"
                ),
                input=self.manifest_input,
                outputs={},
                stats={"people": 0, "candidates": 0},
            ))
        if not self.match_manifest_json.exists() and not self.args.allow_unmatched:
            return self._manifest(MessagesImportManifest(
                status="failed",
                reason="messages_contacts_not_matched",
                message=(
                    "Match contacts against your network before import (or pass "
                    "--allow-unmatched). Run: uv run --project . python packs/ingestion/"
                    f"primitives/imports/messages/match_local_candidates.py match "
                    f"--contacts {self.contacts_csv}"
                ),
                input=self.manifest_input,
                outputs={},
                stats={"people": 0, "candidates": 0},
            ))
        diff = messages_import_diff(
            self.contacts_csv,
            self.import_dir,
            min_message_count=self.min_message_count,
            include_group_only=self.include_group_only,
        )
        if diff["new_rows"] > 0 and not self.args.confirm_import:
            message = (
                f"Import Messages contacts: attach message activity to {diff['people_rows']} "
                f"matched people and add {diff['candidate_rows']} research candidates?"
            )
            return self._manifest(MessagesImportManifest(
                status="blocked_approval",
                approval_type="import_confirmation",
                message=message,
                blocked={
                    "status": "blocked_approval",
                    "approval_type": "import_confirmation",
                    "source": "messages",
                    "message": message,
                    "payload": diff,
                },
                input=self.manifest_input,
                outputs={},
                stats={
                    "people": 0,
                    "candidates": diff["candidate_rows"],
                },
                diff=diff,
            ))
        return self._materialize(diff)

    def _materialize(self, diff: dict[str, Any]) -> MessagesImportManifest:
        """Split matched people vs candidates, write both CSVs, replace the
        directory messages slice, and write the completed/failed manifest."""
        materialized, people_rows, candidate_rows = selected_contacts_people(
            self.contacts_csv,
            min_message_count=self.min_message_count,
            include_group_only=self.include_group_only,
        )
        self.import_dir.mkdir(parents=True, exist_ok=True)
        # Artifacts older Powerpacks versions left in this directory. What they
        # are and when they can stop being handled lives in common/legacy.py.
        scrub_messages_import_dir(self.import_dir)
        write_csv_rows(self.people_csv, PEOPLE_SCHEMA_COLUMNS, people_rows + candidate_rows)
        directory_replacement = replace_messages_directory_rows(self.people_csv)
        directory_normalization = normalize_directory_source_accounts("messages")
        directory_quality = directory_source_account_quality("messages")
        status = "completed" if directory_quality["status"] == "ok" else "failed"
        return self._manifest(MessagesImportManifest(
            status=status,
            reason="directory_source_account_quality_failed" if status == "failed" else "",
            input=self.manifest_input,
            outputs={
                "people_csv": str(self.people_csv),
            },
            stats={
                "people": csv_count(str(self.people_csv)),
                "candidates": len(candidate_rows),
            },
            diff=diff,
            materialized=materialized,
            directory={
                "path": str(self.directory_csv),
                "replacement": directory_replacement,
                "normalization": directory_normalization,
                "quality": directory_quality,
            },
        ))


def run(args: argparse.Namespace) -> dict:
    """The whole import, via the `MessagesImport` orchestrator: schema/fingerprint
    no-op checks -> prerequisite gates (contacts discovered; matched unless
    --allow-unmatched) -> the --confirm-import approval when the diff adds rows ->
    materialize people.csv -> replace the directory messages slice.

    Returns the manifest `write_manifest` produced, not the Node template's typed
    body: that manifest (with its `source`, `fingerprints`, `noop`) is the payload
    the skills and the no-op gate read."""
    imp = MessagesImport(args)
    imp.run()
    return imp.written


def build_parser() -> argparse.ArgumentParser:
    """CLI: one run command; floor knobs + the two explicit consent flags
    (--confirm-import, --allow-unmatched)."""
    parser = argparse.ArgumentParser(
        description="Import matched Messages contacts + research candidates"
    )
    parser.add_argument("command", choices=["run"])
    parser.add_argument("--operator-id", default="local")
    parser.add_argument("--confirm-import", action="store_true")
    parser.add_argument(
        "--min-message-count", type=int, default=DEFAULT_MIN_MESSAGE_COUNT,
        help="Minimum total DM messages for an unmatched contact to become a candidate",
    )
    parser.add_argument(
        "--include-group-only", action="store_true",
        help="Keep low-DM contacts that only appear via group chats",
    )
    parser.add_argument(
        "--allow-unmatched", action="store_true",
        help="Proceed without a match manifest (all contacts floor-tested as unmatched)",
    )
    return parser


def main() -> int:
    """Exit 0 success/no-op, 1 failure, 20 blocked on the --confirm-import
    approval (unlike gmail, this import HAS a real approval: adding rows to
    the network needs an explicit yes)."""
    args = build_parser().parse_args()
    payload = run(args)
    emit(payload)
    return 20 if payload.get("status") == "blocked_approval" else 1 if payload.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
