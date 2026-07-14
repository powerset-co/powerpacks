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
| Gmail metadata import and identity lookup | [Gmail import pipeline](gmail-import-pipeline.md) | [`$import-gmail`](../skills/import-gmail/SKILL.md) |
| iMessage and WhatsApp metadata import | [Message import pipeline](message-import-pipeline.md) | [`$import-messages`](../skills/import-messages/SKILL.md) |
| Message-derived dossiers and identity self-heal | [Deep-context pipeline](deep-context-pipeline.md) | [`$deep-context`](../skills/deep-context/SKILL.md) |

For LinkedIn ingestion and the shared cloud index build, see the
[LinkedIn and Modal indexing pipeline](../../indexing/docs/linkedin-modal-pipeline.md).

## Shared pipeline seam

`$setup`, `$import-gmail`, and `$import-messages` are currently end-to-end user
workflows. Each source import nevertheless writes the same fixed contract at
`.powerpacks/network-import/import/<source>/people.csv`. Shared fan-in through
`index_contacts_pipeline.py fan-in` and the subsequent Modal or local indexing
stage are separate primitives.

That boundary is an implementation seam, not a second shipped workflow. A later
PR can add selective import-then-index orchestration by composing chosen source
imports, one shared fan-in, and one index build without changing source outputs.

## Historical and specialist references

| Document | Status |
| --- | --- |
| [Data-processing handoff](data-processing-handoff.md) | Historical implementation handoff. Some skill names and orchestration details predate the current source-specific flows. |
| [Gmail contact LLM review proposal](gmail-contact-llm-review-proposal.md) | Proposal. The verification/review layer is not wired into `$import-gmail`. |
| [Synthetic profiles plan](synthetic-profiles-plan.md) | Design history plus shipped implementation notes. Use the deep-context guide and skill for current behavior. |
