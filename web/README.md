Created: 2026-09-26

Change log:

- 2026-10-01: the Review page (`/review`) and its routes, folders and fixtures.
- 2026-10-01: the Review page is `/`; the Jinja page it replaced is deleted.
- 2026-10-02: `/install` shows installation, sign-in, search access, and network
  checks from the bootstrap manifest, including waiting and skipped steps.

# web

The local UI's React app (Vite 5, React 18, TypeScript strict, Tailwind 3 +
shadcn/ui). Same stack and conventions as the hosted `network-search-app`.

`dist/` is committed. The Python review server serves `dist/app.js` and
`dist/app.css` at `/app/assets/` (`packs/shared/web/app.py`), and installs have no
node, so every source change must be rebuilt and the rebuilt `dist/` committed with it.

One React root: `src/main.tsx` mounts `App`, whose shell renders the top bar once and
the routed page below it, so pages switch without a document reload. The router claims
`/people`, `/searches` and `/searches/run?run_id=…`, and `/` (the review flow) outside the
shell; `/install` is the installer status page. The server answers each with the
app page (`packs/shared/web/app.py` `PAGE_PATHS`).

## Build

```bash
cd web
pnpm install
pnpm build       # writes dist/app.js + dist/app.css (fixed names, no hashes)
pnpm typecheck
pnpm test
pnpm check       # typecheck, lint (zero warnings), format check, tests
```

Lint is strict and type-checked; every `eslint-disable` in `src/` names a waiver in
[`LINT-WAIVERS.md`](LINT-WAIVERS.md), and a new one needs a row there.

`tests/test_visual.py` screenshots both pages in Chrome (reduced motion) over synthetic
fixtures, served by one handler composed as `review/server.py` composes it, and compares them
to `tests/visual/people/*.png` and `tests/visual/searches/*.png` (fails above 0.4% changed
pixels). `UPDATE_VISUAL=1` rewrites the baselines; do that only for a change meant to move pixels.

`pnpm dev` serves `index.html` (open `/people`) and proxies `/api` to the Python
server on `http://127.0.0.1:8765`.

## Layout

Page -> sections -> shared components -> ui primitives. Only `lib/api` talks to the
server; everything else reads typed values.

