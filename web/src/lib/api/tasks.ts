// The Scheduled tasks page's JSON routes on the Python review server.

import { body, failure } from "@/lib/api/http"
import type { Runner, Task } from "@/types/tasks"

export async function fetchTask(signal?: AbortSignal): Promise<Task> {
  const response = await fetch("/tasks/api/task", { signal })
  if (!response.ok) throw await failure(response, "Couldn't load scheduled tasks.")
  return body<Task>(response)
}

/** Installs or removes the refresh task under one runner; resolves to the task as it now stands. */
export async function setInstalled(runner: Runner, installed: boolean): Promise<Task> {
  const response = await fetch(`/tasks/api/${installed ? "install" : "uninstall"}`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ runner }),
  })
  if (!response.ok) throw await failure(response, "Couldn't change the schedule.")
  return body<Task>(response)
}
