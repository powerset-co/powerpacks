import { useState } from "react"

import { CHIP, CLOSE_MARK, ReadRow, Row, Select, TraitChips } from "@/components/shared"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Textarea } from "@/components/ui/textarea"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { Plus } from "lucide-react"
import { AutocompleteInput, type EntityValue } from "./AutocompleteInput"
import type { PreparedSearch, SearchTrait } from "@/lib/agent/preparedSearch"
import { type ExpandedFilters, fromExpanded, toExpanded } from "@/lib/agent/expandedFilters"
import { ENTITY_TYPES, SECTOR_TYPES, SENIORITY_BANDS as BAND_ITEMS } from "@/lib/agent/companyFilters"
import {
  FIELDS,
  TEMPORALS,
  type Control,
  type Field,
  type Values,
  fieldHasValue,
  filledFields,
  names,
  scalar,
} from "@/lib/agent/searchFields"
import { send } from "@/lib/agent/store"
import { body } from "@/lib/api/http"
import { titleCase } from "@/lib/people/copy"
import { cn } from "@/lib/utils"
import type { FilterValue } from "@/types/searches"

import { EditFiltersModal } from "./EditFiltersModal"

/** Display names for the ids the modal shows as chips: bands, degrees, entity and sector types. */
const LABELS: Record<string, Record<string, string>> = {
  seniority_bands: Object.fromEntries(BAND_ITEMS.map((band) => [band.id, band.display_value])),
  degree_levels: { Bachelors: "Bachelor's", Masters: "Master's", MBA: "MBA", PhD: "PhD", MD: "MD", JD: "JD" },
  entity_types: ENTITY_TYPES,
  sector_types: SECTOR_TYPES,
}

interface SearchCardProps {
  search: PreparedSearch
  // Editing and Execute: the newest card's, while the agent waits for an answer.
  actions: boolean
}

// A chip being edited: its value is a text field, and the chip shows the field's focus.
const EDIT_CHIP = cn(CHIP, "inline-flex max-w-full items-center gap-1 pr-1.5 focus-within:border-primary")
const ADD_FIELD = cn(
  CHIP,
  "w-28 border-dashed bg-transparent text-foreground outline-none placeholder:text-faint focus:border-primary focus-visible:outline-none",
)
const FIELD =
  "rounded-[var(--radius-s)] border border-line bg-transparent px-2 py-1 text-[12px] text-foreground outline-none focus:border-primary focus-visible:outline-none"
// A select at the chip's size: the shared Select's trigger in the FIELD style.
const PICKER = cn(FIELD, "h-auto gap-1 border-line pr-1.5 text-muted-foreground")
const REMOVE =
  "inline-grid size-4 shrink-0 place-items-center rounded-full text-muted-foreground transition-colors duration-fast ease-out hover:bg-secondary hover:text-foreground"
const TEXT_BUTTON =
  "cursor-pointer border-0 bg-transparent p-0 text-xs text-muted-foreground transition-colors duration-fast ease-out hover:text-foreground"

/** A typed number or date as the payload holds it; nothing typed removes the key. */
function parsed(text: string, type: "number" | "date"): FilterValue | null {
  const trimmed = text.trim()
  if (trimmed === "") return null
  return type === "number" ? Number(trimmed) : trimmed
}

/** Typed text as the payload holds the key: a number stays a number while the text reads as one. */
function typedScalar(text: string, previous: FilterValue | null | undefined): FilterValue | null {
  if (text === "") return null
  return typeof previous === "number" && !Number.isNaN(Number(text)) ? Number(text) : text
}

function withNames(values: Values, key: string, list: string[]): Values {
  const next = new Map(values)
  next.set(key, list.length > 0 ? list : null)
  return next
}

function withValue(values: Values, key: string, value: FilterValue | null): Values {
  const next = new Map(values)
  next.set(key, value)
  return next
}

/** The prepared filters as a map, the card's starting point and what Reset restores. */
function preparedValues(search: PreparedSearch): Values {
  return new Map(search.filters.map((filter) => [filter.key, filter.value]))
}

/** The values as sent: a chip emptied while editing is gone, and a list with nothing left is null. */
function clean(values: Values): Values {
  const next: Values = new Map()
  for (const [key, value] of values) {
    const kept = Array.isArray(value) ? value.filter((name) => name.trim() !== "") : value
    next.set(key, Array.isArray(kept) && kept.length === 0 ? null : kept)
  }
  return next
}

