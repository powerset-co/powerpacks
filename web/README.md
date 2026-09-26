Created: 2026-09-26

# web

The local UI's React app (Vite 5, React 18, TypeScript strict, Tailwind 3 +
shadcn/ui). Same stack and conventions as the hosted `network-search-app`.

`dist/` is committed. The Python review server serves `dist/people.js` and
`dist/people.css` directly, and installs have no node, so every source change
must be rebuilt and the rebuilt `dist/` committed with it.

## Build

```bash
cd web
pnpm install
pnpm build       # writes dist/people.js + dist/people.css (fixed names, no hashes)
pnpm typecheck
pnpm test
```

`pnpm dev` serves `index.html` and proxies `/api` and `/people/assets` to the
Python server on `http://127.0.0.1:8765`.

## Layout

Page -> sections -> shared components -> ui primitives. Only `lib/api` talks to the
server; everything else reads typed values.

| Path | Role | Reads / writes |
| --- | --- | --- |
| `src/pages/people/` | Entry (`main.tsx`), `PeoplePage` (load), `PeopleWorkspace` (state wiring), `PeopleShell`, `PeopleLoading`, `BulkBar`, keyboard wiring | Hooks below; renders the sections |
| `src/pages/people/head/` | Decision tabs with rolling counts and the sliding ink | View tab, totals |
| `src/pages/people/filters/` | Quick filters, search box, active facet chips | View filters and text |
| `src/pages/people/rail/` | Facet rail, "More filters", label search, shortcuts hint | Facet counts; toggles filters |
| `src/pages/people/table/` | Virtualized table: head, rows, cells, columns, `ROW_H` | Matching people, selection, focus |
| `src/pages/people/drawer/` | Person drawer: header, actions, sections, person-switch fade | Person detail; writes share tags |
| `src/pages/people/styles/` | Page CSS ported from `share/web/people.css`: shell, rail, table, drawer, overlays | — |
| `src/components/shared/` | Shared pieces, one home each: avatar, source pill, LinkedIn/channel icons, search field, tab ink, toast, virtual rows, facet shell | Props only |
| `src/components/ui/` | shadcn/ui primitives (button, badge, skeleton) | Props only |
| `src/hooks/` | View state (`useFilters`), selection, drawer, person detail, decisions (writes), row entrance, presence, reduced motion | `lib/api`, `sessionStorage` |
| `src/lib/api/` | `GET /api/people/rows`, `GET /api/people/person`, `POST /api/people/tags`, avatar URL | The review server |
| `src/lib/people/` | Page copy and labels, facets and quick filters, the filter/count pass, test fixture | Typed rows |
| `src/lib/storage.ts`, `src/lib/sets.ts`, `src/lib/utils.ts` | Saved view (sessionStorage), immutable set helpers, `cn()` | `sessionStorage` |
| `src/types/` | API shapes and closed vocabularies (`Decision`, `Worth`, `DecidedBy`, `Channel`) | — |
| `src/styles/index.css` | Tokens and base rules copied from `results_web/results.css`, shared keyframes, `.rise` overlay motion | — |
| `tailwind.config.ts` | Maps shadcn color names, radii, shadows and durations to those tokens | — |

Styling has three layers: the tokens in `index.css`, Tailwind utilities on shared and ui
components, and the page CSS in `pages/people/styles/`. Motion is CSS transitions on the
`--t-*` / `--ease-*` tokens; there is no animation library.

## Change log

- 2026-09-26: Scaffold with a placeholder People page.
- 2026-09-26: People page ported from share/web/people.js; the review server serves dist/people.{js,css}.
- 2026-09-26: Dropped `motion` and the unused radix/lucide/router/tailwindcss-animate packages; overlays, tab ink and the drawer person switch animate with CSS transitions.
