# Contact recovery

Use the existing SQLite store and source artifacts. Preserve original contact
facts and explicit human decisions; a seeded machine merge remains a machine
decision. Keep a golden copy before replaying changes; do not introduce another
ledger. A cold rebuild on a separate copy can reuse facts without inheriting
the old family membership.

## Inspect before changing identity

Run `bin/deep-context audit --db <store>` and inspect the affected parents,
children, identifiers, facts, artifacts, merge verdicts and human decisions.
Check the complete `deep-context/facts/` directory, including person-ID files
and original-parent files. A missing SQLite child facts row does not prove its
paid extraction is absent from disk.

For a population audit, enumerate every multi-child parent, including families
with matching names and no audit warnings. The structural audit does not certify
identity. Review original contact histories separately; a combined parent
dossier cannot independently validate the grouping that produced it. Inspect
old `index.json`, merge-verdicts/candidates CSVs and review rows as well as
SQLite: seed may have imported a verdict's result without its original row.

Trace three separate questions: what the source says, what extraction claimed,
and what code or saved verdict joined the contacts. Record an unknown cause
when the original input or decision is missing. A saved model reason proves
what the model claimed, not that its identifier attribution was correct.

Separate these cases:

| Finding | Action |
|---|---|
| Link lacks an owner but has a unique same-parent contact identifier | Restore membership through `ensure-parents`; this does not change identity |
| Wrong LinkedIn on otherwise consistent contact histories | Correct the profile attachment; do not split solely because profiles disagree |
| Different people grouped together | Prove original ownership, then recover separate contact histories before reassessing duplicate pairs |
| Role mailbox, team or shared office number | Keep organizational context separate from proof of one human |
| Original facts exist but are not projected | Reuse them; do not pay to regenerate them merely because SQLite lacks them |
| Only combined facts survive | Preserve them; recollect the affected contacts separately if source stores are available |
| Source rows already collapsed into one imported person | Use the fan-in manifest's original source CSVs; superseded IDs alone do not prove distinct contacts |
| Ownership or identity remains ambiguous | Keep the evidence and report the exact unresolved question |

Names, employers and shared phone numbers are review signals, not sufficient
split or merge rules. When using Sol or Astra, provide separate original contact
facts, identifier context, saved verdict reasons and human choices. Require
source references for each proposed decision. Do not substitute a confidence
score for that evidence or override an explicit human decision silently.

Use five review outcomes: supported same person, supported separate people,
profile attachment concern, organization/shared mailbox, and insufficient
evidence. Keep uncertain contacts separate. A different career or country alone
does not prove a wrong profile. A source contact-book name conflict disqualifies
an extracted phone/email as identity evidence until resolved. Absence from an
older contact book is not a conflicting name. Do not promote an extracted
identifier into source ownership merely because another extraction repeats it.

Human profile approval is an association to that exact candidate, not approval
of every member of an inherited family. Salvage a direct decision only when its
original contact and scope are unique. Generated sibling-settle decisions and
unsupported legacy source labels do not establish a human click; mixed parent
worth decisions remain preserved but unapplied when their intended person is
unknown. New seed imports withhold these cases and report the existing unmatched
or ambiguous counts.

## Repair and verify

Use a copied store and sibling facts for the first replay. Preserve the original
database and files before applying a repair to the user's store. `ensure-parents`
runs the existing local repairs and creates their backups. Its historical split
repair covers only eligible families with proven source ownership; it is not a
general automatic identity judge.

Run `bin/deep-context ensure-parents --db <store> --people-csv <people.csv>`,
then repeat the audit. Verify contact membership, original facts and paid
payloads, explicit human decisions, and foreign keys. Run again: repaired
ownership must remain unchanged and unresolved cases must still be reported.

For a cold comparison, run the old and new code against identical copied input
files and record any path rebinding or missing source. Block provider calls for
offline replay. Applying a saved verdict reproduces its effect; it does not
measure a fresh judge's accuracy. Validate known wrong merges and supported
duplicates, and report aliases that the conservative rules leave separate.

Rebuild derived dossiers only when their facts are correctly attributed.
`parents` renders saved facts even without a retained raw bundle; `compose`
also requires the projected source bundle. Check actual lookup by each affected name, email and
phone; inspect the returned dossier. Splitting four source contacts is not yet
proof that the three aliases of one human have been correctly regrouped.

## Missing evidence and paid work

Original summaries may predate newer combined extractions. Never claim complete
history recovery from old summaries alone. Retain newer combined evidence while
reconciling it; do not copy the whole mixed extraction onto every child.

Verify the source mail/message stores are available before proposing recollection.
Collect locally, run the synthesis estimate, and ask for approval with the exact
scope and cost before paid calls. Start with one affected contact. Reuse existing
provider caches and completed contact extractions, then verify the resulting
dossiers before widening the approved run.

A full rebuild requires a backup and explicit destruction/spend approval. Fix
the ownership path first; reseeding the same bad families is not recovery.
