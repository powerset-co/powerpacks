import { body, failure } from "@/lib/api/http"

export interface UploadStatus {
  status: "idle" | "running" | "ready" | "completed" | "failed" | "interrupted"
  checking?: boolean
  previously_uploaded?: number
  progress: {
    total: number
    uploaded: number
    skipped: number
    namespaces?: Record<string, { upserted: number; patched: number }>
  }
  stage?: string
  message?: string
  error?: string
  skipped_no_linkedin?: number
  companies_skipped_no_row?: number
}

export async function uploadStatus(action?: "check" | "confirm"): Promise<UploadStatus> {
  const response = await fetch(
    action === "check" ? "/api/people/upload/check" : "/api/people/upload",
    action
      ? {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        }
      : undefined,
  )
  if (!response.ok) throw await failure(response, "Couldn't reach the upload. Try again.")
  return body<UploadStatus>(response)
}
