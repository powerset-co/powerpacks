// The Memory page's numbers, worded once.

import type { MemoryJob, MemoryStep } from "@/types/memory"

/** The run's stages in the order the runner takes them (packs/memory/job.py STEPS). */
export const STEPS: readonly { key: MemoryStep; label: string; unit: string }[] = [
  { key: "import", label: "Import saved facts", unit: "sources" },
  { key: "generate", label: "Read them into memories", unit: "chunks" },
  { key: "index", label: "Index for search", unit: "memories" },
  { key: "discover", label: "Connect topics across people", unit: "batches" },
]

export function stepLabel(key: MemoryStep): string {
  return STEPS.find((step) => step.key === key)?.label ?? key
}

export function dollars(value: number): string {
  return value.toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2 })
}

export function tokenText(count: number): string {
  return `${count.toLocaleString()} tokens`
}

/** What the run has spent: dollars against the approved limit, or tokens while prices are unknown. */
export function spentText(record: MemoryJob): string {
  if (record.spent_usd === null) {
    const { input, output, embedding } = record.tokens
    return tokenText(input + output + embedding)
  }
  return `${dollars(record.spent_usd)} of ${dollars(record.approved_spend_usd)} approved`
}
