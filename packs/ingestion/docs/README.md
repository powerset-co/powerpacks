# Ingestion documentation

Use the product guides first. Skills are the executable agent contracts;
primitive docs are implementation references; explicitly labeled proposals are
design history.

The ingestion pack is the canonical home for all current source intake:
LinkedIn, Gmail/email, Twitter/X, iMessage, and WhatsApp. There is no separate
Messages source-code pack. The existing `.powerpacks/messages/` local artifact
namespace stays unchanged so upgrades do not invalidate user data.

## Current product guides

| Workflow | Guide | Executable skill |
| --- | --- | --- |
| Gmail contact sync (free, local) | [Gmail import pipeline](gmail-import-pipeline.md) | [`$import-gmail`](../skills/import-gmail/SKILL.md) |
| iMessage and WhatsApp contact sync | [Message import pipeline](message-import-pipeline.md) | [`$import-messages`](../skills/import-messages/SKILL.md) |
| File-driven post-import processing plus ad-hoc dossier/identity lookup | [Deep-context pipeline](deep-context-pipeline.md) | [`$deep-context`](../skills/deep-context/SKILL.md) |

For LinkedIn ingestion and the shared cloud index build, see the
[LinkedIn and Modal indexing pipeline](../../indexing/docs/linkedin-modal-pipeline.md).

## Shared pipeline seam

Each source import writes `.powerpacks/network-import/import/<source>/people.csv`.
Shared fan-in through `index_contacts_pipeline.py fan-in` retains source IDs and
combines repeated IDs. Deep Context collects facts, reviews associations, merges
people and exports the judged SQLite parents. Directory lookups do not establish
identity. LinkedIn setup has its separate Modal indexing workflow.

## Historical and specialist references

| Document | Status |
| --- | --- |
| [Data-processing handoff](data-processing-handoff.md) | Historical implementation handoff. Some skill names and orchestration details predate the current source-specific flows. |
| [Synthetic profiles plan](synthetic-profiles-plan.md) | Design history plus shipped implementation notes. Use the deep-context guide and skill for current behavior. |
