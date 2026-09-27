// Synthetic People upload statuses, every key present as GET /api/people/upload sends them.

import type { UploadPlan, UploadStatus } from "@/lib/api/upload"

export const PLAN: UploadPlan = {
  marked_share: 130,
  with_linkedin: 128,
  without_linkedin: 2,
  new_to_cloud: 3,
  changed: 0,
  already_shared: 125,
  losing_access: 0,
  companies_missing: 0,
}

export function uploadStatus(overrides: Partial<UploadStatus> = {}): UploadStatus {
  return {
    status: "idle",
    stage: null,
    message: null,
    progress: { total: 0, uploaded: 0, skipped: 0, namespaces: {} },
    plan: null,
    last_upload: null,
    failed_action: null,
    error: null,
    ...overrides,
  }
}

export function uploadResponse(status: UploadStatus, code = 200): Response {
  return new Response(JSON.stringify(status), {
    status: code,
    headers: { "Content-Type": "application/json" },
  })
}
