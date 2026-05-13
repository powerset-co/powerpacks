import { useState, useRef, useEffect } from "react";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Briefcase, Building2, Info, ChevronDown, Linkedin } from "lucide-react";
import { SocialLinks } from "@/components/ui/social-link";
import { PersonSourceBadges, type PersonSource } from "./PersonSourceBadges";
import type { OperatorInfo } from "@/hooks/usePersonOperators";
import {
  HoverCard,
  HoverCardContent,
  HoverCardTrigger,
} from "@/components/ui/hover-card";
import { cn } from "@/lib/utils";
import { SOURCE_CONFIG } from "./PersonSourceBadges";
import { WorkExperience } from "@/types/search";
import { Badge } from "@/components/ui/badge";

interface PersonInfoCellProps {
  name?: string | null;
  headline?: string | null;
  location?: string | null;
  profilePictureUrl?: string | null;
  linkedinUrl?: string | null;
  /** For DatabaseRecord compatibility - constructs LinkedIn URL from public_identifier */
  publicIdentifier?: string | null;
  /** Optional max length for headline truncation */
  headlineMaxLength?: number;
  /** Optional className for the container */
  className?: string;
  /** Size variant */
  size?: "default" | "compact";
  /** Source channels for the person */
  sources?: PersonSource[];
  /** Operator network info — rendered inline with sources */
  operators?: OperatorInfo[];
  /** For lazy-loading source identifiers on hover */
  setId?: string | null;
  /** For lazy-loading source identifiers on hover */
  personId?: string | null;
  /** X/Twitter handle */
  xTwitterHandle?: string | null;
  /** LinkedIn stats */
  linkedinFollowers?: number | null;
  linkedinConnections?: number | null;
  /** Callback when the person name is clicked (e.g. navigate to detail page) */
  onNameClick?: () => void;
  /** Position history from hydrated context */
  positions?: WorkExperience[];
  /** Optional fallback avatar style when no profile image is available */
  avatarFallback?: "initials" | "linkedin";
  /** Indexes into positions array that matched the search query */
  matchedPositionIndexes?: number[];
  /**
   * How to display position data (requires positions prop):
   * - "structured": Replace headline with "Title · Company", show matched past roles below
   * - "badges": Keep headline, add matched position badges below location
   * - "inline": Compact "Title @ Company" replacing headline, matched as second line
   */
  positionDisplay?: "structured" | "badges" | "inline";
}

const getInitials = (name?: string | null): string => {
  if (!name) return "?";
  const parts = name.split(" ").filter(Boolean);
  if (parts.length === 0) return "?";
  const first = parts[0]?.charAt(0) || "";
  const last = parts[parts.length - 1]?.charAt(0) || "";
  return (first + last).toUpperCase();
};

const truncateText = (text: string | null | undefined, maxLength: number = 80): string => {
  if (!text) return "";
  if (text.length <= maxLength) return text;
  return text.substring(0, maxLength) + "...";
};

// Deterministic color from operator name
const AVATAR_COLORS = [
  "bg-blue-500/20 text-blue-600 dark:text-blue-400",
  "bg-purple-500/20 text-purple-600 dark:text-purple-400",
  "bg-emerald-500/20 text-emerald-600 dark:text-emerald-400",
  "bg-orange-500/20 text-orange-600 dark:text-orange-400",
  "bg-pink-500/20 text-pink-600 dark:text-pink-400",
];

const getAvatarColor = (name: string): string => {
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = name.charCodeAt(i) + ((hash << 5) - hash);
  return AVATAR_COLORS[Math.abs(hash) % AVATAR_COLORS.length];
};

/**
 * Reusable component for displaying person info with avatar, name, LinkedIn link, and headline.
 * Used in DatabaseRecordTable and agentic search results.
 */
