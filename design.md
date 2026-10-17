# Powerpacks design

A quiet, precise workspace for reviewing people and searches. Dense tables,
readable evidence, restrained motion. Keep the existing warm charcoal and
orange identity consistent across every local page.

## Shared implementation

`packs/search/primitives/deep_search/results_web/results.css` owns the shared
color, typography, radius and motion tokens for the legacy results page and the
hosted snapshot; the React app copies them into `web/src/styles/index.css`
(keep the two in sync). People and the local Searches page are the React app
(`web/`, shared components in `web/src/components/shared`).
`packs/shared/web/virtual-table.js` owns visible-row rendering for the legacy
results page, company employees and Worth Yes/No lists, using the vendored
TanStack Virtual core.
Extend these before adding another table implementation or page-specific theme.

## Typography

Use the native system sans stack: `-apple-system, BlinkMacSystemFont, "Segoe UI",
system-ui, sans-serif`. Use monospace only for identifiers and versions.
Use tabular numerals for counts, scores, dates and cost.

Five sizes, nothing in between (no 10.5, 11.5, 12.5 or 14): a size that is not
one of these is a bug.

| Role | Size / weight | Treatment |
| --- | --- | --- |
| Page or search title | 20px / 600 | Short, sentence case |
| Drawer name, dialog title | 15px / 600 | Strong neutral ink |
| Body, controls, table name or primary value | 13px / 400–600 | Line height 1.45–1.5 |
| Reasoning and secondary details | 12px / 400 | Line height 1.5 |
| Dense headers, metadata, badges, chips, keys | 11px / 600 | Uppercase only for short headers |

Avoid large marketing headlines inside the app. Keep long evidence regular
weight; use spacing and hierarchy before adding colors or bold text.

Casing: labels and sentences are sentence case ("Refresh signals", "Not worth
it"). A chip that shows a machine value (a seniority band, a keyword, a role id,
a stage) is title case with hyphens kept ("Vice-President", "Software Engineer",
"Series C"), through `titleCase` in `web/src/lib/people/copy.ts`; `sentence`
does the sentence-case version. Raw enums, ids and internal statuses
(`SERIES_C`, `role_ids`, `needs_you`) never reach a page.

## Colors and branding

| Token | Value | Use |
| --- | --- | --- |
| `--background` | `#1a1614` | Page canvas |
| `--card` | `#242120` | Main surfaces |
| `--surface-2` | `#2c2826` | Raised or selected surfaces |
| `--foreground` | `#f0eae2` | Primary text |
| `--muted` | `#9c8e84` | Supporting text |
| `--border` | `#3a3432` | Subtle component boundaries |
| `--primary` | `#dd3d17` | Brand mark, active filters, focus |
| `--ok` | `#4ade80` | Completed / positive state |
| `--warn` | `#fbbf24` | Attention needed |
| `--bad` | `#f87171` | Error or negative state |

Use the small orange mark and POWERPACKS wordmark in the top bar. Orange is an
accent, not a large background. Pair status colors with text. Neutral evidence
text stays neutral. Reserve shadows for overlays; regular rows use thin dividers.
No decorative gradients, neon glows, emoji icons or stacked card borders.

## Spacing and shape

Use a 4px rhythm: 4, 8, 12, 16, 20, 24, 32, 40, 48. A 2px adjustment is fine
for icon alignment. Use 8–12px between controls, 12–16px inside compact surfaces,
and 24px between sections. Page gutters are 20–24px on desktop and 12–16px on
small screens. Search content caps at 1240px; People uses the available width.

Keep radii at 6px for controls, 10px for panels, 14px for dialogs. Pills mean
filters or labels. The top bar is 52px tall. People rows are 36px; Search rows
size to readable reasoning. Touch controls grow to at least 44px.

## One home per action

A control appears once per screen. The action bar owns every call on the
selection or the open person (Keep, Skip, Delete, Logbook, Close), with its key
printed on the button; a drawer shows readouts (the Decision badge), never a
second copy of the bar's buttons. Hover-only actions on a row are a smell: if an
action matters it is on the bar.

Status text exists only while the state can still change: a run reads
"Running" or "Needs review", never "Search complete" on every finished item. A
select hides an option with nothing behind it (no "Maybe · 0"). An empty state
says what to do next in one sentence.

A drawer header is the picture, then the name and its lines at full width. Close
and the feedback flag float over the top right corner and take no column; only
the name line stops short of them. Actions on the person (Refresh signals,
Logbook) are labeled pill buttons on one row under the header, not icon rings
beside Close.

## Tables and interactions

