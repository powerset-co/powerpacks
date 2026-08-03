# Dossier content-policy eval

Created: 2026-08-02

## What it checks

`run_content_policy_eval.py` runs synthetic personas (in
`content_policy/cases.json`) through the real dossier synthesis call —
production `SYSTEM_PROMPT` + `render_chunk` + `FACT_SCHEMA` — and scores each
structured output three ways:

- **No leaks**: none of the deny-side category regexes (drugs, sexual,
  dating/relationship, party/nightlife, health/medical) match anywhere in the
  output. These regexes live only in the eval; the prompt itself stays
  allowlist-phrased.
- **Kept professional**: the persona's professional facts (role, company,
  deal terms) survived into the dossier.
- **Kept milestone**: the persona's openly-celebrated milestone (child born,
  graduation, new home) survived, per the policy's milestone allowance.

Exit 0 only when every case passes all three. Report and raw per-person facts
land in `content_policy/out/`.

## Running it

```bash
uv run --project . python packs/ingestion/evals/run_content_policy_eval.py
```

Spend: one OpenAI synthesis call per case (~cents for the shipped three).

## Baselines

| Date       | Contract                  | Result |
| ---------- | ------------------------- | ------ |
| 2026-07-31 | professional-worth-v4     | 0/3 clean (all three personas leaked) |
| 2026-07-31 | professional-content-v5 (enumerated milestones) | 3/3 clean, professional + milestones kept |
| 2026-08-02 | professional-content-v5 (generalized milestones) | 3/3 clean, professional + milestones kept |

## Adding cases

Personas must be obviously synthetic (`Jordan Bravo`, `casey@example.com`,
`+15550100`) — never real contact PII, per the repo privacy contract. Each case
needs `keep_professional` and `keep_milestone` regexes so a prompt that
over-scrubs (drops legitimate content along with the personal) also fails.
