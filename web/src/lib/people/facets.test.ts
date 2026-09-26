import { describe, expect, it } from "vitest";

import { decodePeople } from "@/lib/api/people";

import { LAST, lastBucket, nextTags, QUICK, sortRows } from "./facets";
import { PAYLOAD } from "./fixture";
import { filterRows } from "./filter";

const rows = decodePeople(PAYLOAD);
const view = { tab: "confirm" as const, filters: new Map<string, Set<string>>(), text: "", sort: { key: "name" as const, dir: 1 as const } };

describe("lastBucket", () => {
  it("splits at a year and two years", () => {
    expect(lastBucket(364)).toBe(LAST[0]);
    expect(lastBucket(365)).toBe(LAST[1]);
    expect(lastBucket(730)).toBe(LAST[1]);
    expect(lastBucket(731)).toBe(LAST[2]);
    expect(lastBucket(null)).toBe(LAST[3]);
  });
});

describe("filterRows", () => {
  it("keeps the tab and sorts", () => {
    const { matching } = filterRows(rows, view);
    expect(matching.map((row) => row.name)).toEqual(["Casey Delta", "Jordan Bravo", "Riley Echo"]);
  });

  it("counts a facet ignoring its own selection", () => {
    const filters = new Map([["relationship_kind", new Set(["family"])], ["last", new Set([LAST[0]])]]);
    const { matching, counts } = filterRows(rows, { ...view, filters });
    expect(matching.map((row) => row.name)).toEqual(["Jordan Bravo"]);
    // Relationship counts see rows passing every other facet (last < 1 year): Jordan only.
    expect([...(counts.get("relationship_kind") ?? [])]).toEqual([["family", 1]]);
    // Last-contact counts see the family rows: Jordan (< 1 year) and Riley (1–2 years).
    expect(counts.get("last")?.get(LAST[0])).toBe(1);
    expect(counts.get("last")?.get(LAST[1])).toBe(1);
    // Other facets count only full matches.
    expect([...(counts.get("worth") ?? [])]).toEqual([["yes", 1]]);
  });

  it("counts quick filters within the tab, before text and facets", () => {
    const filters = new Map([["worth", new Set(["yes"])]]);
    const { quickCounts } = filterRows(rows, { ...view, filters, text: "jordan" });
    const byName = Object.fromEntries(QUICK.map((quick, position) => [quick.name, quickCounts[position]]));
    expect(byName).toMatchObject({ Family: 2, Recruiters: 1, "Close friends": 1, "Last contact > 2 years": 1 });
  });

  it("matches text on name, title, company and location", () => {
    expect(filterRows(rows, { ...view, text: " ACME " }).matching.map((row) => row.name)).toEqual(["Jordan Bravo"]);
    expect(filterRows(rows, { ...view, tab: "yes", text: "springfield" }).matching).toHaveLength(1);
  });
});

describe("sortRows and nextTags", () => {
  it("sorts by interactions, most first", () => {
    expect(sortRows(rows, { key: "messages", dir: 1 }).map((row) => row.interactions)).toEqual([90, 40, 12, 3]);
  });

  it("swaps share and private", () => {
    const riley = rows.find((row) => row.name === "Riley Echo");
    expect(riley && nextTags(riley, "share")).toEqual(["share"]);
    expect(riley && nextTags(riley, "worth")).toEqual([]);
  });
});
