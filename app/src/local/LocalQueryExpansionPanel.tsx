import { TraitChip } from "@/components/results/TraitChip";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import type { Trait } from "@/types/search";
import type { LocalRunDetail } from "./types";

interface LocalQueryExpansionPanelProps {
  run?: LocalRunDetail | null;
}

type ExpansionOutput = Record<string, any>;

const FILTER_LABELS: Record<string, string> = {
  role_ids: "Roles",
  role_tracks: "Role tracks",
  company_names: "Companies",
  investor_names: "Investors",
  cities: "Cities",
  states: "States",
  countries: "Countries",
  metro_areas: "Metro areas",
  macro_regions: "Regions",
  seniority_bands: "Seniority",
  sector_names: "Sectors",
  sector_types: "Sectors",
  tech_skills: "Skills",
  education_names: "Education",
  school_names: "Education",
  position_after_date: "After",
  position_before_date: "Before",
  is_current_role: "Current role",
};

const FILTER_ORDER = [
  "role_ids",
  "role_tracks",
  "company_names",
  "investor_names",
  "cities",
  "states",
  "countries",
  "metro_areas",
  "macro_regions",
  "seniority_bands",
  "sector_names",
  "sector_types",
  "tech_skills",
  "education_names",
  "school_names",
  "position_after_date",
  "position_before_date",
  "is_current_role",
];

const HIDDEN_KEYS = new Set([
  "bm25_queries",
  "semantic_query",
  "set_id",
  "role_core_patterns",
  "role_adjacent_patterns",
  // Internal local-index title clustering metadata; role keywords (bm25_queries)
  // are surfaced in the dedicated "Role Keywords" chip section instead.
  "local_title_clusters",
  "local_title_cluster_keywords",
  "local_title_clustering_status",
]);

function formatKey(key: string) {
  return FILTER_LABELS[key] || key.split("_").map((word) => word.charAt(0).toUpperCase() + word.slice(1)).join(" ");
}

function asArray(value: unknown): unknown[] {
  if (value == null) return [];
  return Array.isArray(value) ? value : [value];
}

function displayValue(value: unknown): string {
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "object" && value !== null) {
    const obj = value as Record<string, unknown>;
    return String(obj.display_value || obj.name || obj.id || JSON.stringify(value));
  }
  return String(value);
}

function getExpansion(run?: LocalRunDetail | null): ExpansionOutput | null {
  const rawSteps = run?.steps;
  const steps = Array.isArray(rawSteps)
    ? rawSteps
    : rawSteps && typeof rawSteps === "object"
      ? Object.values(rawSteps as Record<string, any>)
      : [];
  const step = steps.find((candidate) => candidate?.id === "expand_search_request");
  return (step?.output as ExpansionOutput | undefined) || null;
}

function getFilters(expansion: ExpansionOutput): Record<string, unknown> {
  return expansion.role_search_filters || expansion.filters || expansion.search_filters || {};
}

function getRoleKeywords(filters: Record<string, unknown>): string[] {
  const keywords = new Set<string>();
  for (const value of asArray(filters.bm25_queries)) keywords.add(displayValue(value));
  for (const pattern of asArray(filters.role_core_patterns)) {
    if (typeof pattern === "object" && pattern !== null && Array.isArray((pattern as any).examples)) {
      for (const example of (pattern as any).examples) keywords.add(displayValue(example));
    }
  }
  return [...keywords].filter(Boolean);
}

function getTraits(expansion: ExpansionOutput): Trait[] {
  return asArray(expansion.traits)
    .map((raw): Trait | null => {
      if (typeof raw === "string") {
        return { value: raw, temporal: "all", meaning: "general" };
      }
      if (typeof raw === "object" && raw !== null) {
        const obj = raw as Record<string, unknown>;
        const value = displayValue(obj.value || obj.trait || obj.name || "");
        if (!value) return null;
        return {
          value,
          temporal: obj.temporal === "current" || obj.temporal === "past" || obj.temporal === "all" ? obj.temporal : "all",
          meaning: typeof obj.meaning === "string" ? (obj.meaning as Trait["meaning"]) : "general",
        };
      }
      return null;
    })
    .filter(Boolean) as Trait[];
}

export function LocalQueryExpansionPanel({ run }: LocalQueryExpansionPanelProps) {
  const expansion = getExpansion(run);
  if (!expansion) return null;

  const filters = getFilters(expansion);
  const roleKeywords = getRoleKeywords(filters);
  const traits = getTraits(expansion);
  const semanticQuery = typeof filters.semantic_query === "string" ? filters.semantic_query : null;
  const notes = Array.isArray(expansion.notes) ? expansion.notes.map(displayValue).filter(Boolean) : [];

  const filterEntries = Object.entries(filters)
    .filter(([key, value]) => !HIDDEN_KEYS.has(key) && value != null && !(Array.isArray(value) && value.length === 0))
    .sort(([a], [b]) => {
      const ai = FILTER_ORDER.indexOf(a);
      const bi = FILTER_ORDER.indexOf(b);
      if (ai === -1 && bi === -1) return a.localeCompare(b);
      if (ai === -1) return 1;
      if (bi === -1) return -1;
      return ai - bi;
    });

  return (
    <Card>
      <CardContent className="space-y-4 p-4">
        <div className="space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-semibold text-foreground">Search parameters</h3>
            {expansion.intent_type && <Badge variant="outline">{displayValue(expansion.intent_type)}</Badge>}
            {expansion.vertical && <Badge variant="outline">{displayValue(expansion.vertical)}</Badge>}
          </div>
          {expansion.normalized_query && (
            <p className="text-sm text-muted-foreground">
              <span className="font-medium text-foreground">Expanded query:</span> {displayValue(expansion.normalized_query)}
            </p>
          )}
        </div>

        {traits.length > 0 && (
          <div>
            <p className="mb-2 text-sm font-medium text-foreground">Traits</p>
            <div className="flex flex-wrap gap-2">
              {traits.map((trait, index) => (
                <TraitChip key={`${trait.value}-${index}`} trait={trait} compact />
              ))}
            </div>
          </div>
        )}

        {roleKeywords.length > 0 && (
          <div>
            <p className="mb-2 text-sm font-medium text-foreground">Role Keywords</p>
            <div className="flex flex-wrap gap-2">
              {roleKeywords.map((keyword) => (
                <Badge key={keyword} variant="secondary">{keyword}</Badge>
              ))}
            </div>
          </div>
        )}

        {semanticQuery && (
          <div>
            <p className="mb-2 text-sm font-medium text-foreground">Semantic query</p>
            <p className="rounded-md bg-muted/50 p-3 text-sm text-muted-foreground">{semanticQuery}</p>
          </div>
        )}

        {filterEntries.length > 0 && (
          <div className="space-y-3">
            {filterEntries.map(([key, value]) => {
              const values = asArray(value).map(displayValue).filter(Boolean);
              if (values.length === 0) return null;
              return (
                <div key={key}>
                  <p className="mb-2 text-sm font-medium text-foreground">{formatKey(key)}:</p>
                  <div className="flex flex-wrap gap-2">
                    {values.map((item, index) => (
                      <Badge key={`${key}-${index}-${item}`} variant="secondary">{item}</Badge>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {notes.length > 0 && (
          <div>
            <p className="mb-2 text-sm font-medium text-foreground">Notes</p>
            <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
              {notes.map((note, index) => <li key={index}>{note}</li>)}
            </ul>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
