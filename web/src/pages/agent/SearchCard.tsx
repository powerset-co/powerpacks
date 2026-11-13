import { useState, type ReactNode } from "react"
import {
  Check,
  ChevronDown,
  Clock,
  History,
  Infinity as InfinityIcon,
  Info,
  Layers,
  Pencil,
  Plus,
  X,
} from "lucide-react"

import { Select } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { CheckboxScrollList } from "@/components/ui/checkbox-scroll-list"
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
import { AutocompleteInput, type EntityValue } from "./AutocompleteInput"
import type { PreparedSearch, SearchTrait } from "@/lib/agent/preparedSearch"
import { type ExpandedFilters, fromExpanded, toExpanded } from "@/lib/agent/expandedFilters"
import { ENTITY_TYPES, SECTOR_TYPES, SENIORITY_BANDS as BAND_ITEMS } from "@/lib/agent/companyFilters"
import {
  FIELDS,
  KEYWORDS,
  LEAD_KEYS,
  ROLE_QUERY,
  TEMPORALS,
  type Control,
  type Field,
  type Values,
  fieldHasValue,
  filledFields,
  names,
  pairText,
  range,
  previewShows,
  previewTitle,
  scalar,
} from "@/lib/agent/searchFields"
import { runCommand, send, useAgent } from "@/lib/agent/store"
import { useSearchScope } from "@/lib/agent/useSearchScope"
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

// The card follows the cloud app's search screen (network-search-app PowerSearchV2): the query
// and the set over a rule, Traits, then Search Filters with Role Query, Seniority and Role
// Keywords first and every other filter as a titled row of chips, the estimate, and Edit Filters
// beside Run Search.

/** The location keys as their own rows, titled as the cloud app titles them. */
const LOCATION_LABELS: Record<string, string> = {
  cities: "Cities",
  states: "States",
  metro_areas: "Metro Areas",
  macro_regions: "Regions",
  countries: "Countries",
  company_cities: "Company Cities",
  company_states: "Company States",
  company_metro_areas: "Company Metro Areas",
  company_macro_regions: "Company Regions",
  company_countries: "Company Countries",
}

/** The cloud app's order for the location rows. */
const LOCATION_ORDER = Object.keys(LOCATION_LABELS)

/** A list row shows its first ten names, then "+ N hidden", as the cloud app's does. */
const SHOWN_NAMES = 10

interface SearchCardProps {
  search: PreparedSearch
  // Editing and Run Search: the newest card's, while the agent waits for an answer.
  actions: boolean
  // A refined run's search: "Showing now", or a button going back to it.
  step?: ReactNode
}

// The cloud app's badge: a filled pill, its remove × after the name.
const BADGE =
  "inline-flex max-w-full items-center gap-1 rounded-full border border-transparent bg-secondary px-2.5 py-0.5 text-xs font-semibold leading-5 text-foreground"
// Its outline twin for "+ Add" and "Edit".
const BADGE_ACTION =
  "inline-flex cursor-pointer items-center gap-1 rounded-full border border-line bg-transparent px-2.5 py-0.5 text-xs font-semibold leading-5 text-foreground transition-colors duration-fast ease-out hover:bg-secondary"
const ADD_FIELD =
  "w-full rounded-[var(--radius-s)] border border-line bg-transparent px-2 py-1.5 text-[13px] text-foreground outline-none placeholder:text-faint focus:border-primary focus-visible:outline-none"
const FIELD =
  "rounded-[var(--radius-s)] border border-line bg-transparent px-2 py-1 text-[12px] text-foreground outline-none focus:border-primary focus-visible:outline-none"
// A select at the chip's size: the shared Select's trigger in the FIELD style.
const PICKER = cn(FIELD, "h-auto gap-1 border-line pr-1.5 text-muted-foreground")
const REMOVE =
  "inline-grid size-4 shrink-0 cursor-pointer place-items-center rounded-sm border-0 bg-transparent p-0 text-muted-foreground transition-colors duration-fast ease-out hover:text-bad"
const TEXT_BUTTON =
  "cursor-pointer border-0 bg-transparent p-0 text-xs text-muted-foreground transition-colors duration-fast ease-out hover:text-foreground"

/** A count as the cloud app shows an estimate: 641 → 600, 1531 → 1500, 12345 → 12000. */
function approxCount(count: number): number {
  if (count >= 10000) return Math.round(count / 1000) * 1000
  if (count >= 1000) return Math.round(count / 500) * 500
  if (count >= 100) return Math.round(count / 100) * 100
  if (count >= 10) return Math.round(count / 10) * 10
  return count
}

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
interface CardEdits {
  query?: string
  filters: Record<string, FilterValue | null>
  traits?: SearchTrait[]
}

