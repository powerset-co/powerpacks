import { useEffect, useMemo, useRef, useState } from "react";
import { CheckSquare, Download, Plus, Sparkles, Tag } from "lucide-react";

import { PersonInfoCell } from "@/components/results/PersonInfoCell";
import { PersonDebugPopover } from "@/components/results/PersonDebugPopover";
import { TraitScoreDisplay } from "@/components/results/TraitScoreDisplay";
import { TagsPopover } from "@/components/results/TagsPopover";
import { Badge, badgeVariants } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useTaggedResults } from "@/hooks/useTaggedResults";
import { cn } from "@/lib/utils";
import type { DatabaseRecord } from "@/types/search";

interface LocalResultsTableProps {
  records: DatabaseRecord[];
  query?: string;
  conversationId: string | null;
  totalCount?: number;
}

function scoreFor(record: DatabaseRecord): number {
  return Number(record.overall_trait_score ?? record.score ?? 0) || 0;
}

function csvEscape(value: unknown): string {
  const s = String(value ?? "");
  if (/[",\n]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
  return s;
}

function downloadCsv(records: DatabaseRecord[], fileName: string) {
  const fields = ["rank", "person_id", "name", "headline", "location", "score", "linkedin_url"];
  const lines = [fields.join(",")];
  for (const record of records) {
    lines.push([
      record.result_index ?? "",
      record.personId,
      record.name,
      record.headline || record.title,
      record.location,
      scoreFor(record),
      record.linkedin_url,
    ].map(csvEscape).join(","));
  }
  const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = fileName;
  anchor.click();
  URL.revokeObjectURL(url);
}

export function LocalResultsTable({ records, query, conversationId, totalCount }: LocalResultsTableProps) {
  const prevRecordCount = useRef(records.length);
  const {
    tags: conversationTags,
    taggedIds,
    isTagged,
    getTagsFor,
    toggleTag,
    removeTag,
    untagAll,
    count: taggedCount,
  } = useTaggedResults(conversationId);
  const [filterMode, setFilterMode] = useState<"main" | "tagged">("main");
  const [selectedTagFilters, setSelectedTagFilters] = useState<Set<string>>(new Set());

  const mainRecords = useMemo(() => {
    return [...records]
      .filter((record) => record?.personId)
      .sort((a, b) => scoreFor(b) - scoreFor(a));
  }, [records]);

  const taggedRecords = useMemo(() => {
    const tagged = mainRecords.filter((record) => taggedIds.has(record.personId));
    if (selectedTagFilters.size === 0) return tagged;
    return tagged.filter((record) => {
      const personTags = getTagsFor(record.personId);
      return personTags.some((tag) => selectedTagFilters.has(tag));
    });
  }, [mainRecords, taggedIds, selectedTagFilters, getTagsFor]);

  const displayedRecords = filterMode === "tagged" ? taggedRecords : mainRecords;
  const visibleTaggedCount = displayedRecords.filter((record) => isTagged(record.personId)).length;
  const mainResultsLabel = totalCount && totalCount > mainRecords.length
    ? `Main Results (${mainRecords.length.toLocaleString()} of ${totalCount.toLocaleString()})`
    : `Main Results (${mainRecords.length.toLocaleString()})`;

  useEffect(() => {
    const timer = setTimeout(() => { prevRecordCount.current = records.length; }, 400);
    return () => clearTimeout(timer);
  }, [records.length]);

  const toggleTagFilter = (tag: string) => {
    setSelectedTagFilters((prev) => {
      const next = new Set(prev);
      if (next.has(tag)) next.delete(tag);
      else next.add(tag);
      return next;
    });
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2 flex-wrap">
          <Badge
            variant={filterMode === "main" ? "default" : "outline"}
            className="cursor-pointer hover:bg-muted"
            onClick={() => setFilterMode("main")}
          >
            {mainResultsLabel}
          </Badge>
          <Badge variant="outline" className="text-muted-foreground">
            Bad Results (0)
          </Badge>
          {taggedCount > 0 && (
            <Badge
              variant={filterMode === "tagged" ? "default" : "outline"}
              className="cursor-pointer hover:bg-muted gap-1"
              onClick={() => setFilterMode("tagged")}
            >
              <Tag className="h-3 w-3" />
              Tagged ({taggedCount})
            </Badge>
          )}
        </div>
        <Button
          variant="outline"
          size="sm"
          className="h-7 text-xs"
          onClick={() => downloadCsv(displayedRecords, `${query || "powerpacks-results"}.csv`)}
        >
          <Download className="h-3 w-3 mr-1" />
          CSV
        </Button>
      </div>

      {filterMode === "tagged" && conversationTags.length > 0 && (
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-xs text-muted-foreground">Filter:</span>
          {conversationTags.map((tag) => {
            const active = selectedTagFilters.has(tag);
            return (
              <Badge
                key={tag}
                variant={active ? "default" : "outline"}
                className="cursor-pointer hover:bg-muted gap-1"
                onClick={() => toggleTagFilter(tag)}
              >
                <Tag className="h-3 w-3" />
                {tag}
              </Badge>
            );
          })}
          {selectedTagFilters.size > 0 && (
            <button
              className="text-xs text-muted-foreground hover:text-foreground underline-offset-2 hover:underline"
              onClick={() => setSelectedTagFilters(new Set())}
            >
              Clear filter
            </button>
          )}
        </div>
      )}

      <div className="w-full border border-border rounded-lg overflow-hidden">
        <Table className="table-fixed">
          <colgroup>
            <col className="w-[40%]" />
            <col className="w-[60%]" />
          </colgroup>
          <TableHeader>
            <TableRow className="border-b border-border">
              <TableHead className="border-r border-border w-[40%]">
                <div className="flex items-center gap-2">
                  {visibleTaggedCount > 0 && (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <button
                          className="text-muted-foreground hover:text-foreground transition-colors"
                          onClick={() => untagAll(displayedRecords.map((r) => r.personId).filter(Boolean) as string[])}
                        >
                          <CheckSquare className="h-3.5 w-3.5" />
                        </button>
                      </TooltipTrigger>
                      <TooltipContent side="bottom" className="text-xs">
                        Untag all on page
                      </TooltipContent>
                    </Tooltip>
                  )}
                  Person
                </div>
              </TableHead>
              <TableHead className="w-[60%]">Indicators</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {displayedRecords.map((record, index) => {
              const isNewRow = index >= prevRecordCount.current;
              const traitEntries = Object.entries(record.trait_scores || {})
                .sort(([, a], [, b]) => (b.score ?? 0) - (a.score ?? 0));

              return (
                <TableRow
                  key={`${record.personId || record.name}-${index}`}
                  className={`border-b border-border last:border-b-0 group${isNewRow ? " animate-fade-in-up" : ""}`}
                  style={isNewRow ? { animationDelay: `${(index - prevRecordCount.current) * 50}ms`, animationFillMode: "both" } : undefined}
                  data-testid={`person-row-${record.personId || "no-id"}`}
                  data-person-id={record.personId}
                  data-person-name={record.name}
                >
                  <TableCell
                    className="border-r border-border w-[40%] align-top pt-12 relative"
                    data-testid={`person-cell-${record.personId || "no-id"}`}
                  >
                    {record.personId && (() => {
                      const personTags = getTagsFor(record.personId);
                      const tagged = personTags.length > 0;
                      return (
                        <TagsPopover
                          tags={conversationTags}
                          appliedTags={personTags}
                          onToggle={(tag) => toggleTag(record.personId!, tag)}
                          onRemoveTag={(tag) => removeTag(tag)}
                        >
                          <button
                            className={cn(
                              "absolute top-2 right-2 inline-flex items-center gap-1 flex-wrap justify-end max-w-[70%] transition-opacity",
                              tagged ? "opacity-100" : "opacity-0 group-hover:opacity-100"
                            )}
                            onClick={(e) => e.stopPropagation()}
                            title={tagged ? "Edit tags" : "Add tag"}
                          >
                            {personTags.map((tag) => (
                              <span
                                key={tag}
                                className={cn(
                                  badgeVariants({ variant: "secondary" }),
                                  "gap-1 px-1.5 py-0 text-[10px] font-medium leading-none h-5"
                                )}
                              >
                                <Tag className="h-2.5 w-2.5" />
                                <span className="max-w-[100px] truncate">{tag}</span>
                              </span>
                            ))}
                            <Plus className="h-3.5 w-3.5 text-muted-foreground/30 hover:text-primary transition-colors" />
                          </button>
                        </TagsPopover>
                      );
                    })()}
                    <PersonInfoCell
                      name={record.name}
                      headline={record.headline}
                      location={record.location}
                      profilePictureUrl={record.profile_picture_url}
                      publicIdentifier={record.public_identifier}
                      xTwitterHandle={record.x_twitter_handle}
                      personId={record.personId}
                      linkedinUrl={record.linkedin_url}
                      avatarFallback="linkedin"
                    />
                  </TableCell>

                  <TableCell
                    className="w-[60%] align-top"
                    data-testid={`indicators-cell-${record.personId || "no-id"}`}
                    data-trait-count={traitEntries.length}
                  >
                    <div className="space-y-2 overflow-hidden p-2">
                      {(record.positions?.length > 0 || record.education?.length > 0 || (record.location && record.matched_profile_sections?.includes("location")) || record.summary) && (
                        <div className="flex justify-end">
                          <PersonDebugPopover
                            verticalSources={record.vertical_sources}
                            overallReasoning={record.overall_reasoning}
                            summary={record.summary}
                            location={record.location}
                            positions={record.positions}
                            education={record.education}
                            matchedPositionIndexes={record.matched_position_indexes}
                            matchedEducationIndexes={record.matched_education_indexes}
                            matchedProfileSections={record.matched_profile_sections}
                            personId={record.personId}
                            personName={record.name}
                          />
                        </div>
                      )}

                      {record.overall_trait_score != null && (
                        <div className="flex items-center gap-2 text-xs text-muted-foreground">
                          <Badge variant="secondary" className="gap-1">
                            <Sparkles className="h-3 w-3" />
                            Overall {Math.round(Math.max(0, Math.min(1, record.overall_trait_score)) * 100)}%
                          </Badge>
                          {record.overall_reasoning && <span className="line-clamp-2">{record.overall_reasoning}</span>}
                        </div>
                      )}

                      {traitEntries.length > 0 && (
                        <div className="space-y-2">
                          {traitEntries.map(([traitName, traitScore]) => (
                            <TraitScoreDisplay
                              key={traitName}
                              traitName={traitName}
                              traitScore={traitScore}
                            />
                          ))}
                        </div>
                      )}
                    </div>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