/**
 * What Execute sends for edits: one JSON object the skill copies into the payload, holding only
 * what changed against the prepared search: `filters` the keys edited (a removed one as null),
 * `traits` the whole list when it changed, `query` when it changed. The agent treats every key sent
 * as an edit and traits sent as a new search. Nothing is joined or split, so a name with a comma
 * stays one name.
 */
function modifyMessage(search: PreparedSearch, query: string, values: Values, traits: SearchTrait[]): string {
  const body: { query?: string; filters: Record<string, FilterValue | null>; traits?: SearchTrait[] } = {
    filters: {},
  }
  if (query.trim() !== search.query) body.query = query.trim()
  if (JSON.stringify(traits) !== JSON.stringify(search.traits)) body.traits = traits
  // A changed query or trait value is a new search: the next prepare derives its own filters, so
  // the filters edited on this card do not go with it. A temporal change alone is an in-place edit
  // and travels with them.
  const values_ = (list: SearchTrait[]) => list.map((trait) => trait.value)
  const newSearch =
    body.query !== undefined || JSON.stringify(values_(traits)) !== JSON.stringify(values_(search.traits))
  if (newSearch) return `Modify, then execute:\n${JSON.stringify(body, null, 1)}`
  const prepared = preparedValues(search)
  const sent = clean(values)
  for (const key of new Set([...prepared.keys(), ...sent.keys()])) {
    const next = sent.get(key) ?? null
    if (JSON.stringify(next) !== JSON.stringify(prepared.get(key) ?? null)) body.filters[key] = next
  }
  return `Modify, then execute:\n${JSON.stringify(body, null, 1)}`
}

/**
 * A chip's value as a text field as wide as its text: a hidden copy of the text sizes the cell the
 * field fills, so the chip reads as it does when it is not being edited.
 */
function ChipField({
  label,
  value,
  onChange,
}: {
  label: string
  value: string
  onChange: (value: string) => void
}) {
  return (
    <span className="inline-grid min-w-0">
      <span aria-hidden="true" className="invisible col-start-1 row-start-1 overflow-hidden whitespace-pre">
        {value}
      </span>
      <input
        aria-label={label}
        value={value}
        size={1}
        onChange={(event) => onChange(event.target.value)}
        className="col-start-1 row-start-1 w-full min-w-[1ch] bg-transparent text-foreground outline-none focus-visible:outline-none"
      />
    </span>
  )
}

/** What sits in a row's add field before Enter: the text and, for locations, the kind picked. */
interface Draft {
  text: string
  kind?: string
}

const NO_DRAFT: Draft = { text: "" }