function modifyEdits(
  search: PreparedSearch,
  query: string,
  values: Values,
  traits: SearchTrait[],
): CardEdits {
  const body: CardEdits = {
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
  if (newSearch) return body
  const prepared = preparedValues(search)
  const sent = clean(values)
  for (const key of new Set([...prepared.keys(), ...sent.keys()])) {
    const next = sent.get(key) ?? null
    if (JSON.stringify(next) !== JSON.stringify(prepared.get(key) ?? null)) body.filters[key] = next
  }
  return body
}

/** A changed query or trait value: a new prepare, which only the agent can restate. */
function isNewSearch(search: PreparedSearch, edits: CardEdits): boolean {
  const values = (list: SearchTrait[]) => JSON.stringify(list.map((trait) => trait.value))
  return (
    edits.query !== undefined ||
    (edits.traits !== undefined && values(edits.traits) !== values(search.traits))
  )
}

/** The prepare's own run command, with the saved edits when there are any; null when it printed none. */
function runLine(search: PreparedSearch, edited: boolean): string | null {
  if (search.executeCommand === null || search.runDir === null || search.runDir.includes("'")) return null
  return edited
    ? `${search.executeCommand} --edits-json '${search.runDir}/card-edits.json'`
    : search.executeCommand
}

/** The edits as the message carries them when the card cannot save them to the search's folder. */
function modifyMessage(edits: CardEdits, run: boolean): string {
  return `${run ? "Modify, then execute" : "Modify"}:\n${JSON.stringify(edits, null, 1)}`
}

/** Saves the edits as `<run>/card-edits.json`, so the chat says only that they were made. */
async function saveEdits(run: string, edits: CardEdits): Promise<boolean> {
  try {
    const response = await fetch("/searches/api/card-edits", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run, edits }),
    })
    return response.ok
  } catch {
    return false
  }
}

/**
 * A chip's value as a text field as wide as its text: a hidden copy of the text sizes the cell the
 * field fills, so the chip reads as it does when it is not being edited.
 */
function ChipField({
  label,
  value,
  onChange,
  className,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  className?: string
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
        className={cn(
          "col-start-1 row-start-1 w-full min-w-[1ch] bg-transparent text-foreground outline-none focus-visible:outline-none",
          className,
        )}
      />
    </span>
  )
}

/** What sits in a row's add field before Enter. */
interface Draft {
  text: string
  kind?: string
}

const NO_DRAFT: Draft = { text: "" }

/** A row of the filters: its title over its chips, as the cloud app lays them out. */
function FilterRow({ label, colon, children }: { label: string; colon?: boolean; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <p className="m-0 text-sm font-medium text-foreground">
        {label}
        {colon ? ":" : null}
      </p>
      <div className="flex min-w-0 flex-wrap items-center gap-x-1 gap-y-2">{children}</div>
    </div>
  )
}

/** A filled pill with its × after the name. */
function Badge({
  children,
  label,
  onRemove,
  className,
}: {
  children: ReactNode
  label: string
  onRemove?: () => void
  className?: string
}) {
  return (
    <span className={cn(BADGE, className)}>
      <span className="min-w-0 break-words">{children}</span>
      {onRemove && (
        <button type="button" aria-label={`Remove ${label}`} onClick={onRemove} className={REMOVE}>
          <X className="size-3" />
        </button>
      )}
    </span>
  )
}

/** "+ Add" opening a search over the server's autocomplete: locations or roles. */
function LookupAdd({
  label,
  lookup,
  onPick,
}: {
  label: string
  lookup: "locations" | "roles"
  onPick: (item: EntityValue) => void
}) {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button type="button" aria-label={`Add ${label}`} className={BADGE_ACTION}>
          <Plus className="size-3" />
          Add
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-80" align="start">
        <AutocompleteInput
          label={`Search ${label}`}
          placeholder={lookup === "locations" ? "Search cities, metros, states, countries…" : "Search roles…"}
          allowCustom={false}
          value={[]}
          onSearch={async (query) => {
            const response = await fetch(`/api/autocomplete/${lookup}?q=${encodeURIComponent(query)}`)
            if (!response.ok) throw new Error(await response.text())
            return (await body<{ suggestions: EntityValue[] }>(response)).suggestions
          }}
          onChange={(selected) => {
            const item = selected[0]
            if (item) onPick(item)
          }}
        />
      </PopoverContent>
    </Popover>
  )
}

