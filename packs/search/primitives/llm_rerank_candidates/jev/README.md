# Jev capability judge

Predict whether a person meets the rating-3-or-above capability bar for a JD.
Jev stays frozen; a small trained tree ensemble combines its answers. This is a
qualification screen. Location, willingness to join, and compensation remain
separate judgments.

```mermaid
flowchart LR
    A[JD + original profile and company evidence] --> B[Jev: 18 shared questions + 3 per role]
    B --> C[87 probability and career features]
    C --> D[Five frozen boosted-tree models]
    D --> E[Mean score >= 0.29855554570561965]
    E --> F[Pass to downstream judges]
```

## Selection

Set `TYPESAFE_API_KEY` for Jev and `OPENAI_API_KEY` for the one-time Sol JD
structuring call. The cleaner is shared by both judges and cached across ponds;
the raw JD remains available to retrieval and later logistics review.
Keys never go in requests saved to disk.
Luna capability scoring remains the default until the caller explicitly selects Jev:

```bash
uv run python packs/search/primitives/llm_rerank_candidates/llm_rerank_candidates.py \
  --state /path/to/state.json --write-state \
  --jd-file /path/to/jd.txt --job-title 'Backend Engineer' \
  --job-company 'Example Systems' --capability-judge jev
```

`--capability-judge terra` retains its CLI name and selects Luna/low using `OPENAI_API_KEY`.
The same selector is accepted by pipeline `prepare`/`run` and
`search_harness.py run-pond`. Changing the judge, JD, job metadata, evaluation
criteria, or cleaner/scorer contract reruns scoring and export while retaining
completed retrieval/filter work. Use `--force-llm` to repeat unchanged scoring.
Use `--dry-run` on the reranker to inspect the
structuring request without spending. Capability requests are built afterward
from the validated structured output.

## Evaluated operating point

Frozen pilot models and cutoffs, evaluated on 26,424 pairs across 27 jobs after
excluding pilot people. Reference labels are Luna ratings >=3, not an independent
human assessment of recruiting correctness.

| Readout | Precision | Recall | F1 |
|---|---:|---:|---:|
| **Selected Jev tree ensemble** | **73.2%** | **93.2%** | **82.0%** |
| Experimental logistic combiner | 83.8% | 83.8% | 83.8% |
| Direct Jev rating probabilities | 77.6% | 87.6% | 82.3% |

The tree operating point intentionally favors recall: about 27% of passed
candidates disagree with Luna, and about 7% of Luna-positive candidates are missed.
Results vary by job. The logistic alternative remains an experimental artifact;
this package includes the selected tree model only.

The broad run scored 26,990 of 27,000 pairs; ten failed requests remain unscored.
The quoted aggregate metrics use the older cleaned JD view and capability rubric.
The structured JD and concise rubric change the inputs and need their own evaluation;
these metrics are not a measured claim about the new inputs.

## Exploratory audit checks

The slim 18-plus-three-per-role request exactly replayed cached predictions for
all 320 historical cases after removing the two unused answers. In a separate
live 40-case reference sample, full-versus-slim decisions agreed on 37 cases;
two identical slim requests also agreed on 37. Mean absolute score differences
were 0.0197 between full and slim and 0.0309 between the repeated slim requests,
so live outputs are subject to API variance and are not bit-identical. Saved
input tokens fell from 430,431 to 417,591, about 3.0%.

An exploratory company-evidence check covered 320 historical cases across eight
engineering JDs. On the 200-case control, precision/recall/F1 moved from
71.4%/89.3%/79.4% to 72.9%/91.1%/81.0%, with two decision improvements. On 120
selected near-boundary cases, F1 fell from 54.0% to 48.0%. Source-only evidence
made the same control decisions as sources plus 72 Jev company judgments, so the
extra judgments have no established benefit. All 230 queued research items
completed without a failed paid call; identity guards applied evidence to 90
cases covering 70 companies. Estimated total spend was $1.654.

These checks are exploratory: labels are Luna ratings, person inputs are
historical while company evidence is current, and live calls vary. This PR does
not enable company enrichment in the runtime path.

## Contract and reproducibility

- Pinned API model: `jev-1.13.0`. One request per candidate, with shared context
  and independent questions; four requests may run concurrently.
- Features include function/execution evidence, recency and repeated experience,
  domain, company quality, stage/size, and funding. The selected request omits
  the exploratory school-reputation and direct-overall-rating questions because
  neither feeds the model or displayed evidence. Historical responses containing
  those answers still replay through the same 87-feature model.
- Five person-fold boosted-tree estimators, each with 100 depth-2 trees. The
  included model JSON contains weights and feature names, no training identities.
- Native output is `qualification_score` in [0,1], plus `threshold` and `passed`.
  It is not a 1–5 rating or a verified probability of qualification. Persistence,
  the results viewer, and downstream judgment eligibility retain this distinction.
- Request caching binds evidence, date, rubric, questions, and API model.
  The request version identifies the slim production question set; the model's
  question version remains its training provenance. Feature/model metadata bind
  the classifier to its expected 87-feature schema.
  The assessment date is part of the key, so a new date triggers new scoring.
  Paid HTTP 200 responses checkpoint before validation; malformed output remains
  unscored and is never silently repaid. Failed or oversized requests never
  become rejections.
- Uncached calls append local token usage and explicit TypeSafe cost to the
  shared `POWERPACKS_USAGE_LOG`; cached reads do not add spend. The current
  published rate is $0.042 per million input tokens and output tokens are free.
- CPU prediction matches the experimental ensemble on all 26,424 saved feature
  rows to floating-point precision (maximum absolute error below 4e-16).

## Company evidence

The existing company-taste system predicts investment/company preferences; its
separate résumé-tier score is a founder prior. Neither is a validated JD-specific
talent rating. Monitoring's round radar estimates fundraising activity, not
candidate quality. Those scores are not silently injected into this model.

A future company enrichment should identify the relevant employer and period,
record domain, scale, funding dates and source provenance, then assess talent
strength for the job's function. An unfamiliar employer remains unknown. Founded
date alone is not a talent score. Adding such fields or questions requires a new
feature/model version and evaluation on the same separated people and jobs.

## Files

| File | Responsibility |
|---|---|
| `client.py` | API requests, validation, caching, and candidate scores |
| `questions.py` | Frozen question definitions and role evidence |
| `features.py` | Exact ordered 87-feature construction |
| `model.py` | Validated, dependency-free tree inference |
| `model.json` | Frozen model parameters and provenance |
| `__init__.py` | Public scoring interface |
