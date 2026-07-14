# Ingestion documentation

Use the product guides first. Skills are the executable agent contracts;
primitive docs are implementation references; explicitly labeled proposals are
design history.

## Current product guides

| Workflow | Guide | Executable skill |
| --- | --- | --- |
| Gmail metadata import and identity lookup | [Gmail import pipeline](gmail-import-pipeline.md) | [`$import-gmail`](../skills/import-gmail/SKILL.md) |
| Message-derived dossiers and identity self-heal | [Deep-context pipeline](deep-context-pipeline.md) | [`$deep-context`](../skills/deep-context/SKILL.md) |

For LinkedIn ingestion and the shared cloud index build, see the
[LinkedIn and Modal indexing pipeline](../../indexing/docs/linkedin-modal-pipeline.md).
For iMessage/WhatsApp metadata import, see the
[Messages import pipeline](../../messages/docs/message-import-pipeline.md).

## Historical and specialist references

| Document | Status |
| --- | --- |
| [Data-processing handoff](data-processing-handoff.md) | Historical implementation handoff. Some skill names and orchestration details predate the current source-specific flows. |
| [Gmail contact LLM review proposal](gmail-contact-llm-review-proposal.md) | Proposal. The verification/review layer is not wired into `$import-gmail`. |
| [Synthetic profiles plan](synthetic-profiles-plan.md) | Design history plus shipped implementation notes. Use the deep-context guide and skill for current behavior. |
