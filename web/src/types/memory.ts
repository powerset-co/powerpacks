// packs/memory/job.py Job, and what packs/shared/web/memory.py adds to it before Memory is enabled;
// packs/memory/directory.py Meaning and Page for the search box.

export type MemoryStep = "import" | "generate" | "index" | "discover"

export interface StepCounts {
  done: number
  total: number
}

export interface MemoryFailure {
  step: MemoryStep
  key: string
  error: string
  at: string
}

export interface MemoryEstimate {
  pending_chunks: number
  generation_input_tokens: number
  generation_output_token_cap: number
  embedding_input_tokens: number
  // null while the models' prices are unknown: the page shows the tokens instead.
  estimated_cost_usd: number | null
}

export interface MemoryInventory {
  counts: Partial<Record<"parents" | "facts" | "person_tags" | "current_profile", number>>
}

export interface MemoryJob {
  enabled: boolean
  keep_updated: boolean
  update_available?: boolean
  step: "idle" | MemoryStep | "done"
  status: "idle" | "running" | "done" | "failed"
  steps: Record<MemoryStep, StepCounts>
  current_item: string
  people_indexed: number
  started_at: string
  updated_at: string
  approved_spend_usd: number
  // null when the models' prices are unknown: the tokens are the cost then.
  spent_usd: number | null
  tokens: { input: number; output: number; embedding: number }
  failures: MemoryFailure[]
  error: string
  // Why a finished run stopped short, e.g. "approved spend reached".
  stopped: string
  // Only before Memory is enabled; estimate is null with no Deep Context store yet.
  inventory?: MemoryInventory
  sources?: string[]
  estimate?: MemoryEstimate | null
}

export type MemoryOrigin = "source_name" | "human_tag" | "topic" | "saved_value"

export interface MemorySelector {
  origin: MemoryOrigin
  value: string
}

export interface MemoryCitation {
  source: string
  source_hash: string
  quote: string
  level: string
}

// packs/memory/query.py People and PersonHit: the one search the page and the memory skill share.
export interface MemoryClaim {
  text: string
  kind: string
  topic: string
  scope: string
  object: string
}

export interface MemoryHit {
  claim_id: number
  claim: MemoryClaim
  citations: MemoryCitation[]
}

export interface MemoryPersonHit {
  parent: string
  name: string
  // 0-4, Deep Context's own read of how warm the relationship is.
  warmth: number
  score: number
  evidence: MemoryHit[]
  members: string[]
}

export interface MemoryPeople {
  people: MemoryPersonHit[]
  // Owner labels the words named (family, close friend, past coworker …).
  layers: string[]
  // True when other words narrowed the layer; false when a layer alone lists everyone holding it.
  filtered: boolean
  total: number
  lexical_candidates: number
  vector_candidates: number
  missing_embeddings: number
}

export interface MemoryGraphNode {
  id: string
  label: string
  kind: "person" | "topic"
}

export interface MemoryGraphData {
  corpus: string
  nodes: MemoryGraphNode[]
  edges: { source: string; target: string }[]
  selected_ids: string[]
  total_people: number
  total_topics: number
  truncated: boolean
}
