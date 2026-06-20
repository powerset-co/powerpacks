"""Build LinkedIn-resolution queue CSVs from inferred markers (local, free).

This is the bridge between the marker step and the A/B resolution step of
``$enrich-email-markers``. It reads ``markers.jsonl`` (produced by
``infer_linkedin_markers``) and writes TWO queue CSVs over the SAME contacts:

  queue_control.csv   -- context column blank  (the "without markers" arm)
  queue_context.csv   -- context column filled with the markers we mined
                         (the "with markers" arm)

Running ``resolve_linkedin_queue`` on each and diffing them with
``compare_resolution_ab`` isolates exactly one variable -- whether the mined
markers were attached as context -- so the lift is attributable to the markers.

Both files carry identical ``full_name``/``email``/``primary_email_type`` rows;
``resolve_linkedin_queue`` derives ``company`` from the email domain itself, so
the only difference between the arms is the ``context`` string.

Local-only: reads a local JSONL, writes local CSVs + a manifest. No spend.

Outputs (one fixed directory, overwrite in place):
  <out-dir>/queue_control.csv
  <out-dir>/queue_context.csv
  <out-dir>/manifest.json
"""
import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

_PRIMITIVES_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PRIMITIVES_DIR / "gmail_network_import"))

import gmail_network_import as gni  # noqa: E402

DEFAULT_MARKERS = Path(".powerpacks/network-import/discover/email-context/markers/markers.jsonl")
DEFAULT_OUT_DIR = Path(".powerpacks/network-import/discover/email-context/ab")
QUEUE_COLUMNS = ["full_name", "email", "primary_email_type", "context"]

# Marker categories to fold into the context string, in priority order. Each
# becomes "label: value; value" so the resolver sees the same facts a human would.
CONTEXT_CATEGORIES = [
    ("employers", "employers"),
    ("job_title", "title"),
    ("school", "school"),
    ("field_of_study", "field of study"),
    ("location", "location"),
    ("professional_affiliation", "affiliations"),
    ("online_identifier", "handles/links"),
    ("industry", "industry"),
]


def compose_context(markers_obj: dict[str, Any]) -> str:
    """Build the context string from a marker record's schema object.

    Leads with the high-signal facts (employer/title/school/...), then appends
    the model's own ``linkedin_query`` as a search hint. Empty when the contact
    has no usable markers."""
    by_cat: dict[str, list[str]] = {}
    for mk in (markers_obj.get("markers") or []):
        cat = mk.get("category")
        val = str(mk.get("value") or "").strip()
        if cat and val:
            by_cat.setdefault(cat, []).append(val)
    parts: list[str] = []
    for cat, label in CONTEXT_CATEGORIES:
        vals = by_cat.get(cat)
        if vals:
            parts.append(f"{label}: {'; '.join(vals)}")
    query = str(markers_obj.get("linkedin_query") or "").strip()
    if query:
        parts.append(f"search hint: {query}")
    return " | ".join(parts)


def build_rows(markers_path: Path) -> list[dict[str, str]]:
    """One queue row per marker record that is actually resolvable (has context)."""
    rows: list[dict[str, str]] = []
    for line in markers_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        m = rec.get("markers") or {}
        email = str(rec.get("email") or "").strip().lower()
        if not email:
            continue
        context = compose_context(m)
        # Skip contacts with no usable signal — they would only burn Parallel
        # credits and resolve_linkedin_queue filters most of them anyway.
        if not context:
            continue
        full_name = str(m.get("canonical_name") or rec.get("full_name") or "").strip()
        rows.append({
            "full_name": full_name,
            "email": email,
            "primary_email_type": str(rec.get("primary_email_type") or "").strip(),
            "context": context,
        })
    return rows


def write_queue(path: Path, rows: list[dict[str, str]], with_context: bool) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=QUEUE_COLUMNS)
        writer.writeheader()
        for row in rows:
            out = dict(row)
            if not with_context:
                out["context"] = ""
            writer.writerow(out)


def build_queues(args: argparse.Namespace) -> dict[str, Any]:
    markers_path = Path(args.markers)
    if not markers_path.is_file():
        raise SystemExit(
            f"No markers at {markers_path}. Run step 2 (infer_linkedin_markers) first."
        )
    rows = build_rows(markers_path)
    if not rows:
        raise SystemExit(
            f"No resolvable contacts with markers in {markers_path}. "
            "Nothing to queue for resolution."
        )
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    control_path = out_dir / "queue_control.csv"
    context_path = out_dir / "queue_context.csv"
    write_queue(control_path, rows, with_context=False)
    write_queue(context_path, rows, with_context=True)

    manifest = {
        "source": "build_resolution_queue",
        "status": "completed",
        "markers": str(markers_path),
        "contacts_queued": len(rows),
        "queue_control": str(control_path),
        "queue_context": str(context_path),
        "updated_at": gni.now_iso(),
        "privacy": {"network_called": False, "llm_called": False, "local_only": True},
    }
    manifest_path = out_dir / "manifest.json"
    gni.write_json(manifest_path, manifest)
    manifest["manifest"] = str(manifest_path)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build A/B LinkedIn-resolution queues from markers (local).")
    parser.add_argument("--markers", default=str(DEFAULT_MARKERS), help="markers.jsonl from infer_linkedin_markers")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="Output directory for the two queue CSVs")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = build_queues(args)
    gni.emit(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
