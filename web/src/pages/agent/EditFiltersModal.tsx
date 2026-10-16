// The cloud app's Edit Filters modal (network-search-app `EditFiltersModalV2.tsx`), copied as it
// is, minus what needs the cloud: the entity autocomplete (names are typed and kept on Enter),
// the live result count, the owner-only Debug section. Keywords edit the payload's keyword list.
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { EditableBadgeList } from "@/components/ui/editable-badge-list"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { ScrollArea } from "@/components/ui/scroll-area"
import { CheckboxScrollList } from "@/components/ui/checkbox-scroll-list"
import { cn } from "@/lib/utils"
import {
  Briefcase,
  GraduationCap,
  MapPin,
  Building2,
  Users,
  MessageSquare,
  ChevronRight,
  ChevronsUpDown,
  X,
} from "lucide-react"
import { AutocompleteInput, type EntityValue } from "./AutocompleteInput"
import {
  type ExpandedFilters,
  type EntityItem,
  flattenPersonLocations,
  flattenCompanyLocations,
  unflattenPersonLocations,
  unflattenCompanyLocations,
} from "@/lib/agent/expandedFilters"
import { useState } from "react"
import {
  ENTITY_TYPE_OPTIONS,
  SECTOR_TYPE_OPTIONS,
  FUNDING_STAGES,
  SENIORITY_BANDS,
  fuzzySearch,
} from "@/lib/agent/companyFilters"

const LOCATION_KINDS: [string, string][] = [
  ["metro_area", "Metro area"],
  ["city", "City"],
  ["state", "State"],
  ["country", "Country"],
  ["macro_region", "Region"],
]

const DEGREE_LEVEL_OPTIONS = [
  { id: "Bachelors", label: "Bachelor's" },
  { id: "Masters", label: "Master's" },
  { id: "MBA", label: "MBA" },
  { id: "PhD", label: "PhD" },
  { id: "MD", label: "MD" },
  { id: "JD", label: "JD" },
] as const

interface EditFiltersModalV2Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  filters: ExpandedFilters
  onUpdate: (
    key: keyof ExpandedFilters,
    values: EntityItem[] | string[] | number | boolean | string | null,
  ) => void
  onApply: () => void
}

type FilterSection = "roles" | "education" | "person-location" | "company" | "social" | "interactions"

interface SectionConfig {
  id: FilterSection
  label: string
  icon: React.ComponentType<{ className?: string }>
  getActiveCount: (filters: ExpandedFilters) => number
}

const SECTIONS: SectionConfig[] = [
  {
    id: "roles",
    label: "Experience",
    icon: Briefcase,
    getActiveCount: (f) => {
      let count = 0
      if (f.role_ids?.length) count += f.role_ids.length
      if (f.seniority_bands?.length) count += f.seniority_bands.length
      if (f.role_tracks?.length) count += f.role_tracks.length
      if (f.role_semantic_query) count += 1
      if (f.years_experience_min != null || f.years_experience_max != null) count += 1
      if (f.age_min != null || f.age_max != null) count += 1
      return count
    },
  },
  {
    id: "education",
    label: "Education",
    icon: GraduationCap,
    getActiveCount: (f) => {
      let count = f.education_ids?.length || 0
      if (f.degree_levels?.length) count += f.degree_levels.length
      if (f.fields_of_study?.length) count += f.fields_of_study.length
      if (f.graduation_year_min != null || f.graduation_year_max != null) count += 1
      return count
    },
  },
  {
    id: "person-location",
    label: "Location",
    icon: MapPin,
    getActiveCount: (f) => {
      return (
        (f.cities?.length || 0) +
        (f.states?.length || 0) +
        (f.countries?.length || 0) +
        (f.metro_areas?.length || 0) +
        (f.macro_regions?.length || 0)
      )
    },
  },
  {
    id: "company",
    label: "Company",
    icon: Building2,
    getActiveCount: (f) => {
      let count = 0
      if (f.company_ids?.length) count += f.company_ids.length
      if (f.investors?.length) count += f.investors.length
      if (f.sector_types?.length) count += f.sector_types.length
      if (f.funding_stage_min || f.funding_stage_max) count += 1
      if (f.funding_amount_min || f.funding_amount_max) count += 1
      if (f.headcount_min || f.headcount_max) count += 1
      if (f.founded_year_min || f.founded_year_max) count += 1
      if (f.last_funding_after || f.last_funding_before) count += 1
      if (f.valuation_min || f.valuation_max) count += 1
      if (f.company_cities?.length || f.company_states?.length || f.company_countries?.length) count += 1
      return count
    },
  },
  {
    id: "social",
    label: "Social",
    icon: Users,
    getActiveCount: (f) => {
      let count = 0
      if (f.x_followers_min != null || f.x_followers_max != null) count += 1
      if (f.li_followers_min != null || f.li_followers_max != null) count += 1
      if (f.li_connections_min != null || f.li_connections_max != null) count += 1
      if (f.ig_followers_min != null || f.ig_followers_max != null) count += 1
      return count
    },
  },
  {
    id: "interactions",
    label: "Interactions",
    icon: MessageSquare,
    getActiveCount: (f) => {
      let count = 0
      if (f.operator_interaction_min != null || f.operator_interaction_max != null) count += 1
      if (f.set_interaction_min != null || f.set_interaction_max != null) count += 1
      return count
    },
  },
]

