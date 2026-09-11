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
    args = parser.parse_args()
    source = args.dataset.read_bytes()
    rows = []
    for index, job in enumerate(json.loads(source)["jobs"]):
        original = job.get("original_text") or job["text"]
        before = job_descriptions.clean_description(original)
        after = job_descriptions.focused_description(original)
        if not after or job_descriptions.focused_description(after) != after:
            raise ValueError(f"Empty or non-idempotent output: {job['jd_id']}")
        # The cleaner may remove text/normalize whitespace, never invent words.
        remaining = iter("".join(before.split()))
        if not all(any(word == original_word for original_word in remaining) for word in "".join(after.split())):
            raise ValueError(f"Output is not a source-character subsequence: {job['jd_id']}")
        rows.append({"index": index, "jd_id": job["jd_id"], "title": job["title"],
                     "before": before, "after": after, "removed_chars": len(before) - len(after)})
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
               "idempotence_and_source_character_subsequence": "passed",
               "scope": "JD text only; no labels, profiles, pond queries or training snapshots modified"}
    (args.output_dir / "manifest.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