/** A list of names: chips, each editable and removable, and an add field or a picker. */
function Chips({
  label,
  items,
  options,
  readonly,
  onChange,
  kinds,
  draft,
  onDraft,
  lookup,
}: {
  label: string
  items: { name: string; kind?: string }[]
  options?: string[]
  readonly?: boolean
  onChange: (items: { name: string; kind?: string }[]) => void
  /** Kind labels and their keys, for a row that mixes several lists (locations). */
  kinds?: Record<string, string>
  draft: Draft
  onDraft: (draft: Draft) => void
  lookup?: "locations" | "roles"
}) {
  const typed = draft.text
  const kind = draft.kind ?? (kinds ? Object.keys(kinds)[0] : undefined)
  const kindLabel = (item: { kind?: string }) => (kinds && item.kind ? kinds[item.kind] : undefined)
  const add = (name: string) => {
    const trimmed = name.trim()
    if (!trimmed) return
    onChange([...items, { name: trimmed, kind }])
    onDraft({ text: "", kind })
  }
  const unused = options?.filter((option) => !items.some((item) => item.name === option)) ?? []
  return (
    <>
      {items.map((item, index) => (
        // Index keys: a name changes as it is typed.
        <span key={index} className={EDIT_CHIP}>
          {readonly || options ? (
            <span>{titleCase(item.name)}</span>
          ) : (
            <ChipField
              label={`${label} ${index + 1}`}
              value={item.name}
              onChange={(name) =>
                onChange(items.map((each, at) => (at === index ? { ...each, name } : each)))
              }
            />
          )}
          {kindLabel(item) && <span className="text-faint">{`· ${kindLabel(item)}`}</span>}
          <button
            type="button"
            aria-label={`Remove ${item.name}`}
            onClick={() => onChange(items.filter((_, at) => at !== index))}
            className={REMOVE}
          >
            {CLOSE_MARK}
          </button>
        </span>
      ))}
      {lookup ? (
        <Popover>
          <PopoverTrigger asChild>
            <button
              type="button"
              aria-label={`Add ${label}`}
              className={cn(CHIP, "p-1.5 hover:bg-secondary")}
            >
              <Plus className="size-3.5" />
            </button>
          </PopoverTrigger>
          <PopoverContent className="w-80" align="start">
            <AutocompleteInput
              label={`Search ${label}`}
              placeholder={
                lookup === "locations" ? "Search cities, metros, states, countries…" : "Search roles…"
              }
              allowCustom={false}
              value={[]}
              onSearch={async (query) => {
                const response = await fetch(`/api/autocomplete/${lookup}?q=${encodeURIComponent(query)}`)
                if (!response.ok) throw new Error(await response.text())
                return (await body<{ suggestions: EntityValue[] }>(response)).suggestions
              }}
              onChange={(selected) => {
                const item = selected[0]
                if (!item) return
                const locationKey = {
                  city: "cities",
                  state: "states",
                  country: "countries",
                  metro_area: "metro_areas",
                  macro_region: "macro_regions",
                }[item.type ?? ""]
                const nextKind =
                  kinds && locationKey
                    ? Object.hasOwn(kinds, locationKey)
                      ? locationKey
                      : `company_${locationKey}`
                    : undefined
                const name = lookup === "roles" ? item.id : item.display_value
                if (!items.some((existing) => existing.name === name && existing.kind === nextKind))
                  onChange([...items, { name, kind: nextKind }])
              }}
            />
          </PopoverContent>
        </Popover>
      ) : options ? (
        unused.length > 0 && (
          <Select
            aria-label={`Add ${label}`}
            value=""
            onChange={add}
            placeholder="+"
            className={cn(CHIP, "h-7 w-7 justify-center p-0 hover:bg-secondary [&>svg]:hidden")}
            options={unused.map((option) => ({ value: option, label: titleCase(option) }))}
          />
        )
      ) : readonly ? null : (
        <Popover>
          <PopoverTrigger asChild>
            <button
              type="button"
              aria-label={`Add ${label}`}
              className={cn(CHIP, "p-1.5 hover:bg-secondary")}
            >
              <Plus className="size-3.5" />
            </button>
          </PopoverTrigger>
          <PopoverContent className="w-64" align="start">
            <input
              aria-label={`Add ${label}`}
              placeholder={`Add ${label.toLowerCase()}…`}
              value={typed}
              onChange={(event) => onDraft({ text: event.target.value, kind })}
              onKeyDown={(event) => {
                if (event.key === "Enter") add(typed)
              }}
              onBlur={() => add(typed)}
              className={ADD_FIELD}
            />
          </PopoverContent>
        </Popover>
      )}
    </>
  )
}

