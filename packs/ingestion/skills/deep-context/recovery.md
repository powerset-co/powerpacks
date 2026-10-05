# Contact recovery

Use the existing SQLite store and source artifacts. Preserve original contact
facts and explicit human decisions; a seeded machine merge remains a machine
decision. Keep a golden copy before replaying changes; do not introduce another
ledger. A cold rebuild on a separate copy can reuse facts without inheriting
the old family membership.

## Read the operator's feedback first

Before recovery changes, pull the operator's existing feedback. Fresh `rebuild`
requires `--operator-id`; it reads the operator-scoped `GET /v2/feedback` before
backup or new SQLite creation. The authenticated contact-datalake summary and
every returned feedback row must match that operator. Authentication or scope
failure stops preparation. This is a free read, with no impersonation or remote
write. Use the target operator's login; never substitute another person's account.

For an already obtained read-only snapshot, pass `--feedback-json` containing the
API response row list. The same parser checks every row's operator. This allows
an authorized target-operator snapshot to be used locally without changing login.

Support resolves identity feedback remotely through explicit columns:
`valid_linkedin_url`, `resolution_action`, `resolution_person_ids`,
`resolution_note`, `resolved_by` and `resolved_at`.
Recovery downloads that explicit choice as human authority;
support may research a replacement that was not in the original comment. The
original metadata, comment and guidance remain unchanged. A resolution is
consumed regardless of the original feedback type or action. Null means
unresolved, not "this person has no LinkedIn". Action `linkedin` requires a valid URL; actions
`synthetic`, `exclude` and `unresolved` require a null URL. Missing review
provenance cannot apply.
Local reviewed mapping CSV input is removed: the remote feedback row is the
single authority. A candidate/provider `proposed_linkedin_url` or guidance
`new_url`, standalone URL comment, and unreviewed free text never establish a
resolved mapping. Action `synthetic` creates an accepted existing synthetic
identity marker with the original guidance as its note, retaining the person
and worth without inventing a provider profile. Its human decision prevents
ordinary matching/research from replacing it. Action `exclude` explicitly sets
human Worth No; a wrong URL or failed research never excludes the contact.
Action `unresolved` and free-text reports without a resolution remain held.

Application requires one exact original contact or unique supplied source
email/phone. An old contact UUID can resolve through unchanged source endpoints
and name to one fresh contact; LinkedIn-only or combined source rows cannot prove
that mapping. Support-reviewed `resolution_person_ids`, when present,
replace only the application scope; original metadata stays preserved. Each
reviewed contact is applied separately, without merging it with other contacts.
Unreviewed multiple-contact scope, uncertain wording and conflicting
choices remain held. The remote resolution timestamp and local human decision
timestamp determine precedence; a later explicit choice wins. Proven newer
local choices retain their timestamps, notes and targets. Unproved old
machine/seed fields do not outrank a support resolution. Remote feedback does
not merge source contacts.

The `deep-context/rebuild/feedback.json` output preserves all fetched raw rows.
The adjacent `feedback.csv` presents identity feedback with its feedback ID,
operator, original and reviewed contact IDs/candidate key, resolved URL/action, resolver, resolution
time/note and original guidance, including unresolved notes. `rebuild/manifest.json` records
both paths, the operator, raw row count and considered identity decisions with
applied/held scope, target, source feedback ID and decision timestamp. Raw count
is not an applied mapping count. No new local database schema is created.

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
database and files before applying a repair to the user's store. Ordinary
`ensure-parents` restores missing candidate membership from unique same-parent
source identifiers and runs pending structural migrations. It does not infer
historical identity splits from names or undo an accepted current merge.

Run `bin/deep-context ensure-parents --db <store> --people-csv <people.csv>`,
then repeat the audit. Verify contact membership, original facts and paid
payloads, explicit human decisions, and foreign keys. Run again: repaired
ownership must remain unchanged. Audit still reports unresolved cases.

`source_identity_unresolved` holds contacts whose original names are missing,
conflict with one another, or disagree with current retained fact names. The
same policy withholds human dossiers and paid identity research; facts and raw
evidence remain intact. A hold is not proof of different people. Correcting a
source name alone does not validate old facts attributed to another name.

Historical split recovery remains an explicit operation for a reviewed old
store with sibling original facts. Its name heuristic can separate supported
aliases; it is not part of the fresh rebuild below. After reviewing the exact
families it will affect, invoke it on the copy:

```bash
uv run --project . python - /absolute/copied/deep-context.sqlite <<'PY'
import sys
from dataclasses import asdict
from pathlib import Path
from packs.ingestion.primitives.common.legacy import _scrub_historical_merges
from packs.ingestion.primitives.deep_context.db.store import Db
print(asdict(_scrub_historical_merges(Db(Path(sys.argv[1])))))
PY
```

Verify the reported families and unresolved ownership, then reassess duplicate
pairs from separate original contact evidence. Do not apply this historical
heuristic to the fresh graph after current merges have been accepted.

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

