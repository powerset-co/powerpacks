// LinkedIn's paged connections answer becomes the same rows the page read gave.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { connectionsUrl, connectionRows } = require("../packs/ingestion/primitives/discover/linkedin/connections_api.js");

const profile = (urn, slug, first, last, headline, picture = true) => ({
  $type: "com.linkedin.voyager.dash.identity.profile.Profile", entityUrn: urn, publicIdentifier: slug,
  firstName: first, lastName: last, headline,
  ...(picture ? { profilePicture: { displayImageReference: { vectorImage: { rootUrl: "https://media.licdn.com/dms/image/v2/X/",
    artifacts: [{ width: 400, fileIdentifyingUrlPathSegment: "400.jpg" }, { width: 100, fileIdentifyingUrlPathSegment: "100.jpg" },
      { width: 200, fileIdentifyingUrlPathSegment: "200.jpg" }] } } } } : {}),
});
const connection = (urn, member, createdAt) => ({
  $type: "com.linkedin.voyager.dash.relationships.Connection", entityUrn: urn, createdAt,
  connectedMember: member, "*connectedMemberResolutionResult": member,
});

// Voyager's normalized shape: the order is data["*elements"], the entities are in `included`.
const ANSWER = {
  data: { "*elements": ["urn:li:fsd_connection:2", "urn:li:fsd_connection:1", "urn:li:fsd_connection:3"] },
  included: [
    profile("urn:li:fsd_profile:A", "casey-lane", "Casey ", "Lane", "Engineer  at Example\n"),
    connection("urn:li:fsd_connection:1", "urn:li:fsd_profile:A", Date.UTC(2026, 0, 1, 12)),
    connection("urn:li:fsd_connection:2", "urn:li:fsd_profile:B", Date.UTC(2026, 9, 2, 12)),
    profile("urn:li:fsd_profile:B", "jordan-bravo", "Jordan", "Bravo", "", false),
    // A connection whose profile LinkedIn left out (a closed account): counted, not a row.
    connection("urn:li:fsd_connection:3", "urn:li:fsd_profile:GONE", Date.UTC(2025, 5, 1, 12)),
  ],
};

test("rows come in LinkedIn's order with name, headline, date and the 100 px picture", () => {
  const { rows, size } = connectionRows(ANSWER, "UTC");
  assert.equal(size, 3);
  assert.deepEqual(rows, [
    { slug: "jordan-bravo", name: "Jordan Bravo", headline: "", connected_on: "October 2, 2026", picture: "" },
    { slug: "casey-lane", name: "Casey Lane", headline: "Engineer at Example", connected_on: "January 1, 2026",
      picture: "https://media.licdn.com/dms/image/v2/X/100.jpg" },
  ]);
});

test("an empty page is the end of the list", () => {
  assert.deepEqual(connectionRows({ data: { "*elements": [] }, included: [] }, "UTC"), { rows: [], size: 0 });
});

test("the request pages newest first, 40 at a time, from start", () => {
  const url = new URL(connectionsUrl(80));
  assert.equal(url.pathname, "/voyager/api/relationships/dash/connections");
  assert.deepEqual([url.searchParams.get("start"), url.searchParams.get("count"), url.searchParams.get("sortType")],
    ["80", "40", "RECENTLY_ADDED"]);
});
