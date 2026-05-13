/**
 * PersonDebugPopover — Debug/detail popover for search results.
 *
 * Shows: vertical sources, match reasoning, person summary,
 * work experience with descriptions, and education.
 *
 * Created: 2026-03-13
 */

import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { Briefcase, GraduationCap, MoreHorizontal, FileText, ChevronDown, Flag } from "lucide-react";
import { FeedbackPopover } from "@/components/feedback/FeedbackPopover";

export interface DebugPosition {
  position_title: string;
  company_name?: string;
  company_linkedin_url?: string;
  company_domain?: string;
  location?: string;
  start_date?: string | null;
  end_date?: string | null;
  is_current?: boolean;
  /** Role description — "description" from FE type or "dense_text" from API */
  description?: string;
  dense_text?: string;
  role_type?: string;
}

export interface DebugEducation {
  school_name: string;
  degree?: string | null;
  field_of_study?: string | null;
  /** Year-based (from hydrate API) */
  start_year?: number | null;
  end_year?: number | null;
  /** Date-based (from FE Education type) */
  start_date?: string | null;
  end_date?: string | null;
}

export interface PersonDebugPopoverProps {
  /** Vertical sources that matched this person (e.g. "role", "summary") */
  verticalSources?: string[];
  /** LLM's overall reasoning for why this person matched */
  overallReasoning?: string;
  /** Person's LinkedIn summary / about section */
  summary?: string;
  /** Work experience entries */
  positions?: DebugPosition[];
  /** Education entries */
  education?: DebugEducation[];
  /** Indexes of positions that matched the search query */
  matchedPositionIndexes?: number[];
  /** Which search verticals matched this person */
  verticalSources?: string[];
  /** Person ID for feedback */
  personId?: string;
  /** Person name for feedback context */
  personName?: string;
  /** Called when feedback is submitted */
  onFeedback?: (payload: import("@/components/feedback/FeedbackPopover").FeedbackPayload) => void;
}

const toTitleCase = (str: string | null | undefined) => {
  if (!str) return "";
  return str.replace(/\w\S*/g, (txt) =>
    txt.charAt(0).toUpperCase() + txt.substr(1).toLowerCase()
  );
};

const formatDate = (dateStr: string | null | undefined): string => {
  if (!dateStr) return "";
  try {
    const date = new Date(dateStr);
    return date.toLocaleDateString("en-US", { month: "short", year: "numeric" });
  } catch {
    return dateStr;
  }
};