export function PersonInfoCell({
  name,
  headline,
  location,
  profilePictureUrl,
  linkedinUrl,
  publicIdentifier,
  headlineMaxLength = 80,
  className = "",
  size = "default",
  sources,
  operators,
  setId,
  personId,
  onNameClick,
  xTwitterHandle,
  positions,
  matchedPositionIndexes,
  positionDisplay,
  avatarFallback = "initials",
}: PersonInfoCellProps) {
  // Construct LinkedIn URL from publicIdentifier if linkedinUrl not provided
  // Skip synthetic profiles — they don't have real LinkedIn pages
  const isSynthetic = publicIdentifier?.startsWith("synth-");
  const finalLinkedinUrl = linkedinUrl || (publicIdentifier && !isSynthetic ? `https://www.linkedin.com/in/${publicIdentifier}/` : null);

  const avatarSize = size === "compact" ? "h-8 w-8" : "h-10 w-10";
  const linkedinSize = size === "compact" ? 14 : 16;
  const hasSources = sources && sources.length > 0;
  const hasOperators = operators && operators.length > 0;
  return (
    <div className={`flex items-start gap-3 ${className}`}>
      <div
        className={`flex-shrink-0 ${onNameClick ? "cursor-pointer" : finalLinkedinUrl ? "cursor-pointer" : "cursor-default"}`}
        onClick={onNameClick || undefined}
      >
        <Avatar className={`${avatarSize} mt-0.5`}>
          <AvatarImage src={profilePictureUrl || undefined} />
          <AvatarFallback>
            {avatarFallback === "linkedin" && finalLinkedinUrl ? (
              <Linkedin className="h-5 w-5 text-[#0A66C2]" />
            ) : (
              getInitials(name)
            )}
          </AvatarFallback>
        </Avatar>
      </div>
      <div className="min-w-0 flex-1">
        <div className="font-medium truncate flex items-center gap-2">
          <span
            className={cn(
              size === "compact" ? "text-sm" : "",
              onNameClick && "cursor-pointer hover:text-primary hover:underline transition-colors"
            )}
            onClick={onNameClick}
          >
            {name || "Unknown"}
          </span>
          <SocialLinks linkedinUrl={finalLinkedinUrl} xTwitterHandle={xTwitterHandle} size={linkedinSize} />
        </div>
        {/* Position display variants */}
        {positionDisplay === "structured" && positions && positions.length > 0 ? (
          <StructuredPositionDisplay
            positions={positions}
            matchedIndexes={matchedPositionIndexes}
            location={location}
          />
        ) : positionDisplay === "badges" && positions && positions.length > 0 ? (
          <BadgesPositionDisplay
            headline={headline}
            headlineMaxLength={headlineMaxLength}
            positions={positions}
            matchedIndexes={matchedPositionIndexes}
            location={location}
          />
        ) : positionDisplay === "inline" && positions && positions.length > 0 ? (
          <InlinePositionDisplay
            positions={positions}
            matchedIndexes={matchedPositionIndexes}
            location={location}
          />
        ) : (
          <>
            {headline && (
              <div className="text-sm text-muted-foreground">
                {headline}
              </div>
            )}
            {location && (
              <div className="text-xs text-muted-foreground/70 truncate">
                {location}
              </div>
            )}
          </>
        )}

        {/* Inline compact: source badges + operator mini-avatars */}
        {(hasSources || hasOperators) && (
          <div className="mt-1.5 flex items-center gap-2">
            {hasSources && (
              <PersonSourceBadges sources={sources} compact setId={setId} personId={personId} />
            )}

            {hasSources && hasOperators && (
              <span className="text-muted-foreground/30">•</span>
            )}

            {hasOperators && (
              <HoverCard openDelay={200} closeDelay={100}>
                <HoverCardTrigger asChild>
                  <div className="flex items-center cursor-pointer">
                    {operators.slice(0, 3).map((op, idx) => (
                      <Avatar key={op.operator_id}
                        className={cn(
                          "h-6 w-6 text-[10px] border border-background",
                          idx > 0 && "-ml-1.5",
                          getAvatarColor(op.operator_name),
                        )}>
                        <AvatarFallback className={getAvatarColor(op.operator_name)}>
                          {getInitials(op.operator_name)}
                        </AvatarFallback>
                      </Avatar>
                    ))}
                    {operators.length > 3 && (
                      <span className="text-[10px] text-muted-foreground ml-1">+{operators.length - 3}</span>
                    )}
                  </div>
                </HoverCardTrigger>
                <HoverCardContent className="w-72 p-3" align="start">
                  <OperatorTooltip operators={operators} />
                </HoverCardContent>
              </HoverCard>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/* ─── Position Display Variants ─────────────────────────────────── */

/** Variant A: "Structured" — Title · Company as primary, matched past roles below */
function StructuredPositionDisplay({
  positions,
  matchedIndexes,
  location,
}: {
  positions: WorkExperience[];
  matchedIndexes?: number[];
  location?: string | null;
}) {
  const current = positions.find(p => p.is_current);
  const matchedPast = (matchedIndexes || [])
    .map(i => positions[i])
    .filter(p => p && !p.is_current);

  return (
    <>
      {current ? (
        <div className="text-sm text-muted-foreground truncate flex items-center gap-1.5">
          <span className="font-medium text-foreground/80">{current.position_title}</span>
          <span className="text-muted-foreground/50">·</span>
          <span>{current.company_name}</span>
        </div>
      ) : positions[0] && (
        <div className="text-sm text-muted-foreground truncate">
          {positions[0].position_title} · {positions[0].company_name}
        </div>
      )}
      {location && (
        <div className="text-xs text-muted-foreground/70 truncate">{location}</div>
      )}
      {matchedPast.length > 0 && (
        <div className="text-xs text-muted-foreground/60 truncate mt-0.5 flex items-center gap-1">
          <Briefcase className="h-3 w-3 flex-shrink-0" />
          <span>
            {matchedPast.map(p => `${p.position_title} @ ${p.company_name}`).join(" · ")}
          </span>
        </div>
      )}
    </>
  );
}

/** Format a date string like "2024-10-01T00:00:00Z" to "Oct 2024" */
function formatPositionDate(date: string | null | undefined): string {
  if (!date) return "";
  const d = new Date(date);
  return d.toLocaleDateString("en-US", { month: "short", year: "numeric" });
}

/** Variant B: "Badges" — Headline, then vertical list of recent positions (max 3), matched in green */
function BadgesPositionDisplay({
  headline,
  headlineMaxLength,
  positions,
  matchedIndexes,
  location,
}: {
  headline?: string | null;
  headlineMaxLength?: number;
  positions: WorkExperience[];
  matchedIndexes?: number[];
  location?: string | null;
}) {
  const matchedSet = new Set(matchedIndexes || []);
  // Sort: matched positions first, then non-matched, preserving relative order within each group
  const sortedPositions = [...positions].sort((a, b) => {
    const aMatched = matchedSet.has(positions.indexOf(a));
    const bMatched = matchedSet.has(positions.indexOf(b));
    if (aMatched && !bMatched) return -1;
    if (!aMatched && bMatched) return 1;
    return 0;
  });
  const MAX_VISIBLE_PX = 120;
  const scrollRef = useRef<HTMLDivElement>(null);
  const [showScrollDown, setShowScrollDown] = useState(false);
  const [showScrollUp, setShowScrollUp] = useState(false);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const update = () => {
      const hasOverflow = el.scrollHeight > el.clientHeight + 4;
      const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 4;
      const atTop = el.scrollTop <= 4;
      setShowScrollDown(hasOverflow && !atBottom);
      setShowScrollUp(hasOverflow && !atTop);
    };
    update();
    el.addEventListener("scroll", update, { passive: true });
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => { el.removeEventListener("scroll", update); observer.disconnect(); };
  }, [positions]);

  return (
    <>
      {headline && (
        <div className="text-sm text-muted-foreground truncate">
          {truncateText(headline, headlineMaxLength)}
        </div>
      )}
      {location && (
        <div className="text-xs text-muted-foreground/70 truncate">{location}</div>
      )}
      {sortedPositions.length > 0 && (
        <div className="relative mt-1">
          <div
            ref={scrollRef}
            className="flex flex-col gap-1 overflow-y-auto scrollbar-thin"
            style={{ maxHeight: `${MAX_VISIBLE_PX}px` }}
          >
          {showScrollUp && (
            <div className="flex justify-center sticky top-0 z-10">
              <ChevronDown className="h-4 w-4 text-muted-foreground rotate-180" />
            </div>
          )}
          {sortedPositions.map((p, i) => {
            const isMatched = matchedSet.has(positions.indexOf(p));
            const dateRange = p.is_current
              ? `${formatPositionDate(p.start_date)} – Present`
              : `${formatPositionDate(p.start_date)} – ${formatPositionDate(p.end_date)}`;
            const desc = p.description || null;

            // Matched positions: description inside the badge, hover for full text if truncated
            if (isMatched) {
              const isTruncated = desc && desc.length > 120;
              const matchedBadge = (
                <div
                  className={cn(
                    "text-[10px] py-0.5 px-1.5 rounded-sm border font-normal border-emerald-500/30 text-emerald-600 dark:text-emerald-400 bg-emerald-500/5",
                    desc ? "flex flex-col gap-0.5" : "inline-flex items-center gap-1"
                  )}
                >
                  <div className="inline-flex items-center gap-1">
                    <Building2 className="h-2.5 w-2.5 flex-shrink-0" />
                    <span className="truncate">{p.position_title} · {p.company_name}</span>
                    <span className="text-muted-foreground/50 flex-shrink-0">{dateRange}</span>
                  </div>
                  {desc && (
                    <p className="text-[10px] text-emerald-600/60 dark:text-emerald-400/60 line-clamp-2 ml-3.5">
                      {desc}
                    </p>
                  )}
                </div>
              );

              if (isTruncated) {
                return (
                  <HoverCard key={i} openDelay={0} closeDelay={100}>
                    <HoverCardTrigger asChild>
                      <div className="cursor-default w-fit">{matchedBadge}</div>
                    </HoverCardTrigger>
                    <HoverCardContent side="right" align="start" className="max-w-sm text-xs p-3">
                      <p className="font-medium mb-1">{p.position_title} at {p.company_name}</p>
                      <p className="text-muted-foreground">{desc}</p>
                    </HoverCardContent>
                  </HoverCard>
                );
              }

              return <div key={i} className="w-fit">{matchedBadge}</div>;
            }

            // Non-matched: hover for description
            const badge = (
              <div
                className="text-[10px] py-0.5 px-1.5 rounded-sm border inline-flex items-center gap-1 font-normal border-muted-foreground/20 text-muted-foreground"
              >
                <Building2 className="h-2.5 w-2.5 flex-shrink-0" />
                <span className="truncate">{p.position_title} · {p.company_name}</span>
                <span className="text-muted-foreground/50 flex-shrink-0">{dateRange}</span>
                {desc && <Info className="h-2.5 w-2.5 flex-shrink-0 opacity-40" />}
              </div>
            );

            if (desc) {
              return (
                <HoverCard key={i} openDelay={0} closeDelay={100}>
                  <HoverCardTrigger asChild>
                    <div className="cursor-default w-fit">{badge}</div>
                  </HoverCardTrigger>
                  <HoverCardContent side="right" align="start" className="max-w-sm text-xs p-3">
                    <p className="font-medium mb-1">{p.position_title} at {p.company_name}</p>
                    <p className="text-muted-foreground">{truncateText(desc, 300)}</p>
                  </HoverCardContent>
                </HoverCard>
              );
            }

            return <div key={i} className="w-fit">{badge}</div>;
          })}
          {showScrollDown && (
            <div className="flex justify-center sticky bottom-0 z-10">
              <ChevronDown className="h-4 w-4 text-muted-foreground animate-bounce" />
            </div>
          )}
          </div>
        </div>
      )}
    </>
  );
}

/** Variant C: "Inline" — Compact Title @ Company, matched as muted second line */
function InlinePositionDisplay({
  positions,
  matchedIndexes,
  location,
}: {
  positions: WorkExperience[];
  matchedIndexes?: number[];
  location?: string | null;
}) {
  const current = positions.find(p => p.is_current);
  const matchedPast = (matchedIndexes || [])
    .map(i => positions[i])
    .filter(p => p && !p.is_current);

  return (
    <>
      {current ? (
        <div className="text-sm text-muted-foreground truncate">
          {current.position_title}
          <span className="text-muted-foreground/50"> @ </span>
          {current.company_name}
          <span className="ml-1.5 text-[10px] text-emerald-500 font-medium">● current</span>
        </div>
      ) : positions[0] && (
        <div className="text-sm text-muted-foreground truncate">
          {positions[0].position_title} @ {positions[0].company_name}
        </div>
      )}
      {location && (
        <div className="text-xs text-muted-foreground/70 truncate">{location}</div>
      )}
      {matchedPast.length > 0 && (
        <div className="text-xs text-muted-foreground/50 truncate mt-0.5">
          Previously: {matchedPast.map(p => `${p.position_title} @ ${p.company_name}`).join(", ")}
        </div>
      )}
    </>
  );
}

/** Hover tooltip showing operator details with source channels */
function OperatorTooltip({ operators }: { operators: OperatorInfo[] }) {
  return (
    <div className="space-y-2.5 p-1">
      {operators.map((op) => (
        <div key={op.operator_id} className="flex items-center gap-2.5">
          <Avatar className={cn("h-6 w-6 text-xs border", getAvatarColor(op.operator_name))}>
            <AvatarFallback className={getAvatarColor(op.operator_name)}>
              {getInitials(op.operator_name)}
            </AvatarFallback>
          </Avatar>
            <div className="flex-1 min-w-0">
              <div className="text-sm font-medium truncate">{op.operator_name}</div>
            <div className="flex flex-wrap items-center gap-1.5 mt-0.5 max-w-full">
              {[...new Set((op.source_channels || []).map((channel) =>
                channel === "whatsapp" || channel === "imessage" || channel === "phone" ? "messages" : channel
              ))].map((channel) => {
                const config = SOURCE_CONFIG[channel];
                if (!config) {
                  return (
                    <span key={channel} className="text-xs text-muted-foreground max-w-full break-all">
                      {channel}
                    </span>
                  );
                }
                const Icon = config.icon;
                return (
                  <span
                    key={channel}
                    className={cn(
                      "inline-flex items-center gap-1 text-xs px-1.5 py-0.5 rounded-sm border max-w-full",
                      config.color
                    )}
                  >
                    <Icon className="h-3 w-3" />
                    <span className="truncate">{config.label}</span>
                  </span>
                );
              })}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

export default PersonInfoCell;
