import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

import {
  fetchCollaboration,
  fetchQuestions,
  postConversation,
  type PendingQuestion,
} from "@/lib/api/collaboration"
import { invoke } from "@/lib/desktop"

const KEY = ["collaboration"]

export function useCollaboration() {
  const client = useQueryClient()
  const [busy, setBusy] = useState(false)
  const [answering, setAnswering] = useState<string | null>(null)
  const [error, setError] = useState<string | undefined>()
  const state = useQuery({
    queryKey: KEY,
    queryFn: fetchCollaboration,
    refetchInterval: 5000,
  })
  const pending = useQuery({
    queryKey: [...KEY, "pending"],
    queryFn: fetchQuestions,
    refetchInterval: 5000,
  })
  const data = state.data

  async function run<T>(action: () => Promise<T>): Promise<T | undefined> {
    setBusy(true)
    setError(undefined)
    try {
      return await action()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setBusy(false)
      await client.invalidateQueries({ queryKey: KEY })
    }
  }

  const create = (set_id: string, search_id: string, title: string) =>
    run(async () => {
      const fields = { set_id, search_id, title, query: title }
      return postConversation("conversations", fields)
    })

  const send = (conversation_id: string, text: string, recipient_id?: string, reply_to?: string) =>
    run(async () => {
      await postConversation("messages", { conversation_id, text, recipient_id, reply_to })
      return true
    })

  const answer = async (question: PendingQuestion) => {
    setAnswering(question.id)
    setError(undefined)
    try {
      await invoke("codex_answer_question", { questionId: question.id })
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setAnswering(null)
      await client.invalidateQueries({ queryKey: KEY })
    }
  }

  const resend = (conversation_id: string, message_id: string) =>
    run(async () => {
      await postConversation("resend", { conversation_id, message_id })
    })

  return {
    data,
    busy,
    answering,
    error: error ?? state.error?.message ?? pending.error?.message,
    pending: pending.data?.questions ?? [],
    create,
    send,
    answer,
    resend,
    loading: state.isPending,
  }
}