/** "+ Add" opening a text field; Enter or leaving the field adds what was typed. */
function TextAdd({
  label,
  draft,
  onDraft,
  onAdd,
}: {
  label: string
  draft: Draft
  onDraft: (draft: Draft) => void
  onAdd: (name: string) => void
}) {
  const add = () => {
    const trimmed = draft.text.trim()
    if (!trimmed) return
    onAdd(trimmed)
    onDraft({ text: "" })
  }
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button type="button" aria-label={`Add ${label}`} className={BADGE_ACTION}>
          <Plus className="size-3" />
          Add
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-64" align="start">
        <input
          aria-label={`Add ${label}`}
          placeholder={`Add ${label.toLowerCase()}…`}
          value={draft.text}
          onChange={(event) => onDraft({ text: event.target.value })}
          onKeyDown={(event) => {
            if (event.key === "Enter") add()
          }}
          onBlur={add}
          className={ADD_FIELD}
        />
      </PopoverContent>
    </Popover>
  )
}

/** "⌄ Edit" opening a checkbox list over a fixed vocabulary (seniority bands, degrees). */
function OptionsEdit({
  label,
  options,
  optionLabel,
  selected,
  onChange,
}: {
  label: string
  options: string[]
  optionLabel: (option: string) => string
  selected: string[]
  onChange: (selected: string[]) => void
}) {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button type="button" aria-label={`Edit ${label}`} className={BADGE_ACTION}>
          <ChevronDown className="size-3" />
          Edit
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-48 p-0" align="start">
        <CheckboxScrollList
          items={options.map((option) => ({ id: option, display_value: optionLabel(option), type: label }))}
          selectedIds={new Set(selected)}
          // Kept in the vocabulary's order, so a band added lands where it ranks.
          onAdd={(id) => onChange(options.filter((option) => option === id || selected.includes(option)))}
          onRemove={(id) => onChange(selected.filter((name) => name !== id))}
        />
      </PopoverContent>
    </Popover>
  )
}

/** A list of names: pills, each removable (and editable when typed), then Add or Edit. */
function Chips({
  field,
  control,
  values,
  editable,
  onChange,
  draft,
  onDraft,
}: {
  field: Field
  control: Extract<Control, { kind: "chips" }>
  values: Values
  editable: boolean
  onChange: (values: Values) => void
  draft: Draft
  onDraft: (draft: Draft) => void
}) {
  const items = names(values.get(control.key))
  // Role Keywords show every name, as the cloud app's do; other rows fold past ten.
  const [folded, fold] = useFoldedNames(items)
  const shown = control.key === KEYWORDS ? items : folded
  const set = (next: string[]) => onChange(withNames(values, control.key, next))
  // Typed names edit in place; the role keywords read in title case, as the cloud app shows them.
  const typed = editable && !control.readonly && !control.options && control.key !== KEYWORDS
  return (
    <>
      {shown.map((name, index) =>
        typed ? (
          // Index keys: a name changes as it is typed.
          <span key={index} className={cn(BADGE, "pr-1.5 focus-within:border-primary")}>
            <ChipField
              label={`${field.label} ${index + 1}`}
              value={name}
              onChange={(text) => set(items.map((each, at) => (at === index ? text : each)))}
            />
            <button
              type="button"
              aria-label={`Remove ${name}`}
              onClick={() => set(items.filter((_, at) => at !== index))}
              className={REMOVE}
            >
              <X className="size-3" />
            </button>
          </span>
        ) : (
          <Badge
            key={index}
            label={name}
            onRemove={editable ? () => set(items.filter((_, at) => at !== index)) : undefined}
          >
            {titleCase(name)}
          </Badge>
        ),
      )}
      {control.key === KEYWORDS ? null : fold}
      {editable &&
        (control.options ? (
          <OptionsEdit
            label={field.label}
            options={control.options}
            optionLabel={(option) => LABELS[control.key]?.[option] ?? titleCase(option)}
            selected={items}
            onChange={set}
          />
        ) : control.readonly ? null : (
          <TextAdd
            label={field.label}
            draft={draft}
            onDraft={onDraft}
            onAdd={(name) => set([...items, name])}
          />
        ))}
    </>
  )
}

/** "+ N hidden" past the first ten names of a row, and "Hide" once they show. */
function useFoldedNames<T>(items: T[]): [T[], ReactNode] {
  const [all, setAll] = useState(false)
  if (items.length <= SHOWN_NAMES) return [items, null]
  return [
    all ? items : items.slice(0, SHOWN_NAMES),
    <button
      key="fold"
      type="button"
      onClick={() => setAll(!all)}
      className={cn(BADGE_ACTION, "text-muted-foreground")}
    >
      {all ? "Hide" : `+ ${items.length - SHOWN_NAMES} hidden`}
    </button>,
  ]
}

