import { afterEach, describe, expect, it, vi } from "vitest"

import { buildSearchFeedback } from "@/lib/searches/feedback"

import { postFeedback } from "./feedback"

afterEach(() => vi.unstubAllGlobals())

describe("postFeedback", () => {
  it("posts the record as form fields and returns the reply", async () => {
    const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
      Promise.resolve(new Response(JSON.stringify({ ok: true, status: "submitted" }))),
    )
    vi.stubGlobal("fetch", fetchMock)
    expect(await postFeedback(buildSearchFeedback("jordan-role", "Too senior"))).toEqual({
      ok: true,
      status: "submitted",
    })
    const [url, init] = fetchMock.mock.calls[0] ?? []
    expect(url).toBe("/searches/feedback")
    expect(init?.body).toBeInstanceOf(URLSearchParams)
    expect(init?.body instanceof URLSearchParams ? init.body.toString() : "").toBe(
      "run_id=jordan-role&person_id=&comment=Too+senior",
    )
  })

  it("carries the server's error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response("candidate not found", { status: 404 }))),
    )
    await expect(postFeedback(buildSearchFeedback("jordan-role", "x"))).rejects.toThrow("candidate not found")
  })
})

describe("signIn in the desktop app", () => {
  afterEach(() => {
    vi.doUnmock("@/lib/desktop")
    vi.doUnmock("@/lib/signin")
  })

  it("shows the login's page in the sign-in pane and waits for the server's outcome", async () => {
    vi.resetModules()
    const openSignIn = vi.fn()
    vi.doMock("@/lib/desktop", () => ({ isDesktop: () => true }))
    vi.doMock("@/lib/signin", () => ({
      POWERSET_CALLBACK: ["http://localhost:9876/callback"],
      openSignIn,
      signInFinished: () => Promise.resolve(true),
    }))
    const fetchMock = vi.fn((url: string) =>
      Promise.resolve(
        new Response(
          JSON.stringify(
            url.endsWith("/start")
              ? { status: "sign_in", url: "https://auth.example/authorize" }
              : { ok: true, status: "authenticated" },
          ),
        ),
      ),
    )
    vi.stubGlobal("fetch", fetchMock)
    const { signIn } = await import("./feedback")
    await signIn()
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/searches/auth/login/start",
      "/searches/auth/login/finish",
    ])
    expect(openSignIn).toHaveBeenCalledWith({
      title: "Powerset",
      url: "https://auth.example/authorize",
      finish: ["http://localhost:9876/callback"],
    })
  })

  it("fails at once when the pane is closed before the callback", async () => {
    vi.resetModules()
    vi.doMock("@/lib/desktop", () => ({ isDesktop: () => true }))
    vi.doMock("@/lib/signin", () => ({
      POWERSET_CALLBACK: ["http://localhost:9876/callback"],
      openSignIn: vi.fn(),
      signInFinished: () => Promise.resolve(false),
    }))
    const fetchMock = vi.fn(() =>
      Promise.resolve(new Response(JSON.stringify({ status: "sign_in", url: "https://auth.example/a" }))),
    )
    vi.stubGlobal("fetch", fetchMock)
    const { signIn } = await import("./feedback")
    await expect(signIn()).rejects.toThrow("closed before it finished")
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})
