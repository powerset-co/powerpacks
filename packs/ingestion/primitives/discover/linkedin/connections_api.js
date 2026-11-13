/* LinkedIn's paged connections list, the request its own connections page makes as it
 * scrolls, and the rows read from its answer.
 *
 * Voyager answers in its normalized shape: data["*elements"] lists the connection urns in
 * order (newest first), and `included` holds the connections and the profiles they point to.
 * A row is {slug, name, headline, connected_on, picture}, the same as a card read off the page;
 * `size` is how many connections the page held, rows or not (the next page starts after them).
 *
 * Created: 2026-11-13
 */
const PAGE_SIZE = 40;
const DECORATION = "com.linkedin.voyager.dash.deco.web.mynetwork.ConnectionListWithProfile-16";

function connectionsUrl(start) {
  return `https://www.linkedin.com/voyager/api/relationships/dash/connections?decorationId=${DECORATION}`
    + `&count=${PAGE_SIZE}&q=search&sortType=RECENTLY_ADDED&start=${start}`;
}

// The 100 px picture (or the smallest larger one), as a full https link.
function pictureOf(profile) {
  const image = profile.profilePicture?.displayImageReference?.vectorImage;
  const sizes = (image?.artifacts || []).filter((artifact) => artifact.fileIdentifyingUrlPathSegment)
    .sort((a, b) => a.width - b.width);
  const artifact = sizes.find((size) => size.width >= 100) || sizes[sizes.length - 1];
  return image?.rootUrl && artifact ? image.rootUrl + artifact.fileIdentifyingUrlPathSegment : "";
}

// "October 2, 2026", the way the page writes "Connected on ...".
const connectedOn = (ms, timeZone) => (ms ? new Date(ms).toLocaleDateString("en-US",
  { month: "long", day: "numeric", year: "numeric", timeZone }) : "");

function connectionRows(answer, timeZone) {
  const included = answer.included || [];
  const byUrn = new Map(included.map((entity) => [entity.entityUrn, entity]));
  const isConnection = (entity) => /\.Connection$/.test(entity?.$type || "");
  const urns = answer.data?.["*elements"];
  const connections = urns ? urns.map((urn) => byUrn.get(urn)).filter(isConnection) : included.filter(isConnection);
  const rows = [];
  for (const connection of connections) {
    const profile = byUrn.get(connection["*connectedMemberResolutionResult"] || connection.connectedMember);
    if (!profile?.publicIdentifier) continue;
    rows.push({ slug: profile.publicIdentifier,
                name: [profile.firstName, profile.lastName].filter(Boolean).join(" "),
                headline: profile.headline || "", connected_on: connectedOn(connection.createdAt, timeZone),
                picture: pictureOf(profile) });
  }
  return { rows, size: connections.length };
}

module.exports = { PAGE_SIZE, connectionsUrl, connectionRows };
