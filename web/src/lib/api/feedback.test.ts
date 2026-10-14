import { afterEach, describe, expect, it, vi } from "vitest"

import { buildSearchFeedback } from "@/lib/searches/feedback"
import { must } from "@/lib/must"
import { PAYLOAD } from "@/testing/people-fixture"

import { postFeedback, sendPersonFeedback } from "./feedback"
import { decodePeople } from "./people"

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

describe("sendPersonFeedback", () => {
  it("posts the note with who the person is and the calls shown, not the whole row", async () => {
    const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
      Promise.resolve(new Response(JSON.stringify({ status: "submitted" }))),
    )
    vi.stubGlobal("fetch", fetchMock)
    const jordan = must(decodePeople(PAYLOAD)[0])
    await sendPersonFeedback("Should be shared.", jordan)
    const [url, init] = fetchMock.mock.calls[0] ?? []
    expect(url).toBe("/api/feedback")
    const sent: unknown = JSON.parse(typeof init?.body === "string" ? init.body : "null")
    const { parent_id, name, public_identifier, title, company } = jordan
    const { worth, worth_source, share, share_source, reason, labels, confidence } = jordan
    const person = {
      parent_id,
      name,
      public_identifier,
      title,
      company,
      worth,
      worth_source,
      share,
      share_source,
      reason,
      labels,
      confidence,
    }
    expect(sent).toEqual({ comment: "Should be shared.", person })
  })
})

describe("signIn in the desktop app", () => {
  afterEach(() => {
    vi.doUnmock("@/lib/desktop")
  })

  it("opens the login's page in the browser, waits for the server's outcome, then comes back forward", async () => {
    vi.resetModules()
    const invoke = vi.fn(() => Promise.resolve(null))
    vi.doMock("@/lib/desktop", () => ({ isDesktop: () => true, invoke }))
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
    expect(invoke.mock.calls).toEqual([
      ["open_external", { url: "https://auth.example/authorize" }],
      ["app_focus"],
    ])
  })
})

describe("sign-in recovery", () => {
  afterEach(() => {
    vi.doUnmock("@/lib/desktop")
  })

  it.each([false, true])("returns to the existing Setup login, desktop=%s", async (desktop) => {
    vi.resetModules()
    vi.doMock("@/lib/desktop", () => ({ isDesktop: () => desktop, invoke: vi.fn() }))
    const assign = vi.fn()
    vi.stubGlobal("location", { assign })
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify({ error: "Finish signing in on Setup.", setup_url: "/install" }), {
            status: 409,
          }),
        ),
      ),
    )
    const { signIn } = await import("./feedback")
    await expect(signIn()).rejects.toThrow("Finish signing in on Setup.")
    expect(assign).toHaveBeenCalledWith("/install")
  })

  it("does not treat the callback as success when the token exchange failed", async () => {
    vi.resetModules()
    vi.doMock("@/lib/desktop", () => ({ isDesktop: () => true, invoke: vi.fn(() => Promise.resolve(null)) }))
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(
          url.endsWith("/start")
            ? new Response(JSON.stringify({ url: "https://auth.example/authorize" }))
            : new Response(JSON.stringify({ error: "Sign-in did not complete. Try again." }), {
                status: 401,
              }),
        ),
      ),
    )
    const { signIn } = await import("./feedback")
    await expect(signIn()).rejects.toThrow("Sign-in did not complete. Try again.")
  })
})