Render only the visible rows plus overscan. Keep filters, selection, exports
and counts based on the full result set. Preserve keyed rows and focus when
scrolling or selecting. Measure variable-height search rows; do not clip evidence
to force a fixed row height. Sticky controls should stay with their table.

Filters show their active state immediately. Search's Labels chip controls
Taste, Suggested Pin and Team Similarity; it does not change results, scores or candidate tags.
Company employee tables use the same header typography, dividers and neutral
secondary text. Local employee tables scroll in a 360px viewport; hosted snapshots
keep ten-row pages with shared button styles. Employee names are 13px semibold;
titles, locations and tenure are 12px regular.
People-count popovers show pinned and overall 5/4/3 counts on hover or keyboard
focus. Popovers must stay legible above nearby rows.

## Motion

Use subtle motion to explain changes. Hover/focus feedback: 120ms. Filtered
results: 200ms opacity plus a 6px entrance; stagger visible rows by 14ms, capped
at 100ms. Disclosures: 220ms opening and closing. Drawers use the existing
200ms transition. Default entrance curve: `cubic-bezier(.2, 0, 0, 1)`.

Never replay row entrances while scrolling. Cancel a superseded animation when
users click quickly. No bouncing, looping decoration or long delays before
content becomes usable. Honor `prefers-reduced-motion` in CSS and JavaScript.

## Accessibility and verification

Use native buttons, links and details/summary. Keep visible focus, meaningful
labels and pressed/expanded states. Hover content is also keyboard accessible.
Aim for WCAG AA text contrast (4.5:1 for normal text). Check real content at
small widths, keyboard navigation and reduced motion. Verify empty results,
repeated filtering, row scrolling, export counts and popover placement in-browser.

## References

These inform the principles; Powerpacks' palette, native font, density and exact
motion timings are our existing product choices, not copied Carbon branding.

- [IBM Carbon: spacing](https://carbondesignsystem.com/elements/spacing/overview/)
  — a consistent scale and whitespace that groups related information.
- [IBM Carbon: typography](https://carbondesignsystem.com/elements/typography/overview/)
  — compact product typography, clear hierarchy and neutral running text.
- [IBM Carbon: motion](https://carbondesignsystem.com/elements/motion/overview/)
  — subtle task-focused motion, easing and duration matched to the interaction.
- [W3C: animation from interactions](https://www.w3.org/WAI/WCAG22/Understanding/animation-from-interactions.html)
  — support disabling nonessential interaction-triggered motion.

## People and Logbook

Navigation is a side nav (`web/src/components/shared/Sidebar.tsx`): Chat (desktop app only),
Searches, People, Scheduled tasks, Accounts, with a collapse toggle that leaves an icon rail.
At its foot, who is signed in: their picture, then their name and email, each on one line.
A page's own panel (the chat list, saved searches) sits beside the nav, inside that page.
Every page's title sits in one `PageHeader` (one height, gutter and type) so it never moves
between pages; document pages share one left-aligned 960px column.

People is the title, then one line: the search box, a Decision select (All, Keep, Maybe, Skip,
with counts) and a Relationship select (the kinds in that choice, with counts), with Refresh
signals at its right end (Share network only behind the `share` feature switch). No tabs, no
chips. The decision words are Keep and Skip (the field underneath is still `worth`). Logbook
belongs inside People, not another top-level tab: a labeled pill under the drawer header (View
once saved, Build otherwise) and the selection bar's button; the reader lists every saved
logbook, and Refresh logbook lives there. A flag beside Close sends the Powerset team a note
about that person, as a search's drawer does for a candidate. The drawer's sections read
Decision (folded), Facts and Timeline (open), Relationship, Topics, Dossier, Label confidence.
The account menu at the side nav's foot ends with the app's version.

The reader URL includes the selected conversation, so refresh and copied links
open the same thread. Email participants show names and addresses inline under
From, To and Cc. Reuse the shared colored source icons.

Opening Logbook crossfades over the mounted People view for 350ms, with at most
8px of horizontal movement. Keep a stable reader shell while messages load;
never insert a blank page between views. Back restores the same filters,
selection, drawer and scroll position. Reduced motion removes the animation.

The selected month fills its whole button with the primary color and contrasting
text, rather than a left border. In a person's event timeline, connectors run
between circle centers, behind opaque circles; no line extends above the first
circle or below the last.

## Change log

- 2026-10-17: Five exact type sizes; casing rules (`titleCase` / `sentence`); "One home per
  action" (bar owns the calls, drawer shows readouts, status only while it changes, no
  zero-count options, header layout); People section brought to Keep/Skip, the Decision
  select, the labeled drawer actions and the section order. From the 2026-10-16 design critique.