/** One field's row (or, for locations, one row per kind), edited in place while the card asks. */
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
  const control: Control = field.control
  const lead = "key" in control && LEAD_KEYS.includes(control.key)
  const label = previewTitle(field)
  switch (control.kind) {
    case "chips":
      return (
        <FilterRow label={label} colon={!lead}>
          <Chips
            field={field}
            control={control}
            values={values}
            editable={editable}
            onChange={onChange}
            draft={draft}
            onDraft={onDraft}
          />
        </FilterRow>
      )
    case "kinds": {
      // Each kind is its own row (Cities, Metro Areas); one Add after the last finds any kind.
      const groups = Object.keys(control.keys)
        .sort((a, b) => LOCATION_ORDER.indexOf(a) - LOCATION_ORDER.indexOf(b))
        .map((key) => ({ key, items: names(values.get(key)) }))
        .filter((group) => group.items.length > 0)
      const add = editable ? (
        <LookupAdd
          label={field.label}
          lookup="locations"
          onPick={(item) => {
            const kind = {
              city: "cities",
              state: "states",
              country: "countries",
              metro_area: "metro_areas",
              macro_region: "macro_regions",
            }[item.type ?? ""]
            if (!kind) return
            const key = Object.hasOwn(control.keys, kind) ? kind : `company_${kind}`
            if (!Object.hasOwn(control.keys, key)) return
            const list = names(values.get(key))
            if (!list.includes(item.display_value))
              onChange(withNames(values, key, [...list, item.display_value]))
          }}
        />
      ) : null
      if (groups.length === 0)
        return add ? (
          <FilterRow label={label} colon>
            {add}
          </FilterRow>
        ) : null
      return (
        <>
          {groups.map(({ key, items }, at) => (
            <FilterRow key={key} label={LOCATION_LABELS[key] ?? key} colon>
              {items.map((name, index) => (
                <Badge
                  key={`${name}:${index}`}
                  label={name}
                  onRemove={
                    editable
                      ? () =>
                          onChange(
                            withNames(
                              values,
                              key,
                              items.filter((_, other) => other !== index),
                            ),
                          )
                      : undefined
                  }
                >
                  {name}
                </Badge>
              ))}
              {at === groups.length - 1 && add}
            </FilterRow>
          ))}
        </>
      )
    }
    case "text": {
      const text = scalar(values.get(control.key))
      if (control.key === ROLE_QUERY) {
        const paragraph = <span className="text-sm text-muted-foreground">{text}</span>
        return (
          <div className="flex min-w-0 flex-col gap-1">
            <p className="m-0 text-sm font-medium text-foreground">Role Query</p>
            {editable ? (
              <Dialog>
                <DialogTrigger asChild>
                  <button
                    type="button"
                    aria-label="Edit role query"
                    className="group -mx-1.5 cursor-pointer rounded-[var(--radius-s)] border-0 bg-transparent px-1.5 py-1 text-left break-words transition-colors duration-fast ease-out hover:bg-secondary"
                  >
                    {text ? paragraph : <span className="text-sm text-faint">Describe the role…</span>}
                    <Pencil className="ml-1.5 inline size-3 text-faint opacity-0 transition-opacity duration-fast group-hover:opacity-100" />
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
            ) : (
              <p className="m-0 break-words">{paragraph}</p>
            )}
          </div>
        )
      }
      return (
        <FilterRow label={label} colon>
          {editable ? (
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
          ) : (
            <Badge label={text}>{text}</Badge>
          )}
        </FilterRow>
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
      const [low = "", high = ""] = control.keys.map((key) => shown(values.get(key)))
      return (
        <FilterRow label={label} colon>
          {editable ? (
            control.keys.map((key, index) => (
              <span key={key} className="inline-flex items-center gap-1.5">
                {index === 1 && <span className="text-faint">–</span>}
                <input
                  aria-label={`${field.label} ${index === 0 ? "min" : "max"}`}
                  type={control.type}
                  placeholder={index === 0 ? "min" : "max"}
                  value={index === 0 ? low : high}
                  onChange={(event) => onChange(withValue(values, key, typed(event.target.value)))}
                  className={cn(FIELD, control.type === "number" ? "w-20" : "w-36")}
                />
              </span>
            ))
          ) : (
            <Badge label={field.label}>{pairText(control.keys[0], low, high)}</Badge>
          )}
        </FilterRow>
      )
    }
    case "select-pair": {
      const [low = "", high = ""] = control.keys.map((key) => scalar(values.get(key)))
      return (
        <FilterRow label={label} colon>
          {editable ? (
            control.keys.map((key, index) => (
              <span key={key} className="inline-flex items-center gap-1.5">
                {index === 1 && <span className="text-faint">–</span>}
                <Select
                  aria-label={`${field.label} ${index === 0 ? "min" : "max"}`}
                  value={index === 0 ? low : high}
                  onChange={(next) => onChange(withValue(values, key, next || null))}
                  className={PICKER}
                  options={[
                    { value: "", label: "any" },
                    ...control.options.map((option) => ({ value: option, label: option })),
                  ]}
                />
              </span>
            ))
          ) : (
            <Badge label={field.label}>{range(low, high)}</Badge>
          )}
        </FilterRow>
      )
    }
    case "toggle": {
      const on = values.get(control.key) === true
      return (
        <FilterRow label={label} colon>
          {editable ? (
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
          ) : (
            <Badge label={field.label}>{on ? "Yes" : "No"}</Badge>
          )}
        </FilterRow>
      )
    }
  }
}