/** One field's row, edited in place; read-only it is the shared ReadRow. */
function FieldRow({
  field,
  values,
  prepared,
  editable,
  onChange,
  draft,
  onDraft,
}: {
  field: Field
  values: Values
  /** The values as prepared: a number stays a number however the field is retyped. */
  prepared: Values
  editable: boolean
  onChange: (values: Values) => void
  draft: Draft
  onDraft: (draft: Draft) => void
}) {
  if (!editable) return <ReadRow field={field} values={values} />
  const control: Control = field.control
  switch (control.kind) {
    case "chips":
      return (
        <Row label={field.label}>
          <Chips
            label={field.label}
            lookup={control.key === "role_ids" ? "roles" : undefined}
            items={names(values.get(control.key)).map((name) => ({ name }))}
            options={control.options}
            readonly={control.readonly}
            draft={draft}
            onDraft={onDraft}
            onChange={(items) =>
              onChange(
                withNames(
                  values,
                  control.key,
                  items.map((item) => item.name),
                ),
              )
            }
          />
        </Row>
      )
    case "kinds": {
      const items = Object.keys(control.keys).flatMap((key) =>
        names(values.get(key)).map((name) => ({ name, kind: key })),
      )
      return (
        <Row label={field.label}>
          <Chips
            label={field.label}
            lookup="locations"
            items={items}
            kinds={control.keys}
            draft={draft}
            onDraft={onDraft}
            onChange={(next) => {
              let edited = values
              for (const key of Object.keys(control.keys))
                edited = withNames(
                  edited,
                  key,
                  next.filter((item) => item.kind === key).map((item) => item.name),
                )
              onChange(edited)
            }}
          />
        </Row>
      )
    }
    case "text": {
      const text = scalar(values.get(control.key))
      if (control.key === "semantic_query")
        return (
          <Row label={field.label}>
            <Dialog>
              <DialogTrigger asChild>
                <button
                  type="button"
                  aria-label="Edit role query"
                  className={cn(FIELD, "w-full truncate text-left hover:bg-secondary")}
                >
                  {text || "Describe the role…"}
                </button>
              </DialogTrigger>
              <DialogContent className="sm:max-w-2xl" aria-describedby={undefined}>
                <DialogHeader>
                  <DialogTitle>Role query</DialogTitle>
                </DialogHeader>
                <Textarea
                  aria-label="Role query"
                  value={text}
                  onChange={(event) => onChange(withValue(values, control.key, event.target.value || null))}
                  className="min-h-64 max-h-[60vh] resize-y"
                />
                <DialogClose asChild>
                  <Button className="justify-self-end">Done</Button>
                </DialogClose>
              </DialogContent>
            </Dialog>
          </Row>
        )
      return (
        <Row label={field.label}>
          <input
            aria-label={field.label}
            value={text}
            onChange={(event) =>
              onChange(
                withValue(values, control.key, typedScalar(event.target.value, prepared.get(control.key))),
              )
            }
            className={cn(FIELD, "w-full")}
          />
        </Row>
      )
    }
    case "pair": {
      // Money reads in millions; the payload holds dollars.
      const scale = control.scale ?? 1
      const shown = (value: FilterValue | null | undefined) =>
        typeof value === "number" ? String(value / scale) : scalar(value)
      const typed = (text: string) => {
        const value = parsed(text, control.type)
        return typeof value === "number" ? value * scale : value
      }
      const [low, high] = control.keys.map((key) => shown(values.get(key)))
      return (
        <Row label={field.label}>
          {control.keys.map((key, index) => (
            <span key={key} className="inline-flex items-center gap-1.5">
              {index === 1 && <span className="text-faint">–</span>}
              <input
                aria-label={`${field.label} ${index === 0 ? "min" : "max"}`}
                type={control.type}
                placeholder={index === 0 ? "min" : "max"}
                value={(index === 0 ? low : high) ?? ""}
                onChange={(event) => onChange(withValue(values, key, typed(event.target.value)))}
                className={cn(FIELD, control.type === "number" ? "w-20" : "w-36")}
              />
            </span>
          ))}
        </Row>
      )
    }
    case "select-pair": {
      const [low, high] = control.keys.map((key) => scalar(values.get(key)))
      return (
        <Row label={field.label}>
          {control.keys.map((key, index) => (
            <span key={key} className="inline-flex items-center gap-1.5">
              {index === 1 && <span className="text-faint">–</span>}
              <Select
                aria-label={`${field.label} ${index === 0 ? "min" : "max"}`}
                value={(index === 0 ? low : high) ?? ""}
                onChange={(next) => onChange(withValue(values, key, next || null))}
                className={PICKER}
                options={[
                  { value: "", label: "any" },
                  ...control.options.map((option) => ({ value: option, label: option })),
                ]}
              />
            </span>
          ))}
        </Row>
      )
    }
    case "toggle": {
      const on = values.get(control.key) === true
      return (
        <Row label={field.label}>
          <label className="inline-flex cursor-pointer items-center gap-1.5 text-[13px]">
            <input
              type="checkbox"
              aria-label={field.label}
              checked={on}
              onChange={(event) =>
                onChange(withValue(values, control.key, event.target.checked ? true : null))
              }
            />
            yes
          </label>
        </Row>
      )
    }
  }
}

