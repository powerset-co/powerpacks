import { beforeEach, describe, expect, it } from "vitest";

import { readSession, writeSession } from "./storage";

beforeEach(() => sessionStorage.clear());

describe("session view", () => {
  it("round-trips the view", () => {
    writeSession({ tab: "no", filters: new Map([["worth", new Set(["yes"])]]), text: "acme", sort: { key: "last", dir: -1 } });
    const view = readSession();
    expect(view?.tab).toBe("no");
    expect([...(view?.filters?.get("worth") ?? [])]).toEqual(["yes"]);
    expect(view?.sort).toEqual({ key: "last", dir: -1 });
  });

  it("drops unknown facets and tabs, and survives junk", () => {
    sessionStorage.setItem("powerpacks:people-filters:v2", JSON.stringify({ tab: "maybe", filters: { gone: ["x"] } }));
    const view = readSession();
    expect(view?.tab).toBeUndefined();
    expect(view?.filters?.size).toBe(0);
    sessionStorage.setItem("powerpacks:people-filters:v2", "{");
    expect(readSession()).toBeNull();
  });
});