/** The cloud app's trait temporals: current in blue, past in amber, all neutral. */
const TEMPORAL: Record<string, { icon: typeof Clock; label: string; chip: string; dot: string }> = {
  current: {
    icon: Clock,
    label: "Current",
    chip: "border-[color-mix(in_srgb,var(--info)_45%,transparent)] bg-info-soft text-info",
    dot: "bg-info",
  },
  past: {
    icon: History,
    label: "Past",
    chip: "border-[color-mix(in_srgb,var(--warn)_45%,transparent)] bg-warn-soft text-warn",
    dot: "bg-warn",
  },
  all: {
    icon: InfinityIcon,
    label: "All",
    chip: "border-line bg-secondary text-foreground",
    dot: "bg-faint",
  },
}

/** One trait: dot and icon, the value, its temporal; clicking cycles all → current → past.
 *  Pencil edits the value in place, × removes it. */
function TraitChip({
  trait,
  index,
  editable,
  onChange,
  onRemove,
}: {
  trait: SearchTrait
  index: number
  editable: boolean
  onChange: (trait: SearchTrait) => void
  onRemove: () => void
}) {
  const [editing, setEditing] = useState(false)
  const temporal = TEMPORAL[trait.temporal] ?? TEMPORAL.all
  if (!temporal) return null
  const Icon = temporal.icon
  const chip = cn(
    "inline-flex max-w-full items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs leading-5",
    temporal.chip,
  )
  const cycle = () =>
    onChange({
      ...trait,
      temporal: TEMPORALS[(TEMPORALS.indexOf(trait.temporal) + 1) % TEMPORALS.length] ?? "all",
    })
  if (editing)
    return (
      <span className={cn(chip, "pr-1.5")}>
        <ChipField
          label={`Trait ${index + 1}`}
          value={trait.value}
          onChange={(value) => onChange({ ...trait, value })}
        />
        <button
          type="button"
          aria-label="Done editing"
          onClick={() => setEditing(false)}
          className={cn(REMOVE, "hover:text-foreground")}
        >
          <Check className="size-3" />
        </button>
      </span>
    )
  return (
    <span className={cn(chip, "group")}>
      {editable ? (
        <button
          type="button"
          aria-label={`${trait.value}: ${trait.temporal}`}
          title="Click to change: all, current, past"
          onClick={cycle}
          className="inline-flex min-w-0 cursor-pointer items-center gap-1.5 border-0 bg-transparent p-0 text-inherit"
        >
          <span className={cn("size-1.5 shrink-0 rounded-full", temporal.dot)} />
          <Icon className="size-3 shrink-0 opacity-70" />
          <span className="truncate">{trait.value}</span>
          <span className="shrink-0 text-[10px] font-medium uppercase tracking-wider opacity-60">
            {temporal.label}
          </span>
        </button>
      ) : (
        <>
          <span className={cn("size-1.5 shrink-0 rounded-full", temporal.dot)} />
          <Icon className="size-3 shrink-0 opacity-70" />
          <span className="truncate">{trait.value}</span>
          <span className="shrink-0 text-[10px] font-medium uppercase tracking-wider opacity-60">
            {temporal.label}
          </span>
        </>
      )}
      {editable && (
        // Pencil and × show on hover (or keyboard focus), so the chip reads as the cloud app's.
        <span className="inline-flex max-w-0 items-center gap-0.5 overflow-hidden opacity-0 transition-all duration-med group-focus-within:max-w-[40px] group-focus-within:opacity-100 group-hover:max-w-[40px] group-hover:opacity-100">
          <button
            type="button"
            aria-label={`Edit ${trait.value}`}
            onClick={() => setEditing(true)}
            className={cn(REMOVE, "hover:text-foreground")}
          >
            <Pencil className="size-3" />
          </button>
          <button type="button" aria-label={`Remove ${trait.value}`} onClick={onRemove} className={REMOVE}>
            <X className="size-3" />
          </button>
        </span>
      )}
    </span>
  )
}