/** The traits: value, temporal (click cycles all → current → past), remove, and an add field. */
function Traits({
  traits,
  editable,
  typed,
  onTyped,
  onChange,
}: {
  traits: SearchTrait[]
  editable: boolean
  typed: string
  onTyped: (value: string) => void
  onChange: (traits: SearchTrait[]) => void
}) {
  const add = () => {
    const value = typed.trim()
    if (!value) return
    onChange([...traits, { value, temporal: "current", meaning: "role" }])
    onTyped("")
  }
  return (
    <Row label="Traits">
      {editable ? (
        traits.map((trait, index) => {
          const cycle = () => {
            const next = TEMPORALS[(TEMPORALS.indexOf(trait.temporal) + 1) % TEMPORALS.length] ?? "all"
            onChange(traits.map((each, at) => (at === index ? { ...each, temporal: next } : each)))
          }
          return (
            // Index keys: a trait's value changes as it is typed. The whole chip cycles the temporal;
            // the field keeps its click for editing and the × its own.
            // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions -- the temporal button is the keyboard control
            <span
              key={index}
              className={cn(EDIT_CHIP, "cursor-pointer")}
              title="Click to change: all, current, past"
              onClick={(event) => {
                if (event.target instanceof HTMLElement && event.target.closest("input, button")) return
                cycle()
              }}
            >
              <ChipField
                label={`Trait ${index + 1}`}
                value={trait.value}
                onChange={(value) =>
                  onChange(traits.map((each, at) => (at === index ? { ...each, value } : each)))
                }
              />
              <button
                type="button"
                aria-label={`${trait.value}: ${trait.temporal}`}
                onClick={cycle}
                className="cursor-pointer border-0 bg-transparent p-0 text-faint hover:text-foreground"
              >
                ({trait.temporal})
              </button>
              <button
                type="button"
                aria-label={`Remove ${trait.value}`}
                onClick={() => onChange(traits.filter((_, at) => at !== index))}
                className={REMOVE}
              >
                {CLOSE_MARK}
              </button>
            </span>
          )
        })
      ) : (
        <TraitChips traits={traits} />
      )}
      {editable && (
        <Popover>
          <PopoverTrigger asChild>
            <button type="button" aria-label="Add trait" className={cn(CHIP, "p-1.5 hover:bg-secondary")}>
              <Plus className="size-3.5" />
            </button>
          </PopoverTrigger>
          <PopoverContent className="w-64" align="start">
            <input
              aria-label="Add trait"
              placeholder="Add trait"
              value={typed}
              onChange={(event) => onTyped(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") add()
              }}
              className={ADD_FIELD}
            />
          </PopoverContent>
        </Popover>
      )}
    </Row>
  )
}

/**
 * A prepared search in the chat: the query, the traits and the filters, each edited in place
 * while the card asks, with More filters opening every field the search could carry. Execute
 * sends `execute`, or the edits as one JSON object. A blocked card sends its changes without
 * running. A card that no longer asks shows the search as it was prepared.
 */
