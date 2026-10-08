# Lint waivers

Created: 2026-09-26

Change log:

- 2026-09-26: created with the ESLint gate (typescript-eslint strict + stylistic,
  type-checked; react-hooks; jsx-a11y; Prettier: two spaces, no semicolons).
- 2026-09-26: W1 moved to `lib/api/http.ts` (both clients); W2 reworded; the test override
  listed. glm confirmed every waiver and config decision.
- 2026-09-26: W1 names its three clients; W3 points at `pages/people/hooks/useKeyboard`.
- 2026-10-01: W4, the review page's scrolling Yes / No list.

`pnpm check` runs typecheck, lint (zero warnings), format check and the unit tests.
No `any`, no non-null assertion, no `@ts-ignore`/`@ts-expect-error`. Every
`eslint-disable` comment in `web/src` names one of the waivers below.

## Per-line waivers

| #   | Where                                                                                                         | Rule                                                                           | Why                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| --- | ------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| W1  | `src/lib/api/http.ts` `body()`                                                                                | `consistent-type-assertions`                                                   | The one place JSON becomes a typed value, used by every client (`people.ts`, `searches.ts`, `feedback.ts`). The servers are Python dataclasses on the same machine; `tests/test_share_web.py::test_client_columns_match_the_server` pins `types/people.ts` and `tests/test_search_json_contract.py` pins `types/searches.ts` to them. A runtime schema (zod) would be a third copy of the shapes, validating 28,000 rows per load, for one local client. |
| W2  | `src/lib/api/people.ts` `decodePeople()`                                                                      | `consistent-type-assertions`                                                   | The columnar payload is re-keyed by column name after the column list is checked; each cell takes its column's type.                                                                                                                                                                                                                                                                                                                                     |
| W3  | `src/pages/people/table/PersonRow.tsx` and `src/pages/searches/run/ResultRow.tsx`, the row `<div role="row">` | `jsx-a11y/click-events-have-key-events`, `jsx-a11y/interactive-supports-focus` | A row is not a tab stop by design: the table is the focus target and `pages/people/hooks/useKeyboard` / `pages/searches/hooks/useResultKeys` handle j/k/Enter for the focused row. A per-row key handler would be dead code.                                                                                                                                                                                                                             |
| W4  | `src/pages/review/worth/PileTable.tsx`, the `.decision-list` viewport                                         | `jsx-a11y/no-noninteractive-tabindex`                                          | The pile is a scrolling box with its own overflow, so it must be a tab stop for the keyboard to scroll it (arrow keys, Page Down, End). It has an `aria-label` and holds real controls; the old page's list carried `tabindex='0'` for the same reason.                                                                                                                                                                                                  |

| W5 | `src/pages/install/DesktopSetup.tsx`, the sign-in effect | `react-hooks/exhaustive-deps` | The sign-in opens once per URL setup hands over; the opener reads the provider from the same status, and re-running it on every status poll would reopen the modal while the user is typing in it. |

## Assertions that are not waivers

`src/lib/must.ts` throws on null/undefined instead of `!`. Used where the value
is guaranteed by construction (the virtualizer's index is below `items.length`;
the tab ink's active tab is a sibling) so a broken guarantee fails loudly.

## Config decisions (not per-line)

| Rule                                                 | Setting                                  | Why                                                                                                      |
| ---------------------------------------------------- | ---------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `@typescript-eslint/no-confusing-void-expression`    | `ignoreArrowShorthand: true`             | `() => setX(1)` is idiomatic React; braces add nothing.                                                  |
| `react-hooks/incompatible-library`                   | off                                      | No React Compiler in this build; the rule only warns that `useVirtualizer` would defeat its memoization. |
| `@typescript-eslint/no-unused-vars`                  | `argsIgnorePattern: "^_"`                | Underscore-prefixed unused arguments (mock signatures in tests).                                         |
| `@typescript-eslint/consistent-indexed-object-style` | off for `*.d.ts`                         | A module augmentation (`types/css.d.ts`) must be an interface; a `Record` alias cannot merge.            |
| `*.js` config files                                  | `disableTypeChecked`                     | ESLint/PostCSS configs are outside the TypeScript project.                                               |
| `@typescript-eslint/no-unsafe-assignment`            | off in `*.test.ts(x)` and `src/testing/` | Test doubles (`vi.fn`, stubbed globals) are `any`-typed by their libraries.                              |