/** The traits, and while editing a dashed "+ Add" that opens a field in its place. */
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
  const [adding, setAdding] = useState(false)
  const add = () => {
    const value = typed.trim()
    if (value) onChange([...traits, { value, temporal: "current", meaning: "role" }])
    onTyped("")
    setAdding(false)
  }
  return (
    <section aria-label="Traits">
      <h3 className="m-0 mb-3 text-xl font-semibold text-foreground">Traits</h3>
      <div className="flex flex-wrap items-center gap-1.5">
        {traits.map((trait, index) => (
          <TraitChip
            // Index keys: a trait's value changes as it is typed.
            key={index}
            trait={trait}
            index={index}
            editable={editable}
            onChange={(next) => onChange(traits.map((each, at) => (at === index ? next : each)))}
            onRemove={() => onChange(traits.filter((_, at) => at !== index))}
          />
        ))}
        {editable &&
          (adding ? (
            <span className="inline-flex items-center rounded-full border border-dashed border-line-strong px-2.5 py-1 text-xs leading-5">
              <input
                ref={(field) => field?.focus()}
                aria-label="Add trait"
                placeholder="Add trait…"
                value={typed}
                onChange={(event) => onTyped(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") add()
                  if (event.key === "Escape") {
                    onTyped("")
                    setAdding(false)
                  }
                }}
                onBlur={add}
                className="w-32 bg-transparent text-foreground outline-none placeholder:text-faint focus-visible:outline-none"
              />
            </span>
          ) : (
            <button
              type="button"
              aria-label="Add trait"
              onClick={() => setAdding(true)}
              className="inline-flex cursor-pointer items-center gap-1 rounded-full border border-dashed border-line-strong bg-transparent px-2.5 py-1 text-xs leading-5 text-muted-foreground transition-colors duration-fast ease-out hover:bg-secondary hover:text-foreground"
            >
              <Plus className="size-3" />
              Add
            </button>
          ))}
      </div>
    </section>
  )
}

/** Where this chat searches, as the cloud app's set picker shows it; the chat's pick is pinned. */
function ScopePill() {
  const agent = useAgent()
  const scope = useSearchScope()
  const current = agent.scope
  if (!current) return null
  let name = "Local index"
  let count: number | null = null
  if ("set_id" in current) {
    const set = scope.data?.sets.find(({ set_id }) => set_id === current.set_id)
    if (!set) return null
    name = set.is_personal && set.owned ? "Personal network" : set.name
    count = set.person_count
  }
  return (
    <span
      title="This chat searches here; start a new chat to search elsewhere"
      className="inline-flex h-8 min-w-0 max-w-[260px] shrink items-center gap-2 rounded-[var(--radius-s)] border border-line px-3 text-[13px] font-medium text-foreground"
    >
      <Layers className="size-4 shrink-0 text-muted-foreground" />
      <span className="truncate">{name}</span>
      {count !== null && (
        <span className="shrink-0 font-normal text-muted-foreground">({count.toLocaleString()})</span>
      )}
    </span>
  )
}

/** True when the value holds something: a list with names, any scalar. */
function filled(value: FilterValue | null | undefined): boolean {
  return value !== null && value !== undefined && !(Array.isArray(value) && value.length === 0)
}

/** The FIELDS entry that edits `key`. */
function fieldOf(key: string): Field | undefined {
  return FIELDS.find((field) => "key" in field.control && field.control.key === key)
}

/**
 * A prepared search in the chat, laid out as the cloud app's search screen: the query and the set,
 * the traits, the filters (each edited in place while the card asks), the estimate, and Edit
 * Filters beside Run Search. Run Search sends `execute`, or the edits as one JSON object; a blocked
 * card sends its changes without running. A card that no longer asks shows the search as it was
 * prepared, folded under its query until opened.
 */
