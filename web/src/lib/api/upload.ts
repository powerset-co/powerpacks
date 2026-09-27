import { body, failure } from "@/lib/api/http"

export interface UploadStatus {
  status: "idle" | "running" | "completed" | "failed" | "interrupted"
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
}

export async function uploadStatus(start = false): Promise<UploadStatus> {
  const response = await fetch(
    "/api/people/upload",
    start
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
