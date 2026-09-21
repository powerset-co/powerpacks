# JD scoring cost replay — 2026-09-17

The experimental configuration reduces estimated per-search cost from $12.12 to
approximately $4.93 on one frozen ML-performance JD. It retains 16/17 baseline
scores of 4–5 and 222/240 baseline scores of 3+. These are prior model ratings,
not human labels or an accuracy guarantee.

The PR keeps the original first-stage filter unchanged. The $4.93 experiment
below included compact-filter changes that are not shipping; cost and retention
for the revised end-to-end configuration have not been measured.

| Stage | Candidates | Represented API cost |
| --- | ---: | ---: |
| Luna/none original-evidence filter | 5,371 | $1.511205 |
| Luna/low concise capability screen | 1,349 | $0.482653 |
| Terra/medium qualifications | 503 | $2.189254 |
| Luna/low opportunity | 503 | $0.297017 |
| Terra/medium recheck of opportunity cap 2 | 67 | $0.443869 |
| Filter and scoring total | | $4.923997 |

Unchanged preparation adds approximately $0.007. All model stages use Flex.
Historical unchanged Terra votes were reused and their token costs included in
the estimate; the replay was not a fresh end-to-end run. Original judge usage
omitted less than $0.001 of cache-write surcharges. Completion tokens already
include reasoning. New API spend across all experimental variants was $10.27.

## What changed

The JD, approved pond query, and first-stage filter are unchanged, including
filter evidence, batching, prompts, and error handling. The CE-style capability
scorer already excludes generated dense text; it retains original titles,
descriptions, and full work history. Ranking and judge errors never become
fabricated positive or negative final scores.

The capability rubric is distilled, with the positive score-3 threshold,
transferable experience, specialty evidence, and recency rules preserved.
Qualifications remain Terra/medium. Opportunity uses Luna/low with explicit
guidance separating technical leadership from people management. A Luna cap 2
receives an independent Terra judgment without seeing Luna's answer; that
judgment replaces the opportunity decision. Both calls and costs are recorded.
Final scoring remains `min(qualifications, opportunity cap)`.

## Limitations

The compact filter preserved all 240 baseline 3+ candidates. The final result
has 19 scores of 4–5 and 284 scores of 3+: 222 were previously 3+, 61 were lower,
and one newly admitted candidate has no baseline score. Higher retention thus
comes with more borderline introductions. Eighteen baseline 3+ candidates fail
the new capability gate; one baseline top candidate receives opportunity cap 3.

All-Luna scoring retained only 14/17 top candidates and 141/240 baseline 3+
candidates with the original capability prompt. Lowering Terra reasoning alone
did not approach $5. The main useful changes were input deduplication and using
Terra selectively, not merely shortening instructions.

The configuration was tuned and selected on this JD. Another untouched JD is
needed to measure generalization. Private profiles, raw responses, and review
labels remain outside the repository; committed tests use synthetic people.
