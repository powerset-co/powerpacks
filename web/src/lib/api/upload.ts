// The People upload routes (the status contract of 2026-09-27): every key is always sent.

import { body, failure } from "@/lib/api/http"

export type UploadState = "idle" | "checking" | "ready" | "uploading" | "completed" | "failed" | "interrupted"
export type UploadAction = "check" | "upload"

export interface UploadPlan {
  marked_share: number
  with_linkedin: number
  without_linkedin: number
  new_to_cloud: number
  changed: number
  already_shared: number
  losing_access: number
  companies_missing: number
}

export interface LastUpload {
  finished_at: string
  status: "completed" | "failed" | "interrupted"
  uploaded: number
  skipped: number
}

export interface UploadStatus {
  status: UploadState
  stage: string | null
  message: string | null
  progress: {
    total: number
    uploaded: number
    skipped: number
    namespaces: Record<string, { upserted: number; patched: number }>
  }
  plan: UploadPlan | null
  last_upload: LastUpload | null
  failed_action: UploadAction | null
  error: string | null
}

const STATUS_URL = "/api/people/upload"
const START_URL: Readonly<Record<UploadAction, string>> = {
  check: "/api/people/upload/check",
  upload: "/api/people/upload",
}
const FALLBACK = "Couldn't reach the upload. Try again."

async function answer(response: Response): Promise<UploadStatus> {
  if (!response.ok) throw await failure(response, FALLBACK)
  return body<UploadStatus>(response)
}

export async function readUpload(): Promise<UploadStatus> {
  return answer(await fetch(STATUS_URL))
}

/** Starts the check (a dry run) or the real upload; a refusal (409, 403) throws the server's sentence. */
export async function startUpload(action: UploadAction): Promise<UploadStatus> {
  return answer(
    await fetch(START_URL[action], {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    }),
  )
}
