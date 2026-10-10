import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, renderHook, waitFor } from "@testing-library/react"
import type { ReactNode } from "react"
import { afterEach, expect, it, vi } from "vitest"

import { useCollaboration } from "./useCollaboration"

const api = vi.hoisted(() => ({
  fetchCollaboration: vi.fn(),
  fetchQuestions: vi.fn(),
  postConversation: vi.fn(),
}))
const desktop = vi.hoisted(() => ({ invoke: vi.fn() }))
vi.mock("@/lib/api/collaboration", () => api)
vi.mock("@/lib/desktop", () => desktop)

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return renderHook(() => useCollaboration(), {
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  })
}

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

it("does not run pending live questions until the recipient explicitly answers", async () => {
  const question = {
    id: "question",
    set_id: "set",
    conversation_id: "chat",
    from_name: "Casey Example",
    question: "Who do you know?",
  }
  api.fetchCollaboration.mockResolvedValue({
    me: { operator_id: "me", name: "Me" },
    sets: [],
    conversations: [],
  })
  api.fetchQuestions.mockResolvedValue({ questions: [question] })
  desktop.invoke.mockResolvedValue({ text: "An answer" })
  const { result } = setup()
  await waitFor(() => expect(result.current.pending).toEqual([question]))
  expect(desktop.invoke).not.toHaveBeenCalled()
  await act(() => result.current.answer(question))
  expect(desktop.invoke).toHaveBeenCalledExactlyOnceWith("codex_answer_question", { questionId: "question" })
})

it("retries delivery with the same saved message ID", async () => {
  api.fetchCollaboration.mockResolvedValue({
    me: { operator_id: "me", name: "Me" },
    sets: [],
    conversations: [],
  })
  api.fetchQuestions.mockResolvedValue({ questions: [] })
  const { result } = setup()
  await act(() => result.current.resend("chat", "original-message"))
  expect(api.postConversation).toHaveBeenCalledExactlyOnceWith("resend", {
    conversation_id: "chat",
    message_id: "original-message",
  })
  expect(desktop.invoke).not.toHaveBeenCalled()
})
