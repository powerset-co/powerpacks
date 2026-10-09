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

| Role | Size / weight | Treatment |
| --- | --- | --- |
| Page or search title | 18–20px / 600 | Short, sentence case |
| Table name or primary value | 13–14px / 600 | Strong neutral ink |
| Body and controls | 13px / 400–500 | Line height 1.45–1.5 |
| Reasoning and secondary details | 12px / 400 | Line height 1.5 |
| Dense headers, metadata, badges | 10–12px / 600 | Uppercase only for short headers |

Avoid large marketing headlines inside the app. Keep long evidence regular
weight; use spacing and hierarchy before adding colors or bold text.

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
A page's own panel (the chat list, saved searches, the People filters) sits beside the nav,
inside that page. Logbook belongs
inside People: use the Has logbook filter and a person's View logbook action,
not another top-level tab. An existing archive offers View logbook; Refresh
logbook lives in the reader.

Keep only the share action beside the decision tabs; those tabs already show
the totals. Quick filters omit zero counts and sort by descending count within
the selected tab. Has logbook stays last when available, regardless of count.

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
