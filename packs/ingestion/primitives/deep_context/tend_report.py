"""[tend, free] One JSON report closing the weekly self-heal pass.

Aggregates, in-process and without spending anything, the three numbers the
tend flow parks for a human instead of running:

  resynthesis   what `synthesize` would spend right now (the --dry-run
                estimate, computed via the same `estimate()` the CLI uses)
  web_resolve   what the $0 websearch pass just did (its manifest, verbatim)
  parallel_tail what the paid Parallel.ai escalation would cost for the
                people web-resolve could not crack (same eligible_subset +
                core2x pricing the deep-research node uses)

Report only: prints one JSON object, writes nothing. The paid actions it
prices are run separately with their own estimates and approval gates
(`bin/deep-context synthesize`, `reconcile-deep-research --approve --budget`).

Changelog:
  2026-08-02: new primitive for `bin/deep-context tend`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.common import (
    emit,
    read_jsonl,
    VERDICTS_JSONL,
    LINKEDIN_OVERRIDES_CSV,
)
from packs.ingestion.primitives.deep_context.deep_research_contacts import (
    PROCESSOR_PRICING_USD,
)
from packs.ingestion.primitives.deep_context.reconcile_deep_research import (
    DEFAULT_PROCESSOR,
    eligible_subset,
)
from packs.ingestion.primitives.deep_context.reconcile_linkedin import (
    load_override_rows,
)
from packs.ingestion.primitives.deep_context.review_store import (
    RESEARCH_CONFIRM_THRESHOLD,
)
from packs.ingestion.primitives.deep_context.synthesize_person_context import (
    SynthesizePersonContext,
)
from packs.ingestion.primitives.deep_context.web_resolve import WR_MANIFEST


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def build_report(*, verdicts_jsonl: Path = VERDICTS_JSONL,
                 overrides_csv: Path = LINKEDIN_OVERRIDES_CSV,
                 web_manifest: Path = WR_MANIFEST) -> dict[str, Any]:
    verdicts = list(read_jsonl(verdicts_jsonl))
    overrides = load_override_rows(overrides_csv)
    tail = eligible_subset(verdicts, RESEARCH_CONFIRM_THRESHOLD, overrides)
    cost_per = PROCESSOR_PRICING_USD[DEFAULT_PROCESSOR]
    web = _read_json(web_manifest) or {"status": "never_ran"}
    proposable = int((web.get("counts") or {}).get("proposable") or 0)
    return {
        "source": "tend_report",
        "status": "completed",
        "resynthesis": SynthesizePersonContext().estimate(),
        "web_resolve": web,
        "web_resolve_propose": {
            "proposable": proposable,
            "action": "bin/deep-context web-resolve --propose",
            "note": "OpenAI identity judge, roughly a cent or two per find; "
                    "fingerprint-cached so re-runs are $0",
        },
        "parallel_tail": {
            "people": len(tail),
            "processor": DEFAULT_PROCESSOR,
            "cost_per_person_usd": cost_per,
            "estimated_usd": round(len(tail) * cost_per, 2),
        },
        "updated_at": now_iso(),
    }


def main() -> int:
    emit(build_report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
