"""One-time import of pre-SQLite deep-context artifacts.

This is the only file-to-database path. It is explicit, requires an empty
canonical store, and never runs from a view, server boot, or user action.
"""
from __future__ import annotations

import json
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db import batons
from packs.ingestion.primitives.deep_context.db.schema import (
    ApprovedState,
    DecisionKind,
    DecisionRow,
    FactRow,
    HumanWorth,
    LLM_REJECT_VALUES,
    LinkRow,
    MachineWorth,
    ParentRow,
    PersonRow,
    ResearchRow,
    ReviewAction,
    RowKind,
    SyntheticProfileRow,
    VerdictRow,
    PARENT_WORTH_PREFIX,
    classify_review_key,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError


class LegacyImportError(StoreError):
    pass


def _text(value: object) -> str | None:
    value = str(value or "").strip()
    return value or None


def _number(value: object) -> float | None:
    try:
        return float(str(value)) if str(value or "").strip() else None
    except ValueError:
        return None


def _array(value: object) -> str | None:
    items = [part for part in str(value or "").split("|") if part]
    return json.dumps(items, separators=(",", ":")) if items else None


def _latest_jsonl(path: Path) -> dict:
    latest: dict = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                latest = item
    except OSError:
        pass
    return latest


def _review_rows(rows: dict[str, dict[str, str]], parent_for: dict[str, str]):
    links: list[LinkRow] = []
    parents: list[ParentRow] = []
    decisions: list[DecisionRow] = []
    errors: list[str] = []
    for key, row in rows.items():
        kind = classify_review_key(key)
        action, approved = _text(row.get("action")), _text(row.get("approved"))
        worth, machine_worth = _text(row.get("network_worth")), _text(row.get("llm_worth"))
        reject = _text(row.get("llm_reject"))
        if action and action not in set(ReviewAction):
            errors.append(f"{key}: unknown action {action!r}")
        if approved and approved not in set(ApprovedState):
            errors.append(f"{key}: unknown approved {approved!r}")
        if worth and worth not in set(HumanWorth):
            errors.append(f"{key}: unknown network_worth {worth!r}")
        if machine_worth and machine_worth not in set(MachineWorth):
            errors.append(f"{key}: unknown llm_worth {machine_worth!r}")
        if reject and reject not in LLM_REJECT_VALUES:
            errors.append(f"{key}: unknown llm_reject {reject!r}")
        if approved and not action:
            errors.append(f"{key}: approved without action")
        source, updated = _text(row.get("source")), _text(row.get("updated_at"))
        if kind is RowKind.PARENT:
            parent_id = key.removeprefix(PARENT_WORTH_PREFIX)
            parents.append(ParentRow(
                parent_id=parent_id,
                public_identifier=_text(row.get("public_identifier")) or key,
                worth_person_ids=_array(row.get("worth_person_ids")),
                llm_worth=machine_worth, llm_worth_reason=_text(row.get("llm_worth_reason")),
                source=source, updated_at=updated))
            if worth:
                decisions.append(DecisionRow(
                    DecisionKind.WORTH.value, parent_id, worth, source=source,
                    note=_text(row.get("user_worth_note")), decided_at=updated))
            continue
        person_id = _text(row.get("person_id"))
        links.append(LinkRow(
            row_key=key, public_identifier=_text(row.get("public_identifier")) or key,
            kind=kind.value, person_id=person_id,
            parent_id=parent_for.get(person_id or "") or parent_for.get(key),
            linkedin_url=_text(row.get("linkedin_url")), proposed_action=action,
            new_linkedin_url=_text(row.get("new_linkedin_url")),
            new_public_identifier=_text(row.get("new_public_identifier")),
            confidence=_number(row.get("confidence")), reason=_text(row.get("reason")),
            match_emails=_array(row.get("match_emails")), match_phones=_array(row.get("match_phones")),
            llm_reject=reject, llm_reject_confidence=_number(row.get("llm_reject_confidence")),
            llm_reject_reason=_text(row.get("llm_reject_reason")),
            llm_judge_fingerprint=_text(row.get("llm_judge_fingerprint")),
            llm_worth=machine_worth, llm_worth_reason=_text(row.get("llm_worth_reason")),
            source=source, updated_at=updated))
        if approved:
            decisions.append(DecisionRow(
                DecisionKind.IDENTITY.value, key, action or "", approved, source, decided_at=updated))
        if worth:
            decisions.append(DecisionRow(
                DecisionKind.WORTH.value, key, worth, source=source,
                note=_text(row.get("user_worth_note")), decided_at=updated))
    if errors:
        raise LegacyImportError("; ".join(errors[:10]))
    return links, parents, decisions


def import_legacy(
    db: Db, *, review_csv: Path, synthetic_csv: Path | None = None,
    index_json: Path | None = None, facts_dir: Path | None = None,
    verdicts_jsonl: Path | None = None, research_dir: Path | None = None,
    manifests: tuple[Path, ...] = (),
) -> dict[str, int]:
    """Absorb legacy files once into an empty canonical DB in one transaction."""
    people_data = batons.people_from_index(index_json) if index_json and index_json.exists() else []
    people = [PersonRow(**row) for row in people_data]
    parent_for = {row.person_id: row.parent_id for row in people}
    links, parents, decisions = _review_rows(batons.load_override_rows(review_csv), parent_for)
    parent_rows = {row.parent_id: row for row in parents}
    members: dict[str, list[str]] = {}
    for person in people:
        members.setdefault(person.parent_id, []).append(person.person_id)
    for parent_id, person_ids in members.items():
        parent_rows.setdefault(parent_id, ParentRow(
            parent_id, parent_id, json.dumps(person_ids, separators=(",", ":"))))

    synthetics: list[SyntheticProfileRow] = []
    for row in batons.load_synthetic_rows(synthetic_csv):
        pub = str(row.get("public_identifier") or "").strip().lower()
        if not pub:
            continue
        parent_id = _text(row.get("source_parent_slug"))
        synthetics.append(SyntheticProfileRow(
            public_identifier=pub, person_id=_text(row.get("id")), parent_id=parent_id,
            linkedin_url=_text(row.get("linkedin_url")),
            name=_text(row.get("full_name")) or " ".join(filter(None, (
                _text(row.get("first_name")), _text(row.get("last_name"))))) or None,
            profile_json=json.dumps(row, separators=(",", ":")),
            source_path=str(synthetic_csv), updated_at=_text(row.get("enriched_at"))))
    gates, gate_errors = batons.read_synthetic_gates(synthetic_csv)
    if gate_errors:
        raise LegacyImportError("; ".join(gate_errors[:10]))
    decisions.extend(gates)

    facts: list[FactRow] = []
    if facts_dir and facts_dir.exists():
        links_by_key = {row.row_key: row for row in links}
        for path in sorted(facts_dir.glob("*.jsonl")):
            payload = _latest_jsonl(path)
            if not payload:
                continue
            subject = path.stem
            projected = payload.get("facts") if isinstance(payload.get("facts"), dict) else {}
            worth = projected.get("network_worth") if isinstance(projected, dict) else {}
            worth = worth if isinstance(worth, dict) else {}
            person_id = subject if subject in parent_for else (
                links_by_key.get(subject).person_id if subject in links_by_key else None)
            parent_id = parent_for.get(person_id or "") or (
                links_by_key.get(subject).parent_id if subject in links_by_key else None) or subject
            facts.append(FactRow(
                subject_key=subject, person_id=person_id, parent_id=parent_id,
                path=str(path), mtime_ns=path.stat().st_mtime_ns,
                llm_worth=_text(worth.get("decision")),
                llm_worth_reason=_text(worth.get("reason")),
                confidence=_number(projected.get("confidence") or payload.get("final_confidence")),
                facts_json=json.dumps(projected, separators=(",", ":")), updated_at=now_iso()))
            parent_rows.setdefault(parent_id, ParentRow(parent_id, subject))

    verdicts: list[VerdictRow] = []
    if verdicts_jsonl and verdicts_jsonl.exists():
        for line in verdicts_jsonl.read_text(encoding="utf-8").splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = _text(payload.get("candidate_key"))
            if not key:
                continue
            judged = payload.get("verdict") if isinstance(payload.get("verdict"), dict) else {}
            verdicts.append(VerdictRow(
                candidate_key=key, parent_id=_text(payload.get("parent_slug")),
                verdict=_text(judged.get("verdict")), confidence=_number(judged.get("confidence")),
                reason=_text(judged.get("reason")),
                fingerprint=_text(payload.get("fingerprint") or payload.get("judge_input_fingerprint")),
                payload_json=json.dumps(payload, separators=(",", ":"))))

    research: list[ResearchRow] = []
    if research_dir and research_dir.exists():
        for directory in sorted(path for path in research_dir.iterdir() if path.is_dir()):
            result_path = next((path for path in (
                directory / "01_research_parallel.json", directory / "00_parallel_raw.json")
                if path.exists()), None)
            result = json.loads(result_path.read_text(encoding="utf-8")) if result_path else {}
            research.append(ResearchRow(
                handle=directory.name, dir_path=str(directory), status=_text(result.get("status")),
                fingerprint=_text((result.get("metadata") or {}).get("fingerprint"))
                if isinstance(result.get("metadata"), dict) else None,
                result_json=json.dumps(result, separators=(",", ":")),
                updated_at=now_iso()))

    stage_rows = []
    for path in manifests:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        stage_rows.append((path.parent.name, json.dumps(payload, separators=(",", ":")), now_iso()))

    counts = {"people": len(people), "parents": len(parent_rows), "links": len(links),
              "decisions": len(decisions), "facts": len(facts), "verdicts": len(verdicts),
              "synthetic_profiles": len(synthetics), "research": len(research),
              "stage_state": len(stage_rows)}
    with db.connect() as conn:
        occupied = [table for table in (
            "people", "parents", "links", "decisions", "facts", "verdicts",
            "synthetic_profiles", "research", "guidance", "jobs", "stage_state")
            if conn.execute(
            f"SELECT 1 FROM {table} LIMIT 1").fetchone()]
        if occupied:
            raise LegacyImportError(f"canonical DB is not empty: {', '.join(occupied)}")
        for table, rows in (("people", people), ("parents", parent_rows.values()),
                            ("links", links), ("decisions", decisions), ("facts", facts),
                            ("verdicts", verdicts), ("synthetic_profiles", synthetics),
                            ("research", research)):
            for row in rows:
                db.put(conn, table, row)
        conn.executemany(
            "INSERT INTO stage_state (stage, state_json, updated_at) VALUES (?, ?, ?)", stage_rows)
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('legacy_imported_at', ?)",
                     (now_iso(),))
    return counts
