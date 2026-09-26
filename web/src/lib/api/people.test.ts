import { afterEach, describe, expect, it, vi } from "vitest";

import { PAYLOAD } from "@/lib/people/fixture";

import { avatarUrl, decodePeople, fetchPersonDetail, writeTags } from "./people";

afterEach(() => vi.unstubAllGlobals());

describe("decodePeople", () => {
  it("decodes columns by name and derives last, warmthBucket, search", () => {
    const [jordan] = decodePeople(PAYLOAD);
    expect(jordan).toMatchObject({
      parent_id: "p1", name: "Jordan Bravo", channels: ["gmail", "linkedin"], share: "confirm",
      last: "< 1 year", warmthBucket: "Close (2–3)", search: "jordan bravo engineer acme ",
    });
  });
});

describe("requests", () => {
  it("carries the server's error message", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: "person not found" }), { status: 404 })));
    await expect(fetchPersonDetail("p9")).rejects.toThrow("person not found");
  });

  it("posts absolute tag sets and returns the re-decided rows", async () => {
    const rows = [{ parent_id: "p1", share: "yes", reason: "human_share", share_source: "human", tags: ["share"] }];
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => new Response(JSON.stringify({ rows })));
    vi.stubGlobal("fetch", fetchMock);
    expect(await writeTags([{ parent_id: "p1", tags: ["share"] }])).toEqual(rows);
    expect(fetchMock.mock.calls[0]?.[1]?.body).toBe(JSON.stringify({ people: [{ parent_id: "p1", tags: ["share"] }] }));
  });

  it("encodes avatar ids", () => {
    expect(avatarUrl("a b")).toBe("/api/people/avatar?id=a%20b");
  });
});