function WorkExperienceItem({
  exp,
  isMatched,
  index,
}: {
  exp: DebugPosition;
  isMatched?: boolean;
  index: number;
}) {
  const descriptionText = exp.dense_text || exp.description || null;

  return (
    <div className="py-2 first:pt-0 last:pb-0">
      <div className="flex items-start justify-between gap-2">
        <div className="flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <p className="text-sm font-medium break-words">
              {toTitleCase(exp.position_title)}
            </p>
            {isMatched && (
              <Badge className="text-xs shrink-0 bg-green-500/20 text-green-600 dark:text-green-400 border-green-500/30">
                Matched
              </Badge>
            )}
          </div>
          {(() => {
            const companyUrl = exp.company_domain
              ? `https://${exp.company_domain}`
              : exp.company_linkedin_url;
            return companyUrl ? (
              <a
                href={companyUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="text-xs text-muted-foreground break-words hover:text-primary hover:underline"
              >
                {exp.company_name}
              </a>
            ) : (
              <p className="text-xs text-muted-foreground break-words">
                {exp.company_name}
              </p>
            );
          })()}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <span className="text-xs text-muted-foreground/50">#{index}</span>
          {exp.is_current && (
            <Badge variant="secondary" className="text-xs shrink-0">
              Current
            </Badge>
          )}
        </div>
      </div>
      {exp.location && (
        <p className="text-xs text-muted-foreground mt-0.5 break-words">
          {exp.location}
        </p>
      )}
      <p className="text-xs text-muted-foreground mt-0.5">
        {formatDate(exp.start_date)} –{" "}
        {exp.is_current ? "Present" : formatDate(exp.end_date)}
      </p>
      {descriptionText && (
        <p className="text-xs text-muted-foreground/80 mt-1.5 leading-relaxed line-clamp-4 whitespace-pre-line">
          {descriptionText}
        </p>
      )}
    </div>
  );
}

function EducationItem({ edu }: { edu: DebugEducation }) {
  // Support both year-based (hydrate API) and date-based (FE type) formats
  const startYear = edu.start_year ?? (edu.start_date ? new Date(edu.start_date).getFullYear() : null);
  const endYear = edu.end_year ?? (edu.end_date ? new Date(edu.end_date).getFullYear() : null);
  const dateStr = startYear && endYear
    ? `${startYear} – ${endYear}`
    : endYear
      ? `Graduated ${endYear}`
      : startYear
        ? `${startYear} – Present`
        : null;

  return (
    <div className="py-2 first:pt-0 last:pb-0">
      <div className="flex items-start justify-between gap-2">
        <div className="flex-1 min-w-0">
          <p className="text-sm font-medium break-words">{edu.school_name}</p>
          {(edu.degree || edu.field_of_study) && (
            <p className="text-xs text-muted-foreground break-words">
              {[edu.degree, edu.field_of_study].filter(Boolean).join(" in ")}
            </p>
          )}
        </div>
        {dateStr && (
          <span className="text-xs text-muted-foreground/70 shrink-0">{dateStr}</span>
        )}
      </div>
    </div>
  );
}

function CollapsibleSummary({ summary, isSummaryMatch }: { summary: string; isSummaryMatch?: boolean }) {
  const [expanded, setExpanded] = useState(false);
  // Rough check: if summary is short enough, don't bother with collapse
  const isLong = summary.length > 200 || summary.split("\n").length > 3;

  return (
    <div>
      <div className="flex items-center gap-1.5 mb-1.5">
        <FileText size={14} className="text-muted-foreground" />
        <span className="text-xs font-medium text-muted-foreground">About</span>
        {isSummaryMatch && (
          <Badge className="text-xs shrink-0 bg-green-500/20 text-green-600 dark:text-green-400 border-green-500/30">
            Matched
          </Badge>
        )}
      </div>
      <p className={`text-sm text-foreground/80 leading-relaxed whitespace-pre-line ${!expanded && isLong ? "line-clamp-3" : ""}`}>
        {summary}
      </p>
      {isLong && (
        <button
          onClick={() => setExpanded(!expanded)}
          className="text-xs text-muted-foreground hover:text-foreground mt-1 flex items-center gap-0.5 transition-colors"
        >
          {expanded ? "Show less" : "Show more"}
          <ChevronDown size={12} className={`transition-transform ${expanded ? "rotate-180" : ""}`} />
        </button>
      )}
    </div>
  );
}

export function PersonDebugPopover({
  verticalSources,
  overallReasoning,
  summary,
  positions,
  education,
  matchedPositionIndexes,
  personId,
  personName,
  onFeedback,
}: PersonDebugPopoverProps) {
  const hasContent =
    (positions && positions.length > 0) ||
    (education && education.length > 0) ||
    summary;

  if (!hasContent) return null;

  // Summary-only match: verticals === ['summary'] means no real position matched.
  // The pipeline defaults matched_position_indexes to [0] as a fallback — suppress it.
  const isSummaryOnly = verticalSources?.length === 1 && verticalSources[0] === "summary";
  const effectiveMatchedIndexes = isSummaryOnly ? [] : matchedPositionIndexes;

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="h-6 w-6 text-muted-foreground hover:text-foreground"
        >
          <MoreHorizontal size={14} />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        className="w-96 max-w-[90vw] p-0 bg-popover overflow-hidden"
        align="end"
        side="left"
      >
        <ScrollArea className="h-[500px]">
          <div className="p-4 space-y-4 overflow-hidden">
            {/* Feedback header */}
            {personId && onFeedback && (
              <div className="flex justify-end -mb-2">
                <FeedbackPopover
                  category="person"
                  entityId={personId}
                  fieldValue={personName || "Unknown"}
                  contextLabel={personName || "Person"}
                  onSubmit={onFeedback}
                  align="end"
                >
                  <button className="flex items-center gap-1.5 text-[11px] text-muted-foreground hover:text-foreground transition-colors">
                    <Flag className="h-3 w-3" />
                    Feedback
                  </button>
                </FeedbackPopover>
              </div>
            )}

            {/* Sources */}
            {verticalSources && verticalSources.length > 0 && (
              <div>
                <p className="text-xs font-medium text-muted-foreground mb-1.5">
                  Sources
                </p>
                <div className="flex items-center gap-1.5 flex-wrap">
                  {verticalSources.map((source) => (
                    <Badge
                      key={source}
                      variant="secondary"
                      className="capitalize text-xs"
                    >
                      {source}
                    </Badge>
                  ))}
                </div>
              </div>
            )}

            {/* Overall Reasoning */}
            {overallReasoning && (
              <div className="bg-muted/50 rounded-md p-3">
                <p className="text-xs font-medium text-muted-foreground mb-1">
                  Why they match
                </p>
                <p className="text-sm text-foreground">{overallReasoning}</p>
              </div>
            )}

            {/* Person Summary — collapsible, 3 lines by default */}
            {summary && (
              <CollapsibleSummary
                summary={summary}
                isSummaryMatch={verticalSources?.includes("summary")}
              />
            )}

            {/* Work Experience */}
            {positions && positions.length > 0 && (
              <div>
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-1.5">
                    <Briefcase size={14} className="text-muted-foreground" />
                    <span className="text-xs font-medium text-muted-foreground">
                      Work Experience
                    </span>
                  </div>
                  {effectiveMatchedIndexes &&
                    effectiveMatchedIndexes.length > 0 && (
                      <span className="text-xs text-muted-foreground/70">
                        matched: [{effectiveMatchedIndexes.join(", ")}]
                      </span>
                    )}
                </div>
                <div className="divide-y divide-border">
                  {positions.map((exp, idx) => (
                    <WorkExperienceItem
                      key={idx}
                      exp={exp}
                      index={idx}
                      isMatched={effectiveMatchedIndexes?.includes(idx)}
                    />
                  ))}
                </div>
              </div>
            )}

            {/* Education */}
            {education && education.length > 0 && (
              <>
                {positions && positions.length > 0 && <Separator />}
                <div>
                  <div className="flex items-center gap-1.5 mb-2">
                    <GraduationCap
                      size={14}
                      className="text-muted-foreground"
                    />
                    <span className="text-xs font-medium text-muted-foreground">
                      Education
                    </span>
                  </div>
                  <div className="divide-y divide-border">
                    {education.map((edu, idx) => (
                      <EducationItem key={idx} edu={edu} />
                    ))}
                  </div>
                </div>
              </>
            )}
          </div>
        </ScrollArea>
      </PopoverContent>
    </Popover>
  );
}

export default PersonDebugPopover;