Keep a backup. Obtain approval for paid stages or a destructive live cutover;
approval already given for the same scope remains valid. Fix the ownership path
first; reseeding the same bad families is not recovery.

## Fresh rebuild with current human decisions

Regenerate source imports into an isolated state directory from the declared raw
accounts/channels. Use the source-only LinkedIn parser; the full LinkedIn import
includes provider enrichment. Reconcile source counts, exclusions and unavailable
message bodies before claiming coverage. Historical merged or enriched source
CSVs cannot establish a fresh source population.

Use a dedicated corrected checkout whose `.powerpacks` is the fresh state.
Gmail discovery/import use checkout-relative outputs, and source manifests can
contain relative paths. Run the source primitives and preparation in that same
checkout.

Prepare the isolated graph with the free local command; no approval is required:

```bash
cd /absolute/fresh-checkout
bin/deep-context rebuild \
  --original-state-root /absolute/original/.powerpacks \
  --backup-root /absolute/rollback/.powerpacks \
  --state-root /absolute/fresh-checkout/.powerpacks \
  --people-csv /absolute/fresh-checkout/.powerpacks/network-import/merged/people.csv \
  --owner-profile /absolute/reviewed-owner.json \
  --operator-id <expected-operator-uuid> \
  --feedback-json /absolute/operator-feedback.json
```

The three state roots must be disjoint. The backup destination must not exist.
The new state may contain fresh source imports; it must contain no active Deep
Context artifacts, directory mappings, review overrides or profile cache. The
primitive validates the actual fan-in source files/counts and explicit owner
profile before writing. It preserves the complete original state tree, including
historical backups and symlinks, and makes a consistent canonical SQLite backup.
Symlinks remain links; external raw stores need their own consistent snapshots.
The original store is read only. Preparation never resets or switches the live
installation and makes no provider calls. With `--feedback-json`, preparation is
entirely local; without it, the read-only authenticated feedback preflight runs
first. Existing snapshots without `valid_linkedin_url` resolution fields retain
their unresolved notes and supply no positive mappings.

The new canonical DB contains separately projected source contacts and the
explicit owner configuration. Active facts, raw and research directories start
empty. General `seed`, `clean-slate` and `restart` are not part of this flow.
All old facts, research, machine decisions and families remain comparison
evidence in the backup and have no authority in the fresh graph.

Carry reads raw SQLite human fields and exact `candidate_people` membership,
plus current `network-import/overrides/review.csv` actions not already in SQLite.
Only a direct review/user-guidance decision with one unchanged source contact,
source name/endpoints, timestamp and exact profile target can apply. Profile
approval never merges contacts. Retarget preserves both the reviewed and replacement
URLs. Conflicting current choices remain held, even when stored under different
row keys. Mixed parent worth, missing scope, changed source ownership, unknown
provenance and synthetic targets remain unapplied. Generated sibling decisions
and machine-only rows are counted separately.

Historical `user-guidance` profile marks also need the original explicit target.
The old guided research path could label a provider-selected URL, or a failed
lookup's detach, as a human choice. Carry requires an exact standalone submitted
profile URL in the saved guidance or decision note, matching the applied target
and unique contact scope. Free text, a URL merely mentioned in a sentence, and
missing original guidance leave the decision held. Direct profile-review choices
continue to use their saved review provenance.

For previously seeded stores, the original current review row must also prove
the supported decision source, scope, action, timestamp and target. An old seed
could relabel machine rows as human review. A legacy parent-worth row with one
`worth_person_ids` value is insufficient: the old worth view could rewrite an
inherited multi-contact decision into that shape. Archived/reset decisions are
preserved but never resurrected automatically. Missing current human fields or
CSV means no proven current choice, not permission to choose a historical backup.

The only preparation receipt is `deep-context/rebuild/manifest.json`. It reports
applied, held and unmatched actions with their original keys, contacts, target
and reason, plus excluded generated/machine rows. A second preparation refuses
before mutation; use another isolated destination for another comparison.

Keep every downstream stage in that isolated checkout, or bind **both** the DB
and artifact paths on every command. For example,
collection uses `--db /absolute/fresh-checkout/.powerpacks/deep-context/deep-context.sqlite`
and `--out-dir /absolute/fresh-checkout/.powerpacks/deep-context/raw`. The free synthesis
estimate uses that DB, `--raw-dir /absolute/fresh-checkout/.powerpacks/deep-context/raw`,
`--out-dir /absolute/fresh-checkout/.powerpacks/deep-context/facts` and `--dry-run`.
Changing only `--db` leaves the command's default artifact paths active.
Do not run paid synthesis, judging or research until the exact scope and estimate
are approved. Verify contact membership, human carry, zero inherited machine
artifacts, foreign keys and unchanged export/reimport in the actual copied CLI
before proposing cutover. These checks establish scoped reconstruction, not a
universal identity-precision guarantee.