| Path                                                        | Role                                                                                                                                                    | Reads / writes                    |
| ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------- |
| `src/main.tsx`, `src/App.tsx`                               | The one root: query client, router, the shell (top bar + routed page)                                                                                   | —                                 |
| `src/pages/people/`                                         | `PeoplePage` (load), `PeopleWorkspace` (state wiring), `PeopleShell`, `PeopleLoading`, `BulkBar`                                                        | Its hooks; renders the sections   |
| `src/pages/people/hooks/`                                   | People-typed hooks: view state (`useFilters`), rows query, drawer + person detail, decisions (writes), the page's keys (`useKeyboard`) and their wiring | `lib/api`, `lib/people/view`      |
| `src/pages/people/head/`                                    | Decision tabs with rolling counts and the sliding ink                                                                                                   | View tab, totals                  |
| `src/pages/people/upload/`                                  | Share button (last upload beside it) and its dialog: `useUpload` (poll while running), plan summary, progress bar, result                               | `lib/api/upload`, page toast      |
| `src/pages/people/filters/`                                 | Quick filters, search box, active facet chips                                                                                                           | View filters and text             |
| `src/pages/people/rail/`                                    | Facet rail, "More filters", label search, shortcuts hint                                                                                                | Facet counts; toggles filters     |
| `src/pages/people/table/`                                   | Virtualized table: head, rows, cells, columns, `ROW_H`                                                                                                  | Matching people, selection, focus |
| `src/pages/people/drawer/`                                  | Person drawer (in the shared `Drawer`): header, actions, sections                                                                                       | Person detail; writes share tags  |
| `src/pages/people/styles/`                                  | Page CSS ported from `share/web/people.css`: shell, rail, table, drawer sections                                                                        | —                                 |
| `src/components/shared/`                                    | One home each (table below); imported through `index.ts`                                                                                                | Props only                        |
| `src/components/ui/`                                        | shadcn/ui primitives (button, badge, dialog, skeleton)                                                                                                  | Props only                        |
| `src/hooks/`                                                | Page-agnostic hooks (table below)                                                                                                                       | —                                 |
| `src/lib/api/`                                              | `http.ts` (`failure`, `body`: W1), `people.ts`, `searches.ts` (catalog, run, tags), `feedback.ts` (`POST /searches/feedback`)                           | The review server                 |
| `src/lib/people/`                                           | Page copy and labels, facets and quick filters, the filter/count pass, the saved view and its parser                                                    | Typed rows, `sessionStorage`      |
| `src/lib/advance.ts`                                        | `nextOpenIndex`: which row a drawer opens after its row was labeled (People) or scored (Searches)                                                       | —                                 |
| `src/lib/channels.ts`                                       | The source-family vocabulary: `Channel`, titles and pill colours, `toChannel(s)` (the search server's `twitter` is X, `phone` iMessage)                 | —                                 |
| `src/lib/copy.ts`                                           | Words both pages use: `plural`, `countOf` ("N of M people"), `monthYear`                                                                                | —                                 |
| `src/lib/nav.ts`                                            | The top bar's pages in order (`PAGES`), `HOME`, and `pageAt(pathname)` (unknown paths are `HOME`)                                                       | —                                 |
| `src/lib/storage.ts`, `src/lib/sets.ts`, `src/lib/utils.ts` | `readStored`/`writeStored` (session or local, blocked storage tolerated); immutable set helpers; `cn()`, `isRecord`                                     | `sessionStorage`, `localStorage`  |
| `src/lib/motion.ts`, `src/lib/must.ts`                      | Motion tokens read back for Web Animations; `must()`, the one non-null assertion                                                                        | —                                 |
| `src/types/`                                                | API shapes, `PERSON_COLUMNS` (pinned to model.py by `tests/test_share_web.py`) and closed vocabularies (`Decision`, `Worth`, `DecidedBy`)               | —                                 |
| `src/testing/`                                              | Synthetic fixtures for the vitest suites: `people-fixture.ts` (columnar), `searches-fixture.ts` (rows, operators, ratings, `MemoryStorage`)             | —                                 |
| `src/styles/index.css`                                      | Tokens and base rules copied from `results_web/results.css`, shared keyframes, `.rise` / `.rise-in` motion, `.chevron`, `.focus-bar`, `.section-title`  | —                                 |
| `tailwind.config.ts`                                        | Maps shadcn color names, radii, shadows and durations to those tokens                                                                                   | —                                 |

### Shared components (`src/components/shared/`)

| Component                          | Role                                                                                                                                                            |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `TopBar`, `NavTabs`, `TabInk`      | Brand cell and page tabs (`lib/nav.ts` `PAGES`); the one sliding underline                                                                                      |
| `Avatar`, `initials`               | Initials with the picture fading in over them                                                                                                                   |
| `Chip`, `Kbd`, `EmptyState`        | Filter pill with optional count; key cap; centred muted line that rises in                                                                                      |
| `SourcePill`, `SourcePills`, icons | Channel pill (sm/md, optional count) and a person's pills; `icons/channels.tsx` and `icons/actions.tsx` (plus, pin, flag, `CLOSE_MARK`), pinned by `test_icons` |
| `SearchField`, `SortHeader`        | The one text box style; a sortable column header                                                                                                                |
| `FacetShell`, `FacetValue`, `Fold` | A facet (head + folded values), one value, the height fold (inert while shut)                                                                                   |
| `DetailsSection`                   | A titled section (heading toggle, count, badge) whose body folds both ways; drawer, team, job description                                                       |
| `Appear`                           | Rises in when shown and drops out before unmounting (inert while leaving)                                                                                       |
| `CountRoll`, `Toast`               | A count that rolls; the page toast (rises in, keeps its last message as it leaves)                                                                              |
| `VirtualRows`                      | The virtualized list (fixed row height)                                                                                                                         |
| `Drawer`, `DrawerClose`            | The right-hand panel both pages open a row in: slides in and out, its content keyed and crossfaded (`hooks/useDrawerSwap`), inert while shut                    |
| `ActionBar`, `ActionBarRule`       | The floating bottom bar (label, then the page's buttons); rises in and drops out                                                                                |

`src/components/ui/`: shadcn/ui `button`, `badge`, `dialog`, `skeleton`.

### Hooks

| Hook                                  | Where                   | Role                                                                                                                                                                                 |
| ------------------------------------- | ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `useSelection`                        | `hooks/`                | Selected ids; select-all takes every matching item                                                                                                                                   |
| `usePresence`, `usePresenceList`      | `hooks/`                | Keep an overlay / removed items mounted until their exit's opacity ends                                                                                                              |
| `useKeys`, `useInert`, `useDismiss`   | `hooks/`                | The page's keydown listener and `isTyping`; inert while shut; Escape / outside press closes a panel                                                                                  |
| `useReducedMotion`, `useListEntrance` | `hooks/`                | The OS motion setting; rows fading in after the list changes                                                                                                                         |
| `useDrawerSwap`                       | `hooks/`                | What a drawer draws while it switches item: the old content fades out, then the new fades in                                                                                         |
| `useFilters`, `usePeopleQuery`        | `pages/people/hooks/`   | View state (saved to `sessionStorage`); every person once                                                                                                                            |
| `useDrawer`, `usePersonDetail`        | `pages/people/hooks/`   | Which person the drawer shows; their detail                                                                                                                                          |
| `useDecisions`                        | `pages/people/hooks/`   | Share / keep private / use worth in one write, with undo                                                                                                                             |
| `useKeyboard`, `usePeopleShortcuts`   | `pages/people/hooks/`   | The page's keys and their wiring                                                                                                                                                     |
| `useCatalog`, `useSearchRun`          | `pages/searches/hooks/` | The saved searches; one run                                                                                                                                                          |
| `useRunSwap`                          | `pages/searches/hooks/` | The crossfade between runs                                                                                                                                                           |
| `useSearchTags`, `useFeedback`        | `pages/searches/hooks/` | A run's tags (saved in order; browser-kept tags moved to the server); the open run's feedback queue, its failure, Retry and sign-in                                                  |
| `useSidebarKeys`, `useResultKeys`     | `pages/searches/hooks/` | Sidebar keys; result and review keys (both on `useKeys`)                                                                                                                             |
| `useFloatPanel`                       | `pages/searches/hooks/` | A fixed panel under a trigger (tag editor, source popovers): placed inside the window, flipped above when it would run off the bottom, closed by Escape, a press outside or a scroll |

Styling has three layers: the tokens in `index.css`, Tailwind utilities on shared and ui
components, and the page CSS in `pages/people/styles/`. Motion is CSS transitions and
keyframes, Web Animations for the row entrance, and requestAnimationFrame for the count roll,
all on the `--t-*` / `--ease-*` tokens; there is no animation library.

## Review

`/` is the deep-context review flow (worth, Enrich, LinkedIn, done), the page `bin/deep-context
review` opens (`/?stage=worth|enrich|linkedin|done`). It is the user's entry point, so it stands
OUTSIDE the app shell: no page tabs, its own top bar (brand and the screen's title). One screen
per URL (`stage`, `view`, `preview`, `debug`, `index`); moving between stages and tabs is
client-side, and every navigation reads the screen again. It was ported from a Jinja page and
`reconcile_review.js`, which are deleted; its words and behaviour are theirs.

| Path                                                            | Role                                                                                                                                           | Reads / writes                                                                                                                |
| --------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `src/types/review.ts`                                           | The contract, field for field with `deep_context/review/payloads.py` (pinned by `tests/test_deep_context_review_api.py`)                       | —                                                                                                                             |
| `src/lib/api/review.ts`                                         | One function per route; `ReviewError` carries the status (`gone`, `needsAuth`)                                                                 | `/api/review/*`, `/worth`, `/complete`, `/retarget`, `/feedback`, `/auth/login`, `/api/status`, `/api/events`, `/api/dossier` |
| `src/lib/review/`                                               | Pure rules: guidance routing (URL is the free fix, text the paid re-research), step markers, the status feed-forward, links, timings, copy     | —                                                                                                                             |
| `src/pages/review/ReviewPage.tsx`, `ReviewScreen.tsx`, `hooks/` | The page, one loaded screen (stepper + stage), the `useReview()` context a stage works through, the server watch (Enrich and Done only)        | `lib/api/review`                                                                                                              |
| `src/pages/review/shared/`                                      | What more than one stage draws: person card, fact list, dossier, scroll cue, badges, empty panel, handoff copy, carousel, stage check, stepper | Props; `Dossier` reads `/api/dossier`                                                                                         |
| `src/pages/review/worth/`                                       | The card queue (prefetch, optimistic counts, typeahead) and the Yes / No tables                                                                | `worth-card`, `worth-pending`, `worth-table`, `POST /worth`                                                                   |
| `src/pages/review/enrich/`, `done/`                             | The Enrich panel (approve, live progress, Continue) and All set                                                                                | `approve-enrichment`, `POST /complete`                                                                                        |
| `src/pages/review/linkedin/`                                    | The identity card, guidance box, person menu and feedback, finished state                                                                      | `linkedin-card`, `decide`, `POST /retarget`, `/feedback`, `/complete`                                                         |
| `src/pages/review/styles/`                                      | The old page's CSS ported under `.review-page`: `base.css`, then one file per stage                                                            | —                                                                                                                             |
| `src/testing/review-fixture.ts`, `review-harness.tsx`           | Synthetic payload builders, `FakeEventSource`, and the harness a stage test renders in                                                         | —                                                                                                                             |

## Searches

`/searches` lists the saved searches; `/searches/run?run_id=…` is the same page with that run
open. Picking a run pushes the URL, so back, forward and reload land on it. Searches logic lives
in `src/lib/searches/` (as People's does in `lib/people/`); `src/pages/searches/` holds the
components and hooks.

| Path (`src/pages/searches/`)            | Role                                                                                                                                                                                                                   |
| --------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `SearchesPage.tsx`, `SearchesShell.tsx` | Reads `run_id`, loads the catalog; owns the feedback queue (`useFeedback`) and the page's one `Toast`                                                                                                                  |
| `sidebar/`                              | Search box and recency groups; the search kept for the tab                                                                                                                                                             |
| `run/RunPane.tsx`                       | Empty state (with the keys), loading, error; crossfades between runs (`hooks/useRunSwap`)                                                                                                                              |
| `run/RunView.tsx`                       | One run's wiring: its tags (`useSearchTags`), the filters, the rows they keep, each person's own score                                                                                                                 |
| `run/SearchRun.tsx`                     | Header, pond chain, toolbar, the virtualized results table, the team fold                                                                                                                                              |
| `run/ResultsTable.tsx`, `ResultRow.tsx` | Measured rows laid out as `rendering.py`'s candidate rows (identity, sources and operators left; overall, reasoning and labels right), focus, the open row, keys (`hooks/useResultKeys`) and the advance after a score |
| `run/NetworkSources.tsx`                | The source pills and operator stack under a name; each opens who brought that source (with counts) on hover or click, on `hooks/useFloatPanel`                                                                         |
| `run/ResultDrawer.tsx`, `ReviewBar.tsx` | The candidate in the shared drawer (identity, overall and labels, `Evidence`, `Career`, `ConnectedVia`, team likeness); the rubric, Tag, Pin and Close bar                                                             |
| `run/RowActions.tsx`, `PinButton.tsx`   | `RowTags` (tag editor and the `Pinned` pin over the identity) and `RowScore` (the score badge over the reasoning)                                                                                                      |
| `toolbar/`                              | Tagged, Labels, Overall score, Operators, tag filter, count, Copy/CSV, Untag and Clear all                                                                                                                             |
| `dialogs/`                              | Score dialog (five-point rubric) and search feedback dialog, both handing records to `useFeedback`; `FeedbackStatus`, the stopped queue's count with Retry or sign-in                                                  |
| `styles/`                               | Page CSS: shell, sidebar, run, results, toolbar (panels, `.rise-in`, `.rise-settled`)                                                                                                                                  |

| Path (`src/lib/searches/`) | Role                                                                                           |
| -------------------------- | ---------------------------------------------------------------------------------------------- |
| `ranking.ts`               | `rendering.py` ordering: Jev and rating scales never mixed, missing never zero; the panel      |
| `filters.ts`               | The toolbar's filter (`keptRows` for the table, `filterRows` one per person), labels toggle    |
| `tags.ts`, `exports.ts`    | Pure tag edits; CSV and clipboard of the filtered rows                                         |
| `feedback.ts`, `rubric.ts` | Feedback records and their queue, the person's own score; the rubric as choices                |
| `catalog.ts`, `copy.ts`    | The sidebar filter (and its saved form) and groups; status, date, money and people-count words |
| `sources.ts`               | A candidate's source pills (busiest first, email and message counts) and operator lines        |

The filtered rows are computed once, in `RunView`: the table shows them and the toolbar counts
them, so the count never disagrees with the table. Export and copy take every filtered person,
never only the mounted rows, and write the person's own score when they gave one.

Keys: the arrows move the sidebar, Enter opens the highlighted run, `/` searches the list. In a
run, `j`/`k` move the focused person (an open drawer follows), Enter opens or closes the drawer on
them, Escape closes it, `t` opens their tag editor, `s` their score dialog and `p` toggles their
pin. With the drawer open, a rubric digit (`1`–`5`) saves that score with no note and opens the
next person (`lib/advance.ts`); a save in the score dialog does the same, and past the last
person the drawer closes. No key acts while typing in a field or inside a dialog.

## Change log

- 2026-09-27: The share dialog rebuilt on the upload status contract: the button leaves `DecisionTabs` for the head's right edge with the last upload beside it; opening shows the saved status (only a never-checked network checks); one column of plan, one people-counting bar, the result; a finished background run toasts. Phase logic in `lib/people/upload.ts`, words in `lib/people/copy.ts`.
- 2026-09-26: Searches opens a candidate in a right-hand drawer (the People drawer, moved to `components/shared/Drawer` with `hooks/useDrawerSwap`) with a review bar (`components/shared/ActionBar`, People's bulk bar generalized): rubric digits score and advance (`lib/advance.ts`, shared with People), `t`/`p`/Escape; the inline evidence, the row caret and `VirtualRows`' `measure` mode are gone.
- 2026-09-26: Review fixes: result rows show every source family with counts, the location and who each source came through; the job description and the team fold with `DetailsSection` (now a heading toggle over a `Fold`, animated both ways); the feedback queue sends only the open run's records and shows a stopped queue with Retry or a Powerset sign-in; browser-kept tags move to the server; the catalog's people count says pinned and 5/4/3; one `useKeys`, `useInert`, `errorText`, `isRecord`, storage helper, count copy, chevron, focus bar and glyph set; facet keys are a closed `FacetKey`; presence unmounts on the exit's opacity; the People skeleton matches the loaded layout.

- 2026-09-26: Shared components and hooks tables, waivers pointer; `tests/test_visual.py` covers People and Searches.
- 2026-09-26: Searches run view wired: toolbar filters the table, tags and pin, score and search feedback dialogs, one page toast, result keys; Searches logic in `lib/searches/`, feedback types in `types/searches.ts`, sidebar filters kept for the tab.
- 2026-09-26: Nav tabs render from `PAGES`; the shell resolves the page once and unknown paths redirect to `/people`; `useKeyboard` moved to `pages/people/hooks/`; WhatsApp draws the Simple Icons mark in its own green.
- 2026-09-26: Searches page in the shell: sidebar + run view, `/searches` and `/searches/run` routes, NavTabs are NavLinks with the sliding ink; `useRowEntrance` became `hooks/useListEntrance`.

- 2026-09-26: One React root: `main.tsx` + `App.tsx` (router, shell with the top bar); build is `dist/app.{js,css}` served at `/app/assets/`.
- 2026-09-26: ESLint strict type-checked gate + Prettier (two spaces, no semicolons), `pnpm check`; casts and non-null assertions removed except the three in LINT-WAIVERS.md; the row entrance plays on the view's first paint at the tokens' real durations (the minified CSS says `.2s`), every mounted row; the drawer slides from off-screen (--t-slow).

- 2026-09-26: Scaffold with a placeholder People page.
- 2026-09-26: People page ported from share/web/people.js; the review server serves dist/people.{js,css}.
- 2026-09-26: Dropped `motion` and the unused radix/lucide/router/tailwindcss-animate packages; overlays, tab ink and the drawer person switch animate with CSS transitions.
- 2026-09-26: Review fixes: channel vocabulary in `lib/channels.ts`, People hooks under `pages/people/hooks/`, validated saved view, `PERSON_COLUMNS` checked at decode, nav registry, pill buttons, motion for sections, chips, "More filters" and the sort arrow.