const RANGE_ROW_CLASS = "grid grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-center gap-2"
const RANGE_ROW_WITH_UNIT_CLASS = `${RANGE_ROW_CLASS} sm:flex`
const RANGE_INPUT_CLASS = "w-full min-w-0"
const RANGE_UNIT_CLASS = "col-span-full text-sm text-muted-foreground sm:col-span-auto"

export function EditFiltersModalV2({
  open,
  onOpenChange,
  filters,
  onUpdate,
  onApply,
}: EditFiltersModalV2Props) {
  const [activeSection, setActiveSection] = useState<FilterSection>("roles")
  const visibleSections = SECTIONS

  // Helper functions
  const toEntityItems = (values: EntityValue[]): EntityItem[] =>
    values.map((v) => ({
      id: v.id,
      display_value: v.display_value,
      type: v.type || "",
      score: 0,
      metadata: null,
    }))

  const toEntityValues = (items: EntityItem[]): EntityValue[] =>
    items.map((item) => ({
      id: item.id,
      display_value: item.display_value,
      type: item.type,
    }))

  // Search handlers: the fixed vocabularies match locally; everything else is typed and kept on Enter.
  const handleSearch = async (query: string, searchType: string) => {
    const trimmed = query.trim()
    if (searchType === "entity_type") return fuzzySearch(trimmed, ENTITY_TYPE_OPTIONS)
    if (searchType === "sector_type") return fuzzySearch(trimmed, SECTOR_TYPE_OPTIONS)
    return []
  }

  const handleFetchPreview = async (_searchType: string, previewItems?: EntityItem[]) => previewItems ?? []

  // Location handlers
  const personLocations = flattenPersonLocations(filters)
  const companyLocations = flattenCompanyLocations(filters)

  // Without the cloud's entity service a typed location has no kind, so each location input
  // has a kind picker beside it and a typed name takes the kind picked.
  const [personKind, setPersonKind] = useState("metro_area")
  const [companyKind, setCompanyKind] = useState("metro_area")
  const withKind = (locations: EntityItem[], kind: string) =>
    locations.map((loc) => (loc.type ? loc : { ...loc, type: kind }))

  const handlePersonLocationUpdate = (locations: EntityItem[]) => {
    const unflattened = unflattenPersonLocations(withKind(locations, personKind))
    Object.entries(unflattened).forEach(([key, value]) => {
      onUpdate(key as keyof ExpandedFilters, (value || []) as EntityItem[])
    })
  }

  const handleCompanyLocationUpdate = (locations: EntityItem[]) => {
    const unflattened = unflattenCompanyLocations(withKind(locations, companyKind))
    Object.entries(unflattened).forEach(([key, value]) => {
      onUpdate(key as keyof ExpandedFilters, (value || []) as EntityItem[])
    })
  }

  const kindPicker = (value: string, onChange: (kind: string) => void, label: string) => (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger className="w-40" aria-label={label}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {LOCATION_KINDS.map(([id, name]) => (
          <SelectItem key={id} value={id}>
            {name}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )

  // Render section content
  const renderSectionContent = () => {
    switch (activeSection) {
      case "roles":
        return (
          <div className="space-y-6">
            {/* Role Query */}
            <div className="space-y-2">
              <Label>Role Query</Label>
              <Textarea
                placeholder="Describe the type of role you're looking for..."
                value={filters.role_semantic_query || ""}
                onChange={(e) => onUpdate("role_semantic_query", e.target.value || null)}
                className="min-h-[60px] resize-none"
              />
              <p className="text-xs text-muted-foreground">Semantic search for role descriptions</p>
            </div>

            {/* Seniority Level */}
            <div className="space-y-2">
              <div className="flex justify-between items-center">
                <Label>Seniority Level</Label>
                {filters.seniority_bands?.length > 0 && (
                  <button
                    className="text-xs text-muted-foreground underline hover:text-foreground transition-colors"
                    onClick={() => onUpdate("seniority_bands", [])}
                  >
                    clear
                  </button>
                )}
              </div>
              <Popover modal={false}>
                <PopoverTrigger asChild>
                  <Button variant="outline" className="w-full justify-between font-normal">
                    {filters.seniority_bands?.length > 0
                      ? filters.seniority_bands.map((s: EntityItem) => s.display_value).join(", ")
                      : "Select seniority levels..."}
                    <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
                  </Button>
                </PopoverTrigger>
                <PopoverContent className="w-[--radix-popover-trigger-width] p-0" align="start">
                  <CheckboxScrollList
                    items={SENIORITY_BANDS}
                    selectedIds={new Set((filters.seniority_bands || []).map((s: EntityItem) => s.id))}
                    onAdd={(id) => {
                      const band = SENIORITY_BANDS.find((b) => b.id === id)
                      if (band) onUpdate("seniority_bands", [...(filters.seniority_bands || []), band])
                    }}
                    onRemove={(id) => {
                      onUpdate(
                        "seniority_bands",
                        (filters.seniority_bands || []).filter((s: EntityItem) => s.id !== id),
                      )
                    }}
                  />
                </PopoverContent>
              </Popover>
            </div>

            {/* Role Keywords */}
            <div className="space-y-2">
              <Label>Role Keywords</Label>
              <EditableBadgeList
                items={filters.role_bm25_queries || []}
                variant="secondary"
                placeholder="Add keyword..."
                onAdd={(keyword) => {
                  onUpdate("role_bm25_queries", [...(filters.role_bm25_queries || []), keyword])
                }}
                onRemove={(keyword) => {
                  onUpdate(
                    "role_bm25_queries",
                    (filters.role_bm25_queries || []).filter((k) => k !== keyword),
                  )
                }}
                onEdit={(oldValue, newValue) => {
                  onUpdate(
                    "role_bm25_queries",
                    (filters.role_bm25_queries || []).map((k) => (k === oldValue ? newValue : k)),
                  )
                }}
              />
              <p className="text-xs text-muted-foreground">Keywords matched against job titles</p>
            </div>

            {/* Years of Experience */}
            <div className="space-y-2">
              <Label>Years of Experience</Label>
              <div className={RANGE_ROW_WITH_UNIT_CLASS}>
                <Input
                  type="number"
                  placeholder="Min"
                  min={0}
                  value={filters.years_experience_min ?? ""}
                  onChange={(e) =>
                    onUpdate("years_experience_min", e.target.value ? Number(e.target.value) : null)
                  }
                  className={RANGE_INPUT_CLASS}
                />
                <span className="text-muted-foreground text-sm">to</span>
                <Input
                  type="number"
                  placeholder="Max"
                  min={0}
                  value={filters.years_experience_max ?? ""}
                  onChange={(e) =>
                    onUpdate("years_experience_max", e.target.value ? Number(e.target.value) : null)
                  }
                  className={RANGE_INPUT_CLASS}
                />
                <span className={RANGE_UNIT_CLASS}>years</span>
              </div>
            </div>

            {/* Age Range */}
            <div className="space-y-2">
              <Label>Age Range</Label>
              <div className={RANGE_ROW_WITH_UNIT_CLASS}>
                <Input
                  type="number"
                  placeholder="Min"
                  min={0}
                  value={filters.age_min ?? ""}
                  onChange={(e) => onUpdate("age_min", e.target.value ? Number(e.target.value) : null)}
                  className={RANGE_INPUT_CLASS}
                />
                <span className="text-muted-foreground text-sm">to</span>
                <Input
                  type="number"
                  placeholder="Max"
                  min={0}
                  value={filters.age_max ?? ""}
                  onChange={(e) => onUpdate("age_max", e.target.value ? Number(e.target.value) : null)}
                  className={RANGE_INPUT_CLASS}
                />
                <span className={RANGE_UNIT_CLASS}>years old</span>
              </div>
            </div>
          </div>
        )

      case "education":
        return (
          <div className="space-y-6">
            {/* Schools */}
            <div className="space-y-2">
              <div className="flex justify-between items-center">
                <Label>Schools</Label>
                {filters.education_ids?.length > 0 && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-6 px-2 text-xs"
                    onClick={() => onUpdate("education_ids", [])}
                  >
                    Clear
                  </Button>
                )}
              </div>
              <AutocompleteInput
                key="education"
                placeholder="e.g., Stanford University"
                value={toEntityValues(filters.education_ids || [])}
                onChange={(v) => onUpdate("education_ids", toEntityItems(v))}
                onSearch={(q) => handleSearch(q, "education")}
              />
            </div>

            {/* Degree Level */}
            <div className="space-y-2">
              <div className="flex justify-between items-center">
                <Label>Degree Level</Label>
                {filters.degree_levels?.length > 0 && (
                  <button
                    className="text-xs text-muted-foreground underline hover:text-foreground transition-colors"
                    onClick={() => onUpdate("degree_levels", [])}
                  >
                    clear
                  </button>
                )}
              </div>
              <Popover>
                <PopoverTrigger asChild>
                  <Button variant="outline" className="w-full justify-between font-normal">
                    {filters.degree_levels?.length > 0
                      ? filters.degree_levels.map((d: EntityItem) => d.display_value).join(", ")
                      : "Select degrees..."}
                    <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
                  </Button>
                </PopoverTrigger>
                <PopoverContent className="w-[--radix-popover-trigger-width] p-2" align="start">
                  {DEGREE_LEVEL_OPTIONS.map((degree) => {
                    const isActive = filters.degree_levels?.some((d: EntityItem) => d.id === degree.id)
                    return (
                      <label
                        key={degree.id}
                        className="flex items-center gap-2 rounded-sm px-2 py-1.5 text-sm cursor-pointer hover:bg-accent"
                      >
                        <Checkbox
                          checked={isActive}
                          onCheckedChange={(checked) => {
                            if (checked) {
                              onUpdate("degree_levels", [
                                ...(filters.degree_levels || []),
                                { id: degree.id, display_value: degree.label, type: "degree_level" },
                              ])
                            } else {
                              onUpdate(
                                "degree_levels",
                                (filters.degree_levels || []).filter((d: EntityItem) => d.id !== degree.id),
                              )
                            }
                          }}
                        />
                        {degree.label}
                      </label>
                    )
                  })}
                </PopoverContent>
              </Popover>
            </div>

            {/* Fields of Study */}
            {filters.fields_of_study?.length > 0 && (
              <div className="space-y-2">
                <div className="flex justify-between items-center">
                  <Label>Fields of Study</Label>
                  <button
                    className="text-xs text-muted-foreground underline hover:text-foreground transition-colors"
                    onClick={() => onUpdate("fields_of_study", [])}
                  >
                    clear
                  </button>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {filters.fields_of_study.map((f: EntityItem) => (
                    <Badge key={f.id} variant="secondary" className="gap-1 pr-1">
                      {f.display_value}
                      <button
                        onClick={() =>
                          onUpdate(
                            "fields_of_study",
                            filters.fields_of_study.filter((x: EntityItem) => x.id !== f.id),
                          )
                        }
                        className="ml-0.5 rounded-full hover:bg-muted-foreground/20 p-0.5"
                      >
                        <X className="h-3 w-3" />
                      </button>
                    </Badge>
                  ))}
                </div>
                <p className="text-xs text-muted-foreground">
                  Extracted from your query (e.g., Psychology, Computer Science)
                </p>
              </div>
            )}

            {/* Graduation Year */}
            <div className="space-y-2">
              <div className="flex justify-between items-center">
                <Label>Graduation Year</Label>
                {(filters.graduation_year_min != null || filters.graduation_year_max != null) && (
                  <button
                    className="text-xs text-muted-foreground underline hover:text-foreground transition-colors"
                    onClick={() => {
                      onUpdate("graduation_year_min", null)
                      onUpdate("graduation_year_max", null)
                    }}
                  >
                    clear
                  </button>
                )}
              </div>
              <div className={RANGE_ROW_CLASS}>
                <Input
                  type="number"
                  min="1970"
                  max="2030"
                  placeholder="From"
                  value={filters.graduation_year_min ?? ""}
                  onChange={(e) => {
                    const raw = e.target.value
                    if (raw === "") {
                      onUpdate("graduation_year_min", null)
                      return
                    }
                    const v = parseInt(raw, 10)
                    if (!isNaN(v)) onUpdate("graduation_year_min", v)
                  }}
                  className={RANGE_INPUT_CLASS}
                />
                <span className="text-muted-foreground text-sm">to</span>
                <Input
                  type="number"
                  min="1970"
                  max="2030"
                  placeholder="To"
                  value={filters.graduation_year_max ?? ""}
                  onChange={(e) => {
                    const raw = e.target.value
                    if (raw === "") {
                      onUpdate("graduation_year_max", null)
                      return
                    }
                    const v = parseInt(raw, 10)
                    if (!isNaN(v)) onUpdate("graduation_year_max", v)
                  }}
                  className={RANGE_INPUT_CLASS}
                />
              </div>
              <p className="text-xs text-muted-foreground">Filter by graduation year range</p>
            </div>
          </div>
        )

      case "person-location":
        return (
          <div className="space-y-6">
            <div className="space-y-2">
              <div className="flex justify-between items-center">
                <Label>Person Locations</Label>
                {personLocations.length > 0 && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-6 px-2 text-xs"
                    onClick={() => handlePersonLocationUpdate([])}
                  >
                    Clear
                  </Button>
                )}
              </div>
              <div className="flex items-start gap-2">
                <AutocompleteInput
                  key="person-location"
                  className="min-w-0 flex-1"
                  placeholder="e.g., San Francisco, California"
                  value={toEntityValues(personLocations)}
                  onChange={(v) => handlePersonLocationUpdate(toEntityItems(v))}
                  onSearch={(q) => handleSearch(q, "location")}
                  groupBy="type"
                />
                {kindPicker(personKind, setPersonKind, "Person location kind")}
              </div>
              <p className="text-xs text-muted-foreground">Where candidates are located</p>
            </div>
          </div>
        )

      case "company":
        return (
          <div className="space-y-6">
            {/* Companies */}
            <div className="space-y-2">
              <div className="flex justify-between items-center">
                <Label>Companies</Label>
                {filters.company_ids?.length > 0 && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-6 px-2 text-xs"
                    onClick={() => onUpdate("company_ids", [])}
                  >
                    Clear
                  </Button>
                )}
              </div>
              <AutocompleteInput
                key="company"
                placeholder="e.g., Google"
                value={toEntityValues(filters.company_ids || [])}
                onChange={(v) => onUpdate("company_ids", toEntityItems(v))}
                onSearch={(q) => handleSearch(q, "company")}
              />
            </div>

            {/* Company Location */}
            <div className="space-y-2">
              <div className="flex justify-between items-center">
                <Label>Company Locations</Label>
                {companyLocations.length > 0 && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-6 px-2 text-xs"
                    onClick={() => handleCompanyLocationUpdate([])}
                  >
                    Clear
                  </Button>
                )}
              </div>
              <div className="flex items-start gap-2">
                <AutocompleteInput
                  key="company-location"
                  className="min-w-0 flex-1"
                  placeholder="e.g., San Francisco"
                  value={toEntityValues(companyLocations)}
                  onChange={(v) => handleCompanyLocationUpdate(toEntityItems(v))}
                  onSearch={(q) => handleSearch(q, "location")}
                  groupBy="type"
                />
                {kindPicker(companyKind, setCompanyKind, "Company location kind")}
              </div>
            </div>

            {/* Funding Stage */}
            <div className="space-y-2">
              <div className="flex justify-between items-center">
                <Label>Funding Stage</Label>
                {(filters.funding_stage_min || filters.funding_stage_max) && (
                  <button
                    className="text-xs text-muted-foreground underline hover:text-foreground transition-colors"
                    onClick={() => {
                      onUpdate("funding_stage_min", null)
                      onUpdate("funding_stage_max", null)
                    }}
                  >
                    clear
                  </button>
                )}
              </div>
              <div className={RANGE_ROW_CLASS}>
                <Select
                  value={filters.funding_stage_min || "__any__"}
                  onValueChange={(v) => onUpdate("funding_stage_min", v === "__any__" ? null : v)}
                >
                  <SelectTrigger className={RANGE_INPUT_CLASS}>
                    <SelectValue placeholder="Min" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__any__">None</SelectItem>
                    {Object.entries(FUNDING_STAGES).map(([id, label]) => (
                      <SelectItem key={id} value={id}>
                        {label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <span className="text-muted-foreground text-sm">to</span>
                <Select
                  value={filters.funding_stage_max || "__any__"}
                  onValueChange={(v) => onUpdate("funding_stage_max", v === "__any__" ? null : v)}
                >
                  <SelectTrigger className={RANGE_INPUT_CLASS}>
                    <SelectValue placeholder="Max" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__any__">None</SelectItem>
                    {Object.entries(FUNDING_STAGES).map(([id, label]) => (
                      <SelectItem key={id} value={id}>
                        {label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>

            {/* Funding Amount */}
            <div className="space-y-2">
              <div className="flex justify-between items-center">
                <Label>Funding Amount (millions USD)</Label>
                {(filters.funding_amount_min != null || filters.funding_amount_max != null) && (
                  <button
                    className="text-xs text-muted-foreground underline hover:text-foreground transition-colors"
                    onClick={() => {
                      onUpdate("funding_amount_min", null)
                      onUpdate("funding_amount_max", null)
                    }}
                  >
                    clear
                  </button>
                )}
              </div>
              <div className={RANGE_ROW_CLASS}>
                <Input
                  type="number"
                  min="0"
                  step="0.1"
                  placeholder="Min"
                  value={filters.funding_amount_min ?? ""}
                  onChange={(e) => {
                    const v = e.target.value === "" ? null : parseFloat(e.target.value)
                    if (v === null || (!isNaN(v) && v >= 0)) onUpdate("funding_amount_min", v)
                  }}
                  className={RANGE_INPUT_CLASS}
                />
                <span className="text-muted-foreground text-sm">to</span>
                <Input
                  type="number"
                  min="0"
                  step="0.1"
                  placeholder="Max"
                  value={filters.funding_amount_max ?? ""}
                  onChange={(e) => {
                    const v = e.target.value === "" ? null : parseFloat(e.target.value)
                    if (v === null || (!isNaN(v) && v >= 0)) onUpdate("funding_amount_max", v)
                  }}
                  className={RANGE_INPUT_CLASS}
                />
              </div>
            </div>

            {/* Headcount */}
            <div className="space-y-2">
              <div className="flex justify-between items-center">
                <Label>Headcount</Label>
                {(filters.headcount_min != null || filters.headcount_max != null) && (
                  <button
                    className="text-xs text-muted-foreground underline hover:text-foreground transition-colors"
                    onClick={() => {
                      onUpdate("headcount_min", null)
                      onUpdate("headcount_max", null)
                    }}
                  >
                    clear
                  </button>
                )}
              </div>
              <div className={RANGE_ROW_CLASS}>
                <Input
                  type="number"
                  min="0"
                  placeholder="Min"
                  value={filters.headcount_min ?? ""}
                  onChange={(e) => {
                    const v = e.target.value === "" ? null : parseInt(e.target.value, 10)
                    if (v === null || (!isNaN(v) && v >= 0)) onUpdate("headcount_min", v)
                  }}
                  className={RANGE_INPUT_CLASS}
                />
                <span className="text-muted-foreground text-sm">to</span>
                <Input
                  type="number"
                  min="0"
                  placeholder="Max"
                  value={filters.headcount_max ?? ""}
                  onChange={(e) => {
                    const v = e.target.value === "" ? null : parseInt(e.target.value, 10)
                    if (v === null || (!isNaN(v) && v >= 0)) onUpdate("headcount_max", v)
                  }}
                  className={RANGE_INPUT_CLASS}
                />
              </div>
            </div>

            {/* Founded Year */}
            <div className="space-y-2">
              <div className="flex justify-between items-center">
                <Label>Founded Year</Label>
                {(filters.founded_year_min != null || filters.founded_year_max != null) && (
                  <button
                    className="text-xs text-muted-foreground hover:text-foreground"
                    onClick={() => {
                      onUpdate("founded_year_min", null)
                      onUpdate("founded_year_max", null)
                    }}
                  >
                    clear
                  </button>
                )}
              </div>
              <div className={RANGE_ROW_CLASS}>
                <Input
                  type="number"
                  min="1900"
                  max={new Date().getFullYear()}
                  placeholder="From"
                  value={filters.founded_year_min ?? ""}
                  onChange={(e) => {
                    const v = e.target.value === "" ? null : parseInt(e.target.value, 10)
                    if (v === null || !isNaN(v)) onUpdate("founded_year_min", v)
                  }}
                  className={RANGE_INPUT_CLASS}
                />
                <span className="text-muted-foreground text-sm">to</span>
                <Input
                  type="number"
                  min="1900"
                  max={new Date().getFullYear()}
                  placeholder="To"
                  value={filters.founded_year_max ?? ""}
                  onChange={(e) => {
                    const v = e.target.value === "" ? null : parseInt(e.target.value, 10)
                    if (v === null || !isNaN(v)) onUpdate("founded_year_max", v)
                  }}
                  className={RANGE_INPUT_CLASS}
                />
              </div>
            </div>

            {/* Last Funding Date */}
            <div className="space-y-2">
              <div className="flex justify-between items-center">
                <Label>Last Funding Date</Label>
                {(filters.last_funding_after != null || filters.last_funding_before != null) && (
                  <button
                    className="text-xs text-muted-foreground hover:text-foreground"
                    onClick={() => {
                      onUpdate("last_funding_after", null)
                      onUpdate("last_funding_before", null)
                    }}
                  >
                    clear
                  </button>
                )}
              </div>
              <div className={RANGE_ROW_CLASS}>
                <Input
                  type="date"
                  value={filters.last_funding_after ?? ""}
                  onChange={(e) => onUpdate("last_funding_after", e.target.value || null)}
                  className={RANGE_INPUT_CLASS}
                />
                <span className="text-muted-foreground text-sm">to</span>
                <Input
                  type="date"
                  value={filters.last_funding_before ?? ""}
                  onChange={(e) => onUpdate("last_funding_before", e.target.value || null)}
                  className={RANGE_INPUT_CLASS}
                />
              </div>
            </div>

            {/* Valuation ($M) */}
            <div className="space-y-2">
              <div className="flex justify-between items-center">
                <Label>Valuation (millions USD)</Label>
                {(filters.valuation_min != null || filters.valuation_max != null) && (
                  <button
                    className="text-xs text-muted-foreground hover:text-foreground"
                    onClick={() => {
                      onUpdate("valuation_min", null)
                      onUpdate("valuation_max", null)
                    }}
                  >
                    clear
                  </button>
                )}
              </div>
              <div className={RANGE_ROW_CLASS}>
                <Input
                  type="number"
                  min="0"
                  step="0.1"
                  placeholder="Min ($M)"
                  value={filters.valuation_min ?? ""}
                  onChange={(e) => {
                    const v = e.target.value === "" ? null : parseFloat(e.target.value)
                    if (v === null || (!isNaN(v) && v >= 0)) onUpdate("valuation_min", v)
                  }}
                  className={RANGE_INPUT_CLASS}
                />
                <span className="text-muted-foreground text-sm">to</span>
                <Input
                  type="number"
                  min="0"
                  step="0.1"
                  placeholder="Max ($M)"
                  value={filters.valuation_max ?? ""}
                  onChange={(e) => {
                    const v = e.target.value === "" ? null : parseFloat(e.target.value)
                    if (v === null || (!isNaN(v) && v >= 0)) onUpdate("valuation_max", v)
                  }}
                  className={RANGE_INPUT_CLASS}
                />
              </div>
            </div>

            {/* Entity Types (only if extracted) & Sectors */}
            <div
              className={`grid gap-4 ${filters.entity_types?.length ? "grid-cols-1 sm:grid-cols-2" : "grid-cols-1"}`}
            >
              {filters.entity_types?.length > 0 && (
                <div className="space-y-2">
                  <Label>Entity Type</Label>
                  <AutocompleteInput
                    key="entity-type"
                    placeholder="e.g., Startup"
                    value={toEntityValues(filters.entity_types || [])}
                    onChange={(v) => onUpdate("entity_types", toEntityItems(v))}
                    onSearch={(q) => handleSearch(q, "entity_type")}
                    onFetchPreview={() => handleFetchPreview("entity_type", ENTITY_TYPE_OPTIONS)}
                  />
                </div>
              )}
              <div className="space-y-2">
                <Label>Sector</Label>
                <AutocompleteInput
                  key="sector-type"
                  placeholder="e.g., AI/ML"
                  value={toEntityValues(filters.sector_types || [])}
                  onChange={(v) => onUpdate("sector_types", toEntityItems(v))}
                  onSearch={(q) => handleSearch(q, "sector_type")}
                  onFetchPreview={() => handleFetchPreview("sector_type", SECTOR_TYPE_OPTIONS)}
                />
              </div>
            </div>

            {/* Investors */}
            <div className="space-y-2">
              <Label>Investors</Label>
              <AutocompleteInput
                key="investors"
                placeholder="e.g., Y Combinator"
                value={toEntityValues(filters.investors || [])}
                onChange={(v) => onUpdate("investors", toEntityItems(v))}
                onSearch={(q) => handleSearch(q, "investor")}
              />
            </div>
          </div>
        )

      case "social":
        return (
          <div className="space-y-6">
            {/* X / Twitter Followers */}
            <div className="space-y-2">
              <div className="flex justify-between items-center">
                <Label>X / Twitter Followers</Label>
                {(filters.x_followers_min != null || filters.x_followers_max != null) && (
                  <button
                    className="text-xs text-muted-foreground underline hover:text-foreground transition-colors"
                    onClick={() => {
                      onUpdate("x_followers_min", null)
                      onUpdate("x_followers_max", null)
                    }}
                  >
                    clear
                  </button>
                )}
              </div>
              <div className={RANGE_ROW_CLASS}>
                <Input
                  type="number"
                  min="0"
                  placeholder="Min"
                  value={filters.x_followers_min ?? ""}
                  onChange={(e) => {
                    const v = e.target.value === "" ? null : parseInt(e.target.value, 10)
                    if (v === null || (!isNaN(v) && v >= 0)) onUpdate("x_followers_min", v)
                  }}
                  className={RANGE_INPUT_CLASS}
                />
                <span className="text-muted-foreground text-sm">to</span>
                <Input
                  type="number"
                  min="0"
                  placeholder="Max"
                  value={filters.x_followers_max ?? ""}
                  onChange={(e) => {
                    const v = e.target.value === "" ? null : parseInt(e.target.value, 10)
                    if (v === null || (!isNaN(v) && v >= 0)) onUpdate("x_followers_max", v)
                  }}
                  className={RANGE_INPUT_CLASS}
                />
              </div>
            </div>

            {/* LinkedIn Followers */}
            <div className="space-y-2">
              <div className="flex justify-between items-center">
                <Label>LinkedIn Followers</Label>
                {(filters.li_followers_min != null || filters.li_followers_max != null) && (
                  <button
                    className="text-xs text-muted-foreground underline hover:text-foreground transition-colors"
                    onClick={() => {
                      onUpdate("li_followers_min", null)
                      onUpdate("li_followers_max", null)
                    }}
                  >
                    clear
                  </button>
                )}
              </div>
              <div className={RANGE_ROW_CLASS}>
                <Input
                  type="number"
                  min="0"
                  placeholder="Min"
                  value={filters.li_followers_min ?? ""}
                  onChange={(e) => {
                    const v = e.target.value === "" ? null : parseInt(e.target.value, 10)
                    if (v === null || (!isNaN(v) && v >= 0)) onUpdate("li_followers_min", v)
                  }}
                  className={RANGE_INPUT_CLASS}
                />
                <span className="text-muted-foreground text-sm">to</span>
                <Input
                  type="number"
                  min="0"
                  placeholder="Max"
                  value={filters.li_followers_max ?? ""}
                  onChange={(e) => {
                    const v = e.target.value === "" ? null : parseInt(e.target.value, 10)
                    if (v === null || (!isNaN(v) && v >= 0)) onUpdate("li_followers_max", v)
                  }}
                  className={RANGE_INPUT_CLASS}
                />
              </div>
            </div>

            {/* LinkedIn Connections */}
            <div className="space-y-2">
              <div className="flex justify-between items-center">
                <Label>LinkedIn Connections</Label>
                {(filters.li_connections_min != null || filters.li_connections_max != null) && (
                  <button
                    className="text-xs text-muted-foreground underline hover:text-foreground transition-colors"
                    onClick={() => {
                      onUpdate("li_connections_min", null)
                      onUpdate("li_connections_max", null)
                    }}
                  >
                    clear
                  </button>
                )}
              </div>
              <div className={RANGE_ROW_CLASS}>
                <Input
                  type="number"
                  min="0"
                  placeholder="Min"
                  value={filters.li_connections_min ?? ""}
                  onChange={(e) => {
                    const v = e.target.value === "" ? null : parseInt(e.target.value, 10)
                    if (v === null || (!isNaN(v) && v >= 0)) onUpdate("li_connections_min", v)
                  }}
                  className={RANGE_INPUT_CLASS}
                />
                <span className="text-muted-foreground text-sm">to</span>
                <Input
                  type="number"
                  min="0"
                  placeholder="Max"
                  value={filters.li_connections_max ?? ""}
                  onChange={(e) => {
                    const v = e.target.value === "" ? null : parseInt(e.target.value, 10)
                    if (v === null || (!isNaN(v) && v >= 0)) onUpdate("li_connections_max", v)
                  }}
                  className={RANGE_INPUT_CLASS}
                />
              </div>
            </div>

            {/* Instagram Followers */}
            <div className="space-y-2">
              <div className="flex justify-between items-center">
                <Label>Instagram Followers</Label>
                {(filters.ig_followers_min != null || filters.ig_followers_max != null) && (
                  <button
                    className="text-xs text-muted-foreground underline hover:text-foreground transition-colors"
                    onClick={() => {
                      onUpdate("ig_followers_min", null)
                      onUpdate("ig_followers_max", null)
                    }}
                  >
                    clear
                  </button>
                )}
              </div>
              <div className={RANGE_ROW_CLASS}>
                <Input
                  type="number"
                  min="0"
                  placeholder="Min"
                  value={filters.ig_followers_min ?? ""}
                  onChange={(e) => {
                    const v = e.target.value === "" ? null : parseInt(e.target.value, 10)
                    if (v === null || (!isNaN(v) && v >= 0)) onUpdate("ig_followers_min", v)
                  }}
                  className={RANGE_INPUT_CLASS}
                />
                <span className="text-muted-foreground text-sm">to</span>
                <Input
                  type="number"
                  min="0"
                  placeholder="Max"
                  value={filters.ig_followers_max ?? ""}
                  onChange={(e) => {
                    const v = e.target.value === "" ? null : parseInt(e.target.value, 10)
                    if (v === null || (!isNaN(v) && v >= 0)) onUpdate("ig_followers_max", v)
                  }}
                  className={RANGE_INPUT_CLASS}
                />
              </div>
              <p className="text-[11px] text-muted-foreground/60">Instagram data not yet populated</p>
            </div>
          </div>
        )

      case "interactions":
        return (
          <div className="space-y-6">
            {/* Individual (You) */}
            <div className="space-y-2">
              <div className="flex justify-between items-center">
                <Label>Individual (You)</Label>
                {(filters.operator_interaction_min != null || filters.operator_interaction_max != null) && (
                  <button
                    className="text-xs text-muted-foreground underline hover:text-foreground transition-colors"
                    onClick={() => {
                      onUpdate("operator_interaction_min", null)
                      onUpdate("operator_interaction_max", null)
                    }}
                  >
                    clear
                  </button>
                )}
              </div>
              <p className="text-[11px] text-muted-foreground/60">
                People you&apos;ve personally interacted with (emails, messages)
              </p>
              <div className={RANGE_ROW_CLASS}>
                <Input
                  type="number"
                  min="0"
                  placeholder="Min"
                  value={filters.operator_interaction_min ?? ""}
                  onChange={(e) => {
                    const v = e.target.value === "" ? null : parseInt(e.target.value, 10)
                    if (v === null || (!isNaN(v) && v >= 0)) onUpdate("operator_interaction_min", v)
                  }}
                  className={RANGE_INPUT_CLASS}
                />
                <span className="text-muted-foreground text-sm">to</span>
                <Input
                  type="number"
                  min="0"
                  placeholder="Max"
                  value={filters.operator_interaction_max ?? ""}
                  onChange={(e) => {
                    const v = e.target.value === "" ? null : parseInt(e.target.value, 10)
                    if (v === null || (!isNaN(v) && v >= 0)) onUpdate("operator_interaction_max", v)
                  }}
                  className={RANGE_INPUT_CLASS}
                />
              </div>
            </div>

            {/* Collective (Set) */}
            <div className="space-y-2">
              <div className="flex justify-between items-center">
                <Label>Collective (Set)</Label>
                {(filters.set_interaction_min != null || filters.set_interaction_max != null) && (
                  <button
                    className="text-xs text-muted-foreground underline hover:text-foreground transition-colors"
                    onClick={() => {
                      onUpdate("set_interaction_min", null)
                      onUpdate("set_interaction_max", null)
                    }}
                  >
                    clear
                  </button>
                )}
              </div>
              <p className="text-[11px] text-muted-foreground/60">
                People anyone in your network/team has interacted with
              </p>
              <div className={RANGE_ROW_CLASS}>
                <Input
                  type="number"
                  min="0"
                  placeholder="Min"
                  value={filters.set_interaction_min ?? ""}
                  onChange={(e) => {
                    const v = e.target.value === "" ? null : parseInt(e.target.value, 10)
                    if (v === null || (!isNaN(v) && v >= 0)) onUpdate("set_interaction_min", v)
                  }}
                  className={RANGE_INPUT_CLASS}
                />
                <span className="text-muted-foreground text-sm">to</span>
                <Input
                  type="number"
                  min="0"
                  placeholder="Max"
                  value={filters.set_interaction_max ?? ""}
                  onChange={(e) => {
                    const v = e.target.value === "" ? null : parseInt(e.target.value, 10)
                    if (v === null || (!isNaN(v) && v >= 0)) onUpdate("set_interaction_max", v)
                  }}
                  className={RANGE_INPUT_CLASS}
                />
              </div>
            </div>
          </div>
        )
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex h-[calc(100dvh_-_1rem_-_env(safe-area-inset-top)_-_env(safe-area-inset-bottom))] max-h-[calc(100dvh_-_1rem_-_env(safe-area-inset-top)_-_env(safe-area-inset-bottom))] w-[calc(100vw-1rem)] max-w-[calc(100vw-1rem)] flex-col gap-0 p-0 sm:h-[600px] sm:max-w-3xl [&>*]:min-w-0">
        {/* Header */}
        <div className="flex min-h-14 shrink-0 items-center justify-between gap-3 border-b px-4 py-3 sm:h-14 sm:px-6 sm:py-0">
          <DialogHeader className="min-w-0 p-0 space-y-0">
            <DialogTitle className="truncate">Edit Filters</DialogTitle>
          </DialogHeader>
        </div>

        {/* Main content with sidebar */}
        <div className="flex min-h-0 flex-1 flex-col sm:flex-row">
          {/* Sidebar */}
          <div className="shrink-0 border-b bg-muted/30 sm:w-48 sm:border-b-0 sm:border-r">
            <nav className="flex gap-1 overflow-x-auto p-2 sm:block sm:space-y-1">
              {visibleSections.map((section) => {
                const Icon = section.icon
                const activeCount = section.getActiveCount(filters)
                const isActive = activeSection === section.id

                return (
                  <button
                    key={section.id}
                    onClick={() => setActiveSection(section.id)}
                    className={cn(
                      "flex w-auto shrink-0 items-center gap-2 whitespace-nowrap rounded-md px-3 py-2 text-sm transition-colors sm:w-full",
                      "hover:bg-accent hover:text-accent-foreground",
                      isActive && "bg-accent text-accent-foreground font-medium",
                    )}
                  >
                    <Icon className="h-4 w-4 shrink-0" />
                    <span className="min-w-0 flex-1 truncate text-left">{section.label}</span>
                    {activeCount > 0 && (
                      <Badge variant="secondary" className="h-5 px-1.5 text-xs font-medium">
                        {activeCount}
                      </Badge>
                    )}
                    {isActive && (
                      <ChevronRight className="hidden h-4 w-4 shrink-0 text-muted-foreground sm:block" />
                    )}
                  </button>
                )
              })}
            </nav>
          </div>

          {/* Content area */}
          <ScrollArea className="min-w-0 flex-1">
            <div className="min-w-0 p-4 sm:p-6">{renderSectionContent()}</div>
          </ScrollArea>
        </div>

        {/* Footer */}
        <div className="flex min-h-14 shrink-0 items-center justify-end gap-2 border-t px-4 py-3 sm:h-14 sm:px-6 sm:py-0">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            onClick={() => {
              onOpenChange(false)
              onApply()
            }}
          >
            Apply Filters
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}

export const EditFiltersModal = EditFiltersModalV2