export function SearchCard({ search, actions, step }: SearchCardProps) {
  const [query, setQuery] = useState(search.query)
  const [values, setValues] = useState<Values>(() => preparedValues(search))
  const [traits, setTraits] = useState(search.traits)
  const [newTrait, setNewTrait] = useState("")
  // The Edit Filters modal works on a copy; Apply writes it back, Cancel drops it.
  const [filtersOpen, setFiltersOpen] = useState(false)
  const [modalFilters, setModalFilters] = useState<ExpandedFilters | null>(null)
  // What sits in each row's add field, by the row's label, so Run Search keeps it without an Enter.
  const [drafts, setDrafts] = useState<Record<string, Draft>>({})
  // Once Run Search (or Send changes) went out from this card, it keeps showing what it sent.
  const [submitted, setSubmitted] = useState(false)
  // An earlier card folds under its query, as the cloud app folds the filters once results show.
  const [open, setOpen] = useState(actions)
  const prepared = preparedValues(search)
  const shown = actions || submitted
  const shownValues = shown ? values : prepared
  const shownTraits = shown ? traits : search.traits
  const shownQuery = shown ? query : search.query
  const unfolded = actions || open

  // The traits and the one typed into Add trait, so Run Search keeps it without an Enter.
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

  /**
   * Run Search starts the prepared run on the chat straight away, with the edits saved beside it;
   * a new query or trait, a blocked card's changes, or a failed save go to the agent instead.
   */
  function submit(run: boolean) {
    setValues(clean(valuesWithDrafts()))
    setDrafts({})
    setTraits(traitsWithTyped())
    setNewTrait("")
    setSubmitted(true)
    const direct = run && !(dirty && isNewSearch(search, edits)) ? runLine(search, dirty) : null
    // A run the app refuses (a command it does not allow) goes to the agent as before.
    const runOr = (line: string, fallback: string) =>
      runCommand(line).then((started) => (started ? undefined : send(fallback)))
    if (!dirty) {
      void (direct === null ? send("execute") : runOr(direct, "execute"))
      return
    }
    const message = modifyMessage(edits, run)
    if (search.run === null) {
      void send(message)
      return
    }
    void saveEdits(search.run, edits).then((ok) => {
      if (!ok) return send(message)
      const short = run ? "Run the search with my edits" : "Update the search with my edits"
      return direct === null ? send(short) : runOr(direct, short)
    })
  }

  const edits = modifyEdits(search, query, valuesWithDrafts(), traitsWithTyped())
  const dirty =
    JSON.stringify(edits) !== JSON.stringify(modifyEdits(search, search.query, prepared, search.traits))

  function reset() {
    setQuery(search.query)
    setValues(prepared)
    setDrafts({})
    setTraits(search.traits)
    setNewTrait("")
  }

  // Every field but an id list with nothing in it (ids come from the expand step, never typed).
  function offered(field: Field): boolean {
    const control = field.control
    if (control.kind === "chips" && control.readonly) return fieldHasValue(field, shownValues)
    return true
  }

  // What the run needs before Run Search: a search prepared with role keywords keeps at least one
  // (without them the run matches no titles: it ranks on the role query alone, or with none screens
  // everyone the other filters let through), and a search prepared with filters keeps at least one
  // (with none the run fails). A changed query or trait is prepared again, so it needs neither.
  const sending = clean(valuesWithDrafts())
  const anyFilter = (map: Values) => [...map.values()].some(filled)
  const kept = actions && !(dirty && isNewSearch(search, edits))
  const needsKeyword = kept && filled(prepared.get(KEYWORDS)) && !filled(sending.get(KEYWORDS))
  const needsFilter = kept && anyFilter(prepared) && !anyFilter(sending)

  const row = (field: Field) => {
    const fieldRow = (
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
    )
    if (!(needsKeyword && "key" in field.control && field.control.key === KEYWORDS)) return fieldRow
    return (
      <div key={field.label} className="flex min-w-0 flex-col gap-2">
        {fieldRow}
        <p role="alert" className="m-0 text-xs text-warn">
          Add a role keyword to run this search.
        </p>
      </div>
    )
  }

  // Role Query, Seniority and Role Keywords lead, as the cloud app has them; Seniority and Role
  // Keywords stay while editing so they can be filled. The rest follow in the table's order. Read-
  // only, a row emptied before Run Search is gone; while editing it stays, so it can be refilled.
  const lead = LEAD_KEYS.map(fieldOf)
    .filter((field): field is Field => field !== undefined)
    .filter(
      (field) =>
        fieldHasValue(field, shownValues) ||
        (actions &&
          (fieldHasValue(field, prepared) || ("key" in field.control && field.control.key !== ROLE_QUERY))),
    )
  const rest = filledFields(shownValues, prepared)
    .filter((field) => !("key" in field.control && LEAD_KEYS.includes(field.control.key)))
    .filter(previewShows)
    .filter(offered)
    .filter((field) => actions || fieldHasValue(field, shownValues))
  const applied = [...shownValues.values()].filter(filled).length
  const estimate = search.estimate

  return (
    <section
      aria-label="Search"
      className="rise-in flex min-w-0 flex-col rounded-[var(--radius-m)] border border-line bg-card p-5 shadow-[var(--shadow-1)]"
    >
      {/* Wraps when the chat is narrow: the set, the step and the chevron drop under the query. */}
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <div className="flex min-w-[min(100%,14rem)] flex-1 items-center gap-2">
          <span className="shrink-0 text-sm font-medium text-foreground">Query:</span>
          {actions ? (
            <span className="flex min-w-0 items-baseline text-sm italic text-muted-foreground">
              “
              <ChipField
                label="Query"
                value={query}
                onChange={setQuery}
                className="border-b border-transparent italic text-muted-foreground focus:border-primary focus:text-foreground"
              />
              ”
            </span>
          ) : (
            <h2 className="m-0 min-w-0 truncate text-sm font-normal italic text-muted-foreground">
              “{shownQuery}”
            </h2>
          )}
          {!unfolded && applied > 0 && (
            <span className={cn(BADGE, "shrink-0 whitespace-nowrap font-medium")}>+{applied} filters</span>
          )}
        </div>
        <div className="ml-auto flex min-w-0 max-w-full flex-wrap items-center justify-end gap-x-3 gap-y-2">
          <ScopePill />
          {(!actions || Boolean(step)) && (
            <span className="flex shrink-0 items-center gap-3">
              {step}
              {!actions && (
                <button
                  type="button"
                  aria-label={open ? "Hide filters" : "Show filters"}
                  aria-expanded={open}
                  onClick={() => setOpen(!open)}
                  className="grid size-7 shrink-0 cursor-pointer place-items-center rounded-[var(--radius-s)] border-0 bg-transparent p-0 text-muted-foreground transition-colors duration-fast ease-out hover:bg-secondary hover:text-foreground"
                >
                  <ChevronDown
                    className={cn("size-5 transition-transform duration-med", open && "rotate-180")}
                  />
                </button>
              )}
            </span>
          )}
        </div>
      </div>

      {unfolded && (
        <div className="mt-5 flex min-w-0 flex-col gap-6 border-t border-line pt-6">
          {(shownTraits.length > 0 || actions) && (
            <Traits
              traits={shownTraits}
              editable={actions}
              typed={newTrait}
              onTyped={setNewTrait}
              onChange={setTraits}
            />
          )}

          <section aria-label="Search filters" className="flex min-w-0 flex-col">
            <h3 className="m-0 mb-2 text-xl font-semibold text-foreground">Search Filters</h3>
            <p className="m-0 mb-4 flex items-center gap-3 text-sm text-muted-foreground">
              {applied} {applied === 1 ? "filter" : "filters"} applied
              {actions && dirty && (
                <button type="button" onClick={reset} className={TEXT_BUTTON}>
                  Reset
                </button>
              )}
            </p>
            <div className="flex min-w-0 flex-col gap-4">
              {lead.map(row)}
              {rest.map(row)}
            </div>
            {needsFilter && !needsKeyword && (
              <p role="alert" className="m-0 mt-4 text-xs text-warn">
                Add a filter to run this search.
              </p>
            )}
            {estimate !== null && (
              <div
                className={cn(
                  "mt-4 inline-flex w-fit items-center gap-2 rounded-[var(--radius-s)] border px-3 py-1.5 text-xs",
                  estimate > 3000
                    ? "border-[color-mix(in_srgb,var(--warn)_40%,transparent)] bg-warn-soft text-warn"
                    : "border-line bg-secondary/50 text-muted-foreground",
                )}
              >
                <Info className="size-3.5 shrink-0" />
                <span className="font-mono font-medium">~{approxCount(estimate).toLocaleString()}</span>
                <span>
                  {estimate > 3000 ? "results — consider adding more filters" : "estimated results"}
                </span>
              </div>
            )}
          </section>

          {search.notes.length > 0 && (
            <ul
              aria-label="Notes"
              className="m-0 -mt-2 flex list-none flex-col gap-1 p-0 text-xs text-muted-foreground"
            >
              {search.notes.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          )}
          {search.blockers.length > 0 && (
            <ul
              aria-label="Blockers"
              className="m-0 -mt-2 flex list-none flex-col gap-1 p-0 text-xs text-warn"
            >
              {search.blockers.map((blocker) => (
                <li key={blocker}>{blocker}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {actions && (
        <div className="flex min-w-0 gap-2 pt-5">
          <Button
            variant="outline"
            size="lg"
            className="flex-1"
            onClick={() => {
              setModalFilters(toExpanded(valuesWithDrafts(), LABELS))
              setFiltersOpen(true)
            }}
          >
            Edit Filters
          </Button>
          {search.blockers.length === 0 ? (
            <Button
              variant="primary"
              size="lg"
              className="flex-1"
              onClick={() => submit(true)}
              disabled={needsKeyword || needsFilter}
            >
              Run Search
            </Button>
          ) : (
            <Button
              variant="primary"
              size="lg"
              className="flex-1"
              onClick={() => submit(false)}
              disabled={!dirty}
            >
              Send changes
            </Button>
          )}
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
        </div>
      )}
    </section>
  )
}
