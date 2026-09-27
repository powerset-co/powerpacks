Created: 2026-09-26

# web

The local UI's React app (Vite 5, React 18, TypeScript strict, Tailwind 3 +
shadcn/ui). Same stack and conventions as the hosted `network-search-app`.

`dist/` is committed. The Python review server serves `dist/app.js` and
`dist/app.css` at `/app/assets/` (`packs/shared/web/app.py`), and installs have no
node, so every source change must be rebuilt and the rebuilt `dist/` committed with it.

One React root: `src/main.tsx` mounts `App`, whose shell renders the top bar once and
the routed page below it, so pages switch without a document reload. The router claims
`/people`, `/searches` and `/searches/run?run_id=…`; the server answers all three with the
shell (`packs/shared/web/app.py` `PAGE_PATHS`).

## Build

```bash
cd web
pnpm install
pnpm build       # writes dist/app.js + dist/app.css (fixed names, no hashes)
pnpm typecheck
pnpm test
```

`pnpm dev` serves `index.html` (open `/people`) and proxies `/api` to the Python
server on `http://127.0.0.1:8765`.

## Layout

Page -> sections -> shared components -> ui primitives. Only `lib/api` talks to the
server; everything else reads typed values.

| Path                                                        | Role                                                                                                                                      | Reads / writes                    |
| ----------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------- |
| `src/main.tsx`, `src/App.tsx`                               | The one root: query client, router, the shell (top bar + routed page)                                                                     | —                                 |
| `src/pages/people/`                                         | `PeoplePage` (load), `PeopleWorkspace` (state wiring), `PeopleShell`, `PeopleLoading`, `BulkBar`                                          | Its hooks; renders the sections   |
| `src/pages/people/hooks/`                                   | People-typed hooks: view state (`useFilters`), rows query, drawer + person detail, decisions (writes), keyboard wiring                    | `lib/api`, `lib/people/view`      |
| `src/pages/people/head/`                                    | Decision tabs with rolling counts and the sliding ink                                                                                     | View tab, totals                  |
| `src/pages/people/filters/`                                 | Quick filters, search box, active facet chips                                                                                             | View filters and text             |
| `src/pages/people/rail/`                                    | Facet rail, "More filters", label search, shortcuts hint                                                                                  | Facet counts; toggles filters     |
| `src/pages/people/table/`                                   | Virtualized table: head, rows, cells, columns, `ROW_H`                                                                                    | Matching people, selection, focus |
| `src/pages/people/drawer/`                                  | Person drawer: header, actions, sections, person-switch fade                                                                              | Person detail; writes share tags  |
| `src/pages/people/styles/`                                  | Page CSS ported from `share/web/people.css`: shell, rail, table, drawer, overlays                                                         | —                                 |
| `src/components/shared/`                                    | One home each: avatar, chip, source pill (sm/md), channel icons, search field, sort header, tab ink, toast, virtual rows, facet shell     | Props only                        |
| `src/components/ui/`                                        | shadcn/ui primitives (button, badge, dialog, skeleton)                                                                                    | Props only                        |
| `src/hooks/`                                                | Page-agnostic hooks: keyboard, selection (keyed by the caller), presence, reduced motion, list entrance (`useListEntrance`)               | —                                 |
| `src/lib/api/`                                              | `GET /api/people/rows`, `GET /api/people/person`, `POST /api/people/tags`, avatar URL                                                     | The review server                 |
| `src/lib/people/`                                           | Page copy and labels, facets and quick filters, the filter/count pass, the saved view and its parser                                      | Typed rows, `sessionStorage`      |
| `src/lib/channels.ts`                                       | The source-family vocabulary: `Channel`, titles and pill colours, `toChannels`                                                            | —                                 |
| `src/lib/nav.ts`                                            | The top bar's pages (`PAGES`) and `pageAt(pathname)`                                                                                      | —                                 |
| `src/lib/storage.ts`, `src/lib/sets.ts`, `src/lib/utils.ts` | Generic `readSession`/`writeSession`, immutable set helpers, `cn()`                                                                       | `sessionStorage`                  |
| `src/types/`                                                | API shapes, `PERSON_COLUMNS` (pinned to model.py by `tests/test_share_web.py`) and closed vocabularies (`Decision`, `Worth`, `DecidedBy`) | —                                 |
| `src/testing/`                                              | Synthetic columnar fixture for the vitest suites                                                                                          | —                                 |
| `src/styles/index.css`                                      | Tokens and base rules copied from `results_web/results.css`, shared keyframes, `.rise` overlay motion                                     | —                                 |
| `tailwind.config.ts`                                        | Maps shadcn color names, radii, shadows and durations to those tokens                                                                     | —                                 |

