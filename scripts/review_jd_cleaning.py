"""Write local before/after JD previews without reading candidate labels."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
from pathlib import Path

from packs.indexing.lib import job_descriptions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--edits", type=Path, help="Reviewed JSON removal plans; no model calls")
    args = parser.parse_args()
    source = args.dataset.read_bytes()
    jobs = json.loads(source)["jobs"]
    plans = {}
    if args.edits:
        plan_rows = json.loads(args.edits.read_text())
        plans = {row["jd_id"]: row for row in plan_rows}
        if len(plans) != len(plan_rows) or set(plans) != {job["jd_id"] for job in jobs}:
            raise ValueError("Reviewed edits must cover each source JD exactly once")
    rows = []
    for index, job in enumerate(jobs):
        original = job.get("original_text") or job["text"]
        before = job_descriptions.clean_description(original)
        deterministic = job_descriptions.focused_description(original)
        plan = plans.get(job["jd_id"])
        after = job_descriptions.focused_description(original, **(
            {"removal_quotes": plan["remove_quotes"], "source_sha256": plan["source_sha256"]}
            if plan else {}
        ))
        if not after or (not plan and job_descriptions.focused_description(after) != after):
            raise ValueError(f"Empty or non-idempotent output: {job['jd_id']}")
        # The cleaner may remove text/normalize whitespace, never invent words.
        remaining = iter("".join(before.split()))
        if not all(any(word == original_word for original_word in remaining) for word in "".join(after.split())):
            raise ValueError(f"Output is not a source-character subsequence: {job['jd_id']}")
        rows.append({"index": index, "jd_id": job["jd_id"], "title": job["title"],
                     "before": before, "deterministic": deterministic, "after": after,
                     "removed_chars": len(before) - len(after)})
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "outputs.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False))
    (args.output_dir / "review.md").write_text("\n\n".join(
        f"## {row['index']}: {row['title']}\n\n```diff\n" + "\n".join(difflib.unified_diff(
            row["before"].splitlines(), row["after"].splitlines(),
            fromfile="before", tofile="after", lineterm="")) + "\n```" for row in rows))
    summary = {"jobs": len(rows), "changed": sum(row["before"] != row["after"] for row in rows),
               "before_chars": sum(len(row["before"]) for row in rows),
               "after_chars": sum(len(row["after"]) for row in rows),
               "source_sha256": hashlib.sha256(source).hexdigest(),
               "cleaner_sha256": hashlib.sha256(Path(job_descriptions.__file__).read_bytes()).hexdigest(),
               "source_character_subsequence": "passed",
               "deterministic_idempotence": "not applied to semantic output" if plans else "passed",
               "edits_sha256": hashlib.sha256(args.edits.read_bytes()).hexdigest() if args.edits else None,
               "scope": "JD text only; no labels, profiles, pond queries or training snapshots modified"}
    (args.output_dir / "manifest.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