export function SearchCard({ search, actions }: SearchCardProps) {
  const [query, setQuery] = useState(search.query)
  const [values, setValues] = useState<Values>(() => preparedValues(search))
  const [traits, setTraits] = useState(search.traits)
  const [newTrait, setNewTrait] = useState("")
  // The Edit Filters modal works on a copy; Apply writes it back, Cancel drops it.
  const [filtersOpen, setFiltersOpen] = useState(false)
  const [modalFilters, setModalFilters] = useState<ExpandedFilters | null>(null)
  // What sits in each row's add field, by the row's label, so Execute keeps it without an Enter.
  const [drafts, setDrafts] = useState<Record<string, Draft>>({})
  // Once Execute (or Send changes) went out from this card, it keeps showing what it sent.
  const [submitted, setSubmitted] = useState(false)
  const prepared = preparedValues(search)
  const shown = actions || submitted
  const shownValues = shown ? values : prepared
  const shownTraits = shown ? traits : search.traits
  const shownQuery = shown ? query : search.query

  // The traits and the one typed into Add trait, so Execute keeps it without an Enter.
  function traitsWithTyped(): SearchTrait[] {
    const value = newTrait.trim()
    return value ? [...traits, { value, temporal: "current", meaning: "role" }] : traits
  }

  // The values with every row's typed-but-not-entered name added under its key.
  function valuesWithDrafts(): Values {
    let next = values
    const fields = [...FIELDS, ...filledFields(values, prepared)]
    for (const [label, draft] of Object.entries(drafts)) {
      const text = draft.text.trim()
      const control = fields.find((field) => field.label === label)?.control
      if (!text || !control) continue
      const key =
        control.kind === "kinds"
          ? (draft.kind ?? Object.keys(control.keys)[0])
          : control.kind === "chips" && !control.readonly && !control.options
            ? control.key
            : undefined
      if (key) next = withNames(next, key, [...names(next.get(key)), text])
    }
    return next
  }

  function submit(message: string) {
    setValues(clean(valuesWithDrafts()))
    setDrafts({})
    setTraits(traitsWithTyped())
    setNewTrait("")
    setSubmitted(true)
    void send(message)
  }

  const edited = modifyMessage(search, query, valuesWithDrafts(), traitsWithTyped())
  const dirty = edited !== modifyMessage(search, search.query, prepared, search.traits)

  function reset() {
    setQuery(search.query)
    setValues(prepared)
    setDrafts({})
    setTraits(search.traits)
    setNewTrait("")
  }

  // More filters offers every field except an id list with nothing in it (ids come from the
  // expand step, never typed) and the current-only toggles when traits carry that already.
  function offered(field: Field): boolean {
    const control = field.control
    if (control.kind === "chips" && control.readonly) return fieldHasValue(field, shownValues)
    if (
      control.kind === "toggle" &&
      (control.key === "is_current_role" || control.key === "is_current_company")
    )
      return search.traits.length === 0 && traits.length === 0
    return true
  }

  const rows = (fields: Field[]) =>
    fields.map((field) => (
      <FieldRow
        key={field.label}
        field={field}
        values={shownValues}
        prepared={prepared}
        editable={actions}
        onChange={setValues}
        draft={drafts[field.label] ?? NO_DRAFT}
        onDraft={(draft) => setDrafts({ ...drafts, [field.label]: draft })}
      />
    ))

  return (
    <section
      aria-label="Search"
      className="rise-in flex flex-col gap-3 rounded-[var(--radius-m)] border border-line bg-card p-4 shadow-[var(--shadow-1)]"
    >
      {actions ? (
        <input
          aria-label="Query"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          className="m-0 w-full border-0 border-b border-transparent bg-transparent p-0 text-[15px] font-semibold text-foreground outline-none focus:border-primary focus-visible:outline-none"
        />
      ) : (
        <h2 className="m-0 text-[15px] font-semibold">{shownQuery}</h2>
      )}
      {(shownTraits.length > 0 || actions) && (
        <Traits
          traits={shownTraits}
          editable={actions}
          typed={newTrait}
          onTyped={setNewTrait}
          onChange={setTraits}
        />
      )}
      {/* Read-only, a row emptied before Execute is gone; while editing it stays, so it can be refilled. */}
      {rows(
        filledFields(shownValues, prepared)
          .filter(offered)
          .filter((field) => actions || fieldHasValue(field, shownValues)),
      )}
      {search.notes.length > 0 && (
        <ul
          aria-label="Notes"
          className="m-0 flex list-none flex-col gap-1 p-0 text-xs text-muted-foreground"
        >
          {search.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
      {search.blockers.length > 0 && (
        <ul aria-label="Blockers" className="m-0 flex list-none flex-col gap-1 p-0 text-xs text-warn">
          {search.blockers.map((blocker) => (
            <li key={blocker}>{blocker}</li>
          ))}
        </ul>
      )}
      {actions && (
        <div className="flex flex-wrap items-center gap-3">
          {search.blockers.length === 0 ? (
            <Button variant="primary" onClick={() => submit(dirty ? edited : "execute")}>
              Execute
            </Button>
          ) : (
            <Button
              onClick={() => submit(edited.replace("Modify, then execute:", "Modify:"))}
              disabled={!dirty}
            >
              Send changes
            </Button>
          )}
          <button
            type="button"
            onClick={() => {
              setModalFilters(toExpanded(valuesWithDrafts(), LABELS))
              setFiltersOpen(true)
            }}
            className={TEXT_BUTTON}
          >
            More filters…
          </button>
          <EditFiltersModal
            open={filtersOpen}
            onOpenChange={setFiltersOpen}
            local={search.local}
            filters={modalFilters ?? toExpanded(values, LABELS)}
            // Functional: the modal fires several updates in one event (a clear, a location edit).
            onUpdate={(key, value) =>
              setModalFilters((previous) => ({ ...(previous ?? toExpanded(values, LABELS)), [key]: value }))
            }
            onApply={() => {
              if (modalFilters) setValues(fromExpanded(modalFilters, valuesWithDrafts()))
              setDrafts({})
            }}
          />
          {dirty && (
            <button type="button" onClick={reset} className={cn(TEXT_BUTTON, "ml-auto")}>
              Reset
            </button>
          )}
        </div>
      )}
    </section>
  )
}
