"""Diff two LinkedIn-resolution runs (baseline vs +context) into an A/B report.

Reads two ``linkedin_resolutions.csv`` files produced by ``resolve_linkedin_queue``
-- one run WITHOUT the markers context (baseline / original method) and one WITH it
-- joins them by email, and emits a per-person comparison plus summary stats so we
can see whether passing markers context actually changes/improves resolution.

Output (one fixed directory, overwrite in place):
  <out-dir>/ab_comparison.csv   one row per email: baseline vs context, side by side
  <out-dir>/ab_summary.json     outcome counts + average confidence delta
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from packs.shared.csv_io import CsvIO
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.shared.csv_io import CsvIO


def maybe_open(path: Path, do_open: bool) -> None:
    """Open the CSV in the OS default app when --open is set. macOS only and
    best-effort, so headless/CI/remote runs (the default) are never affected."""
    if not do_open or sys.platform != "darwin" or not path.exists():
        return
    try:
        subprocess.run(["open", str(path)], check=False)
    except Exception as exc:  # never let opening a file fail the run
        print(f"[compare_resolution_ab] could not open {path}: {exc}", file=sys.stderr)


def top_mc(candidates_cell: str) -> float | None:
    """Best match_confidence across a run's candidates JSON, or None if absent."""
    try:
        cands = json.loads(candidates_cell or "[]")
    except json.JSONDecodeError:
        return None
    mcs = [c.get("match_confidence") for c in cands if isinstance(c, dict) and isinstance(c.get("match_confidence"), (int, float))]
    return max(mcs) if mcs else None


def load_run(path: Path) -> dict[str, dict[str, Any]]:
    by_email: dict[str, dict[str, Any]] = {}
    for row in CsvIO.dict_reader(path.open(encoding="utf-8")):
        email = str(row.get("email", "")).strip().lower()
        if not email:
            continue
        by_email[email] = {
            "name": row.get("full_name", ""),
            "status": row.get("status", ""),
            "url": (row.get("linkedin_url", "") or "").strip(),
            "mc": top_mc(row.get("candidates", "")),
        }
    return by_email


def classify(b: dict[str, Any], c: dict[str, Any]) -> str:
    b_url, c_url = b.get("url", ""), c.get("url", "")
    if not b_url and c_url:
        return "newly_found"
    if b_url and not c_url:
        return "lost"
    if b_url and c_url and b_url != c_url:
        return "url_changed"
    # same (or no) url -> compare confidence
    bm, cm = b.get("mc"), c.get("mc")
    if bm is not None and cm is not None:
        if cm - bm >= 0.1:
            return "confidence_up"
        if bm - cm >= 0.1:
            return "confidence_down"
    return "unchanged"


def build(args: argparse.Namespace) -> dict[str, Any]:
    baseline = load_run(Path(args.baseline))
    context = load_run(Path(args.context))
    emails = sorted(set(baseline) | set(context))

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "ab_comparison.csv"
    header = [
        "email", "name",
        "baseline_status", "baseline_url", "baseline_mc",
        "context_status", "context_url", "context_mc",
        "mc_delta", "url_changed", "outcome",
    ]
    outcomes: dict[str, int] = {}
    deltas: list[float] = []
    rows_out: list[dict[str, Any]] = []
    for email in emails:
        b = baseline.get(email, {})
        c = context.get(email, {})
        outcome = classify(b, c)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        bm, cm = b.get("mc"), c.get("mc")
        delta = round(cm - bm, 3) if (bm is not None and cm is not None) else ""
        if isinstance(delta, float):
            deltas.append(delta)
        rows_out.append({
            "email": email,
            "name": c.get("name") or b.get("name", ""),
            "baseline_status": b.get("status", ""),
            "baseline_url": b.get("url", ""),
            "baseline_mc": "" if b.get("mc") is None else b.get("mc"),
            "context_status": c.get("status", ""),
            "context_url": c.get("url", ""),
            "context_mc": "" if c.get("mc") is None else c.get("mc"),
            "mc_delta": delta,
            "url_changed": str(bool(b.get("url") and c.get("url") and b.get("url") != c.get("url"))).lower(),
            "outcome": outcome,
        })

    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows_out)

    found = lambda run: sum(1 for v in run.values() if v.get("url"))  # noqa: E731
    summary = {
        "source": "compare_resolution_ab",
        "status": "completed",
        "people_total": len(emails),
        "baseline_found": found(baseline),
        "context_found": found(context),
        "net_new_found": outcomes.get("newly_found", 0) - outcomes.get("lost", 0),
        "outcomes": outcomes,
        "avg_mc_delta": round(sum(deltas) / len(deltas), 3) if deltas else None,
        "output_csv": str(csv_path),
    }
    (out_dir / "ab_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    maybe_open(csv_path, args.open)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Diff baseline vs +context LinkedIn resolution into an A/B report.")
    parser.add_argument("--baseline", required=True, help="linkedin_resolutions.csv from the no-context run")
    parser.add_argument("--context", required=True, help="linkedin_resolutions.csv from the +context run")
    parser.add_argument("--out-dir", default=".powerpacks/network-import/discover/email-context/ab", help="Output directory")
    parser.add_argument("--open", action="store_true", help="Open ab_comparison.csv when done (macOS, interactive; off by default for headless runs)")
    return parser


def main(argv: list[str] | None = None) -> int:
    summary = build(build_parser().parse_args(argv))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
