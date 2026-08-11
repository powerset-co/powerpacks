"""Frozen result and progress rows for one Parallel research run."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.manifests.receipt_counts import ReceiptCounts
from packs.ingestion.primitives.deep_context.shared.coerce import boolean, json_array, number, text
from packs.ingestion.primitives.deep_context.enrich.parallel_research import config
from packs.ingestion.primitives.deep_context.enrich.parallel_research.queue import ResearchQueueRow


@dataclass(frozen=True)
class ResearchRunCounts:
    run_ids: int
    results_fetched: int
    errors: int
    real_name_found: int
    linkedin_found: int


@dataclass(frozen=True)
class ResearchProgress:
    status: str
    counts: ReceiptCounts

    @property
    def completed(self) -> int:
        return self.counts.completed

    def to_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "counts": {
                "total": self.counts.total,
                "completed": self.counts.completed,
                "pending": self.counts.pending,
                "failed": self.counts.failed,
            },
        }


@dataclass(frozen=True)
class ResearchRunParams:
    """One explicit configuration door for an in-process research pass."""

    output_dir: Path
    db: Db
    rows: tuple[ResearchQueueRow, ...] = ()
    processor: str = config.DEFAULT_PROCESSOR
    selection_fingerprint: str | None = None
    manifest: Path | None = None
    api_key: str | None = None
    base_url: str = config.DEFAULT_BASE_URL
    beta_header: str = config.DEFAULT_BETA_HEADER
    batch_size: int = config.DEFAULT_BATCH_SIZE
    poll_interval: int = config.DEFAULT_POLL_INTERVAL
    max_wait: int = config.DEFAULT_MAX_WAIT
    api_timeout: int = 60
    on_progress: Callable[[ResearchProgress], None] | None = None
    # False when a caller (research_reconcile) already owns the on-disk receipt
    # and only wants progress projected into the DB, not double-written to a
    # second manifest.json.
    owns_receipt: bool = True

    def __post_init__(self) -> None:
        if self.manifest is not None and self.manifest.name != "manifest.json":
            raise SystemExit("--manifest must end in manifest.json")


@dataclass(frozen=True)
class ResearchRunResult:
    """One provider run after its raw SDK payload has been parsed."""

    status: str
    error: str | None = None
    queue_rows: int | None = None
    skipped_already_done: int | None = None
    output_dir: str | None = None
    counts: ResearchRunCounts | None = None
    errors: tuple[str, ...] = ()

    @classmethod
    def failed(cls, error: str) -> ResearchRunResult:
        return cls("failed", error=error)


# Terminal statuses where the already-completed portion of a run is usable —
# unlike "failed", where nothing in this pass completed. completed_with_errors
# fires whenever ANY handle in the batch errored, including the benign case at
# parallel_client.py where a completed run just came back without its
# metadata.handle: the rest of the batch still succeeded and billed, so a
# caller must still consume it, not discard the whole pass. Both
# research_reconcile.coordinator and identity_reconcile.guided gate on this
# set — import it from here, don't respell it.
RESEARCH_OK_STATUSES = frozenset({"no_work", "completed", "completed_with_errors"})


@dataclass(frozen=True)
class ParallelPosition:
    title: str | None
    company_name: str | None
    company_domain: str | None = None
    description: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    is_current: bool = False
    confidence: float = 0.7
    sources: tuple[str, ...] = ()

    @classmethod
    def from_payload(cls, payload: object) -> ParallelPosition | None:
        """Parse one work_experience entry from the provider's output JSON.

        Example: {"title": "Engineer", "company": "Acme Robotics",
        "start_date": "2020-01", "current": true}. A bare string becomes a
        low-confidence company-only row instead of being dropped.
        """
        if isinstance(payload, str):
            return cls(None, payload, confidence=0.5)
        if not isinstance(payload, dict):
            return None
        evidence = payload.get("evidence")
        if isinstance(evidence, list):
            sources = tuple(str(value) for value in evidence)
        else:
            sources = (str(payload["source"]),) if payload.get("source") else ()
        return cls(
            text(payload.get("title") or payload.get("position")),
            next(
                (
                    str(payload[key])
                    for key in ("company", "organization", "employer", "company_name", "name")
                    if payload.get(key)
                ),
                None,
            ),
            text(payload.get("domain") or payload.get("company_domain")),
            text(payload.get("description")),
            text(payload.get("start_date")),
            text(payload.get("end_date")),
            boolean(payload.get("current") or payload.get("is_current", False)),
            number(payload.get("confidence"), 0.7),
            sources,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "title": self.title or "",
            "company_name": self.company_name or "",
            "company_domain": self.company_domain,
            "company_linkedin_url": None,
            "description": self.description,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "is_current": self.is_current,
            "confidence": self.confidence,
            "sources": list(self.sources),
        }


@dataclass(frozen=True)
class ParallelEducation:
    school_name: str | None
    degree: str | None = None
    field_of_study: str | None = None
    start_year: str | None = None
    end_year: str | None = None
    confidence: float = 0.7
    source: str | None = None

    @classmethod
    def from_payload(cls, payload: object) -> ParallelEducation | None:
        """Parse one education entry from the provider's output JSON.

        Example: {"school": "State University", "degree": "BS",
        "field_of_study": "Computer Science", "end_year": "2018"}.
        """
        if isinstance(payload, str):
            return cls(payload, confidence=0.5)
        if not isinstance(payload, dict):
            return None
        return cls(
            next(
                (
                    str(payload[key])
                    for key in ("school", "school_name", "institution", "university", "name")
                    if payload.get(key)
                ),
                None,
            ),
            text(payload.get("degree")),
            text(payload.get("field") or payload.get("field_of_study")),
            text(payload.get("start_year")),
            text(payload.get("end_year")),
            number(payload.get("confidence"), 0.7),
            text(payload.get("evidence")),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "school_name": self.school_name or "",
            "degree": self.degree,
            "field_of_study": self.field_of_study,
            "start_year": self.start_year,
            "end_year": self.end_year,
            "confidence": self.confidence,
            "source": self.source or "",
        }


@dataclass(frozen=True)
class ParallelProviderResult:
    """One raw Parallel payload after its provider-boundary parse."""

    real_name: str | None
    name_confidence: float
    name_evidence: str | None
    location_city: str | None
    location_country: str | None
    linkedin_url: str | None
    github_url: str | None
    personal_website: str | None
    summary: str | None
    research_notes: str | None
    positions: tuple[ParallelPosition, ...]
    education: tuple[ParallelEducation, ...]
    raw_position_count: int
    raw_education_count: int
    _payload_json: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ParallelProviderResult:
        """Parse one completed run's output content — the paid result, once.

        Example: {"real_name": "Jordan Bravo", "name_confidence": 0.8,
        "linkedin_url": "https://linkedin.com/in/jbravo",
        "work_experience": [...], "education": [...]}. Every field is
        optional; a run that found nothing still parses to an all-None
        result rather than raising, so a thin provider answer surfaces as
        low completeness (see `gaps`/`completeness` below), not an error.
        """
        raw_positions = json_array(payload.get("work_experience"))
        raw_education = json_array(payload.get("education"))
        positions = tuple(row for value in raw_positions if (row := ParallelPosition.from_payload(value)) is not None)
        education = tuple(row for value in raw_education if (row := ParallelEducation.from_payload(value)) is not None)
        return cls(
            text(payload.get("real_name")),
            number(payload.get("name_confidence"), 0.3),
            text(payload.get("name_evidence")),
            text(payload.get("location_city")),
            text(payload.get("location_country")),
            text(payload.get("linkedin_url")),
            text(payload.get("github_url")),
            text(payload.get("personal_website")),
            text(payload.get("summary")),
            text(payload.get("research_notes")),
            positions,
            education,
            len(raw_positions),
            len(raw_education),
            json.dumps(payload, ensure_ascii=False),
        )

    def to_payload(self) -> dict[str, Any]:
        return json.loads(self._payload_json)

    @property
    def completeness(self) -> float:
        score = 0.3 if self.real_name else 0.0
        score += min(0.3, self.raw_position_count * 0.1)
        score += min(0.2, self.raw_education_count * 0.1)
        score += 0.1 if self.location_city else 0.0
        score += 0.1 if self.linkedin_url else 0.0
        return round(min(1.0, score), 2)

    @property
    def gaps(self) -> tuple[str, ...]:
        return tuple(
            label
            for missing, label in (
                (not self.real_name, "Real name not identified"),
                (not self.raw_position_count, "No work experience found"),
                (not self.raw_education_count, "No education found"),
                (
                    not self.location_city and not self.location_country,
                    "Location unknown",
                ),
                (not self.linkedin_url, "No LinkedIn profile found"),
            )
            if missing
        )


@dataclass(frozen=True)
class ParallelRunInput:
    task_spec: object
    _input_json: str
    handle: str
    processor: str

    @classmethod
    def from_payload(
        cls,
        task_spec: object,
        input_payload: dict[str, Any],
        handle: str,
        processor: str,
    ) -> ParallelRunInput:
        return cls(task_spec, json.dumps(input_payload, ensure_ascii=False), handle, processor)

    def to_payload(self) -> dict[str, Any]:
        """One element of the list parallel_client.execute() submits to add_runs().

        Example: {"task_spec": {...pinned TASK_SPEC...},
        "input": {"handle": "jbravo", "dossier": "Name: Jordan Bravo\\n..."},
        "metadata": {"handle": "jbravo"}, "processor": "core2x"}. `metadata.handle`
        is how a completed run is matched back to its ResearchQueueRow in
        ParallelClient.execute — an unrecognized handle becomes an error string,
        not a dropped result.
        """
        return {
            "task_spec": self.task_spec,
            "input": json.loads(self._input_json),
            "metadata": {"handle": self.handle},
            "processor": self.processor,
        }


@dataclass(frozen=True)
class ProviderStatusCounts:
    """One poll's task_run_status_counts. The synonym fields (succeeded/success,
    error/errored, cancelled/canceled) exist because the field the SDK actually
    returns has drifted across releases; completed_total/failed_total take the
    first non-zero variant (precedence, not a sum) so a version bump that reports
    two synonyms for the same count at once can't double it.
    """

    completed: int = 0
    succeeded: int = 0
    success: int = 0
    failed: int = 0
    error: int = 0
    errored: int = 0
    cancelled: int = 0
    canceled: int = 0
    _payload_json: str = "{}"

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ProviderStatusCounts:
        def count(key: str) -> int:
            return int(payload.get(key) or 0)

        return cls(
            count("completed"),
            count("succeeded"),
            count("success"),
            count("failed"),
            count("error"),
            count("errored"),
            count("cancelled"),
            count("canceled"),
            json.dumps(payload, ensure_ascii=False),
        )

    @property
    def completed_total(self) -> int:
        return self.completed or self.succeeded or self.success

    @property
    def failed_total(self) -> int:
        return self.failed or self.error or self.errored or self.cancelled or self.canceled

    def to_payload(self) -> dict[str, Any]:
        return json.loads(self._payload_json)


@dataclass(frozen=True)
class ProviderGroupStatus:
    # None means the payload didn't carry a boolean is_active — parallel_client's
    # poll loop treats that the same as "still active" (it only stops on
    # is_active is False), so a malformed status payload just costs another
    # poll, not a crash.
    is_active: bool | None
    task_counts: ProviderStatusCounts
    _payload_json: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ProviderGroupStatus:
        counts = payload.get("task_run_status_counts")
        return cls(
            payload.get("is_active") if isinstance(payload.get("is_active"), bool) else None,
            ProviderStatusCounts.from_payload(counts if isinstance(counts, dict) else {}),
            json.dumps(payload, ensure_ascii=False),
        )

    @classmethod
    def empty(cls) -> ProviderGroupStatus:
        return cls.from_payload({})

    def to_payload(self) -> dict[str, Any]:
        return json.loads(self._payload_json)


@dataclass(frozen=True)
class ParallelExecutionResult:
    # Runs actually created by add_runs(), i.e. already billed — not len(inputs).
    # If a batch call raises partway through submission, the run_ids from earlier
    # batches in the same execute() call are still billed but never reach this
    # dataclass (the exception propagates out of execute() first); see driver.py.
    run_count: int
    results: tuple[tuple[str, ParallelProviderResult], ...]
    errors: tuple[str, ...]
    final_status: ProviderGroupStatus