Styling has three layers: the tokens in `index.css`, Tailwind utilities on shared and ui
components, and the page CSS in `pages/people/styles/`. Motion is CSS transitions and
keyframes, Web Animations for the row entrance, and requestAnimationFrame for the count roll,
all on the `--t-*` / `--ease-*` tokens; there is no animation library.

## Searches

`/searches` lists the saved searches; `/searches/run?run_id=…` is the same page with that run
open. Picking a run pushes the URL, so back, forward and reload land on it.

| Path                                    | Role                                                                                                      |
| --------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `SearchesPage.tsx`, `SearchesShell.tsx` | Reads `run_id`, loads the catalog; the sidebar in People's rail column beside the run pane                |
| `sidebar/`                              | Search box, company and status selects, version chips (newest preselected, "All"), recency groups, keys   |
| `run/RunPane.tsx`                       | The main pane: empty state, loading, error; crossfades between runs (`hooks/useRunSwap`)                  |
| `run/SearchRun.tsx`                     | Header, pond chain, toolbar slot, the virtualized results table, the team fold                            |
| `run/ResultRow.tsx`, `run/Evidence.tsx` | One person; the row opens in place to traits, pin judge, about, roles (matched marked) and schools        |
| `lib/ranking.ts`                        | `rendering.py` ordering: Jev and rating scales never mixed, overall then screen score, missing never zero |
| `lib/catalog.ts`, `lib/copy.ts`         | The sidebar filter and groups; status, date and money words                                               |

Slots, for the tags, feedback and export work: `SearchRun` takes

- `toolbar?: ReactNode`: rendered between the pond chain and the table.
- `headerActions?: ReactNode`: rendered at the right of the run header.
- `rowActions?: (candidate: PondCandidate) => ReactNode`: rendered at the end of each row,
  outside the row's toggle button.
- `tags?: Tagged | null`: each row shows `tags.assignments[person_id]` as chips.

`SearchesPage` passes `RunPane` a `renderRun(payload)`; to fill the slots, render a component
there that calls the run's hooks (e.g. its tags) and returns `<SearchRun … />`.

## Change log

- 2026-09-26: Searches page in the shell: sidebar + run view, `/searches` and `/searches/run` routes, NavTabs are NavLinks with the sliding ink; `useRowEntrance` became `hooks/useListEntrance`.

- 2026-09-26: One React root: `main.tsx` + `App.tsx` (router, shell with the top bar); build is `dist/app.{js,css}` served at `/app/assets/`.
- 2026-09-26: ESLint strict type-checked gate + Prettier (two spaces, no semicolons), `pnpm check`; casts and non-null assertions removed except the three in LINT-WAIVERS.md; the row entrance plays on the view's first paint at the tokens' real durations (the minified CSS says `.2s`), every mounted row; the drawer slides from off-screen (--t-slow).

- 2026-09-26: Scaffold with a placeholder People page.
- 2026-09-26: People page ported from share/web/people.js; the review server serves dist/people.{js,css}.
- 2026-09-26: Dropped `motion` and the unused radix/lucide/router/tailwindcss-animate packages; overlays, tab ink and the drawer person switch animate with CSS transitions.
- 2026-09-26: Review fixes: channel vocabulary in `lib/channels.ts`, People hooks under `pages/people/hooks/`, validated saved view, `PERSON_COLUMNS` checked at decode, nav registry, pill buttons, motion for sections, chips, "More filters" and the sort arrow.
