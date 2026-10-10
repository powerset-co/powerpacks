import { body, failure } from "./http"

export interface Member {
  operator_id: string
  name: string
}

export interface SharedMessage {
  id: string
  author_id: string
  author_name: string
  text: string
  created_at: string
  reply_to?: string
  request_id?: string
  recipient_id?: string
  status?: "unsent" | "queued" | "working" | "answered" | "failed"
  answer?: string
  error?: string
}

export interface Conversation {
  id: string
  set_id: string
  title: string
  search_id: string
  kind: "search"
  messages: SharedMessage[]
  query?: string
}

export interface Collaboration {
  me: Member
  sets: { set_id: string; name: string; members: Member[] }[]
  conversations: Conversation[]
}

export interface PendingQuestion {
  id: string
  set_id: string
  conversation_id: string
  from_name: string
  question: string
  thread_id?: string
  query?: string
  answer?: string
  error?: string
}

const URL = "/api/collaboration"

export async function fetchCollaboration(): Promise<Collaboration> {
  const response = await fetch(URL)
  if (!response.ok) throw await failure(response, "Could not load set conversations.")
  return body<Collaboration>(response)
}

export async function fetchQuestions(): Promise<{ questions: PendingQuestion[] }> {
  const response = await fetch(`${URL}/pending`)
  if (!response.ok) throw await failure(response, "Could not load questions.")
  return body<{ questions: PendingQuestion[] }>(response)
}

export async function postConversation(
  path: "conversations" | "messages" | "resend",
  payload:
    | { set_id: string; search_id: string; title: string; query?: string }
    | { conversation_id: string; text: string; recipient_id?: string; reply_to?: string }
    | { conversation_id: string; message_id: string },
): Promise<Conversation> {
  const response = await fetch(`${URL}/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw await failure(response, "Could not send to the set.")
  return body<Conversation>(response)
}
