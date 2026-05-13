import { useState, useCallback, useMemo } from "react";
import { Badge } from "@/components/ui/badge";
import { Linkedin, Mail, FileSpreadsheet, MessageCircle, Link2, Users, Loader2 } from "lucide-react";
import { XIcon } from "@/components/icons/XIcon";
import { cn } from "@/lib/utils";
import {
  HoverCard,
  HoverCardContent,
  HoverCardTrigger,
} from "@/components/ui/hover-card";
import { fetchSourceIdentifiers, SourceIdentifier } from "@/hooks/usePersonAttribution";

export interface PersonSource {
  source_channel: string;
  total_interactions?: number;
  source_identifier?: string;
  source_accounts?: string[];  // Gmail accounts that sourced this (effective operator only)
  /** Per-account interaction breakdown — YOUR email accounts + counts (from gmail_account_details) */
  account_details?: { email: string; interactions: number }[];
}

interface PersonSourceBadgesProps {
  sources: PersonSource[];
  className?: string;
  /** Show compact badges without labels */
  compact?: boolean;
  /** Required for lazy-loading source identifiers on hover */
  setId?: string | null;
  /** Required for lazy-loading source identifiers on hover */
  personId?: string | null;
}

const formatMessageCount = (count: number): string => {
  if (count >= 1000) {
    return `~${Math.round(count / 1000)}k`;
  }
  return count.toString();
};

const MESSAGE_CHANNELS = new Set(["whatsapp", "imessage", "phone"]);
const EMAIL_CHANNELS = new Set(["gmail", "email"]);

export const SOURCE_CONFIG: Record<string, {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  color: string;
  showMessageCount: boolean;
}> = {
  email: {
    icon: Mail,
    label: "Email",
    color: "bg-green-500/20 text-green-400 border-green-500/30",
    showMessageCount: true,
  },
  gmail: {
    icon: Mail,
    label: "Email",
    color: "bg-green-500/20 text-green-400 border-green-500/30",
    showMessageCount: true,
  },
  linkedin: {
    icon: Linkedin,
    label: "LinkedIn",
    color: "bg-blue-500/20 text-blue-400 border-blue-500/30",
    showMessageCount: false,
  },
  csv_import: {
    icon: FileSpreadsheet,
    label: "Contacts Export",
    color: "bg-orange-500/20 text-orange-400 border-orange-500/30",
    showMessageCount: false,
  },
  messages: {
    icon: MessageCircle,
    label: "Messages",
    color: "bg-emerald-500/20 text-emerald-400 border-emerald-500/30",
    showMessageCount: true,
  },
  whatsapp: {
    icon: MessageCircle,
    label: "WhatsApp",
    color: "bg-emerald-500/20 text-emerald-400 border-emerald-500/30",
    showMessageCount: true,
  },
  x: {
    icon: XIcon,
    label: "X",
    color: "bg-neutral-100 text-neutral-900 border-neutral-300 dark:bg-neutral-800 dark:text-neutral-100 dark:border-neutral-600",
    showMessageCount: false,
  },
  twitter: {
    icon: XIcon,
    label: "X",
    color: "bg-neutral-100 text-neutral-900 border-neutral-300 dark:bg-neutral-800 dark:text-neutral-100 dark:border-neutral-600",
    showMessageCount: false,
  },
  imessage: {
    icon: MessageCircle,
    label: "iMessage",
    color: "bg-sky-500/20 text-sky-400 border-sky-500/30",
    showMessageCount: true,
  },
  linkedin_connections: {
    icon: Users,
    label: "Connections",
    color: "bg-indigo-500/20 text-indigo-400 border-indigo-500/30",
    showMessageCount: false,
  },
};

interface AggregatedSource {
  source_channel: string;
  total_interactions: number;
  identifiers: { identifier: string; interactions: number }[];
  source_accounts: string[];  // Gmail accounts that sourced this
  account_details: { email: string; interactions: number }[];  // Per-account breakdown
  channel_breakdown?: Record<string, number>;  // For merged channels (messages)
}

/**
 * Displays source channel badges for a person (gmail, linkedin, csv, etc.)
 * Shows message count for gmail/whatsapp/imessage channels
 * Gmail/CSV badges lazy-load identifiers (emails) on hover via API
 */
export function PersonSourceBadges({ sources, className, compact = false, setId, personId }: PersonSourceBadgesProps) {
  if (!sources || sources.length === 0) return null;

  // Deduplicate and aggregate by source_channel
  const aggregatedSources = sources.reduce((acc, source) => {
    const normalizedChannel = MESSAGE_CHANNELS.has(source.source_channel)
      ? "messages"
      : EMAIL_CHANNELS.has(source.source_channel)
        ? "gmail"
        : source.source_channel;
    const existing = acc.find(s => s.source_channel === normalizedChannel);
    if (existing) {
      existing.total_interactions += source.total_interactions || 0;
      if (source.source_identifier) {
        existing.identifiers.push({
          identifier: source.source_identifier,
          interactions: source.total_interactions || 0,
        });
      }
      // Merge source_accounts (deduplicate)
      if (source.source_accounts) {
        for (const acct of source.source_accounts) {
          if (!existing.source_accounts.includes(acct)) {
            existing.source_accounts.push(acct);
          }
        }
      }
      // Merge account_details (deduplicate by email, sum interactions)
      if (source.account_details) {
        for (const detail of source.account_details) {
          const existingDetail = existing.account_details.find(d => d.email === detail.email);
          if (existingDetail) {
            existingDetail.interactions += detail.interactions;
          } else {
            existing.account_details.push({ ...detail });
          }
        }
      }
      if (normalizedChannel === "messages") {
        existing.channel_breakdown = existing.channel_breakdown || {};
        existing.channel_breakdown[source.source_channel] =
          (existing.channel_breakdown[source.source_channel] || 0) +
          (source.total_interactions || 0);
      }
    } else {
      acc.push({
        source_channel: normalizedChannel,
        total_interactions: source.total_interactions || 0,
        identifiers: source.source_identifier
          ? [{ identifier: source.source_identifier, interactions: source.total_interactions || 0 }]
          : [],
        source_accounts: source.source_accounts ? [...source.source_accounts] : [],
        account_details: source.account_details ? source.account_details.map(d => ({ ...d })) : [],
        channel_breakdown:
          normalizedChannel === "messages"
            ? { [source.source_channel]: source.total_interactions || 0 }
            : undefined,
      });
    }
    return acc;
  }, [] as AggregatedSource[]);

  // Sort by total_interactions descending — most messages first (leftmost)
  aggregatedSources.sort((a, b) => b.total_interactions - a.total_interactions);

  // Can we lazy-load? Need both setId and personId
  const canLazyLoad = !!(setId && personId);

  return (
    <div className={cn("flex flex-wrap gap-1.5", className)}>
      {aggregatedSources.map((source) => {
        const config = SOURCE_CONFIG[source.source_channel];
        if (!config) return null;

        const Icon = config.icon;
        const showCount = config.showMessageCount && source.total_interactions > 0;
        const isEmailChannel = EMAIL_CHANNELS.has(source.source_channel);
        const isCsvChannel = source.source_channel === "csv_import";
        const isMessagesChannel = source.source_channel === "messages";
        const isLazyLoadChannel = isEmailChannel || isCsvChannel;

        // For gmail/csv_import: show HoverCard with lazy-loaded identifiers
        if (isLazyLoadChannel && canLazyLoad) {
          return (
            <LazySourceBadge
              key={source.source_channel}
              source={source}
              config={config}
              compact={compact}
              setId={setId!}
              personId={personId!}
            />
          );
        }

        if (isMessagesChannel) {
          return (
            <MessagesSourceBadge
              key={source.source_channel}
              source={source}
              config={config}
              compact={compact}
              setId={setId}
              personId={personId}
            />
          );
        }

        // For other channels (or if lazy-load not available), show simple badge
        return (
          <Badge
            key={source.source_channel}
            variant="outline"
            className={cn(
              "flex items-center gap-1.5 px-2 py-1 text-xs font-normal border",
              config.color
            )}
          >
            <Icon className="h-3.5 w-3.5" />
            {!compact && <span>{config.label}</span>}
            {showCount && (
              <span className="font-medium">
                {formatMessageCount(source.total_interactions)}
              </span>
            )}
          </Badge>
        );
      })}
    </div>
  );
}

/**
 * HoverCard badge for gmail/csv_import channels.
 *
 * Gmail: shows YOUR email accounts + per-account interaction counts
 * (from account_details, no lazy-load needed). Falls back to lazy-loading
 * source identifiers if account_details is empty.
 *
 * CSV: lazy-loads imported records on hover.
 */
function LazySourceBadge({
  source,
  config,
  compact,
  setId,
  personId,
}: {
  source: AggregatedSource;
  config: (typeof SOURCE_CONFIG)[string];
  compact: boolean;
  setId: string;
  personId: string;
}) {
  const [identifiers, setIdentifiers] = useState<SourceIdentifier[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [hasFetched, setHasFetched] = useState(false);

  const Icon = config.icon;
  const showCount = config.showMessageCount && source.total_interactions > 0;
  const isEmailChannel = EMAIL_CHANNELS.has(source.source_channel);
  const hasAccountDetails = isEmailChannel && source.account_details.length > 0;

  const handleOpenChange = useCallback(
    async (open: boolean) => {
      if (!open || hasFetched) return;

      setIsLoading(true);
      try {
        const results = await fetchSourceIdentifiers(setId, personId, source.source_channel);
        setIdentifiers(results);
      } catch (err) {
        console.error("Failed to fetch source identifiers:", err);
      } finally {
        setIsLoading(false);
        setHasFetched(true);
      }
    },
    [setId, personId, source.source_channel, hasFetched]
  );

  const accountEmails = useMemo(
    () => new Set(source.account_details.map((acct) => acct.email.toLowerCase())),
    [source.account_details]
  );

  const identifiersByAccount = useMemo(() => {
    const groups = new Map<string, SourceIdentifier[]>();
    for (const item of identifiers) {
      const account = item.source_account?.toLowerCase();
      if (!account) continue;
      const group = groups.get(account) ?? [];
      group.push(item);
      groups.set(account, group);
    }
    return groups;
  }, [identifiers]);

  const groupedIdentifierValues = useMemo(() => {
    const values = new Set<string>();
    for (const account of accountEmails) {
      for (const item of identifiersByAccount.get(account) ?? []) {
        values.add(item.identifier.toLowerCase());
      }
    }
    return values;
  }, [accountEmails, identifiersByAccount]);

  const ungroupedIdentifiers = useMemo(
    () => identifiers.filter((item) => {
      const account = item.source_account?.toLowerCase();
      return (!account || !accountEmails.has(account)) &&
        !groupedIdentifierValues.has(item.identifier.toLowerCase());
    }),
    [identifiers, accountEmails, groupedIdentifierValues]
  );

  const headerLabel = hasAccountDetails
    ? `${source.account_details.length} Email Account${source.account_details.length !== 1 ? "s" : ""}`
    : isEmailChannel
      ? "Linked Emails"
      : identifiers.length > 0
        ? `${identifiers.length} Imported Record${identifiers.length !== 1 ? "s" : ""}`
        : "Imported Records";

  return (
    <HoverCard openDelay={100} closeDelay={200} onOpenChange={handleOpenChange}>
      <HoverCardTrigger asChild>
        <span className="inline-flex">
          <Badge
            variant="outline"
            className={cn(
              "flex items-center gap-1.5 px-2 py-1 text-xs font-normal border cursor-pointer hover:opacity-80 transition-opacity",
              config.color
            )}
          >
            <Icon className="h-3.5 w-3.5" />
            {!compact && <span>{config.label}</span>}
            {showCount && (
              <span className="font-medium">
                {formatMessageCount(source.total_interactions)}
              </span>
            )}
          </Badge>
        </span>
      </HoverCardTrigger>
      <HoverCardContent className="w-80 p-0" align="start">
        <div className="p-3 border-b">
          <div className="flex items-center gap-2">
            <Icon className={cn("h-4 w-4", isEmailChannel ? "text-green-400" : "text-orange-400")} />
            <span className="font-medium text-sm">{headerLabel}</span>
            {isEmailChannel && source.total_interactions > 0 && (
              <span className="text-xs text-muted-foreground ml-auto">
                {formatMessageCount(source.total_interactions)} total
              </span>
            )}
          </div>
        </div>
        <div className="max-h-64 overflow-y-auto">
          {/* Gmail with account_details: show YOUR accounts + per-account counts */}
          {hasAccountDetails &&
            source.account_details.map((acct, idx) => {
              const nested = identifiersByAccount.get(acct.email.toLowerCase()) ?? [];
              return (
                <div key={idx} className="border-b last:border-b-0">
                  <div className="flex items-center justify-between px-3 py-2 hover:bg-muted/50">
                    <div className="flex items-center gap-2 min-w-0 flex-1 mr-2">
                      <Mail className="h-3.5 w-3.5 text-green-400 shrink-0" />
                      <span className="text-sm truncate">{acct.email}</span>
                    </div>
                    {acct.interactions > 0 && (
                      <span className="text-[10px] font-medium tabular-nums shrink-0">
                        {formatMessageCount(acct.interactions)} msgs
                      </span>
                    )}
                  </div>
                  {nested.map((item, nestedIdx) => (
                    <div
                      key={`${item.identifier}-${nestedIdx}`}
                      className="px-3 pb-2 pl-9 text-xs text-muted-foreground"
                    >
                      <span className="truncate">{item.identifier}</span>
                    </div>
                  ))}
                </div>
              );
            })}
          {/* Fallback: lazy-loaded identifiers (CSV or Gmail without account_details) */}
          {isLoading && (
            <div className="flex items-center justify-center py-4">
              <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
              <span className="ml-2 text-xs text-muted-foreground">Loading...</span>
            </div>
          )}
          {!isLoading && identifiers.length === 0 && hasFetched && !hasAccountDetails && (
            <div className="px-3 py-3 text-xs text-muted-foreground">
              No identifiers found
            </div>
          )}
          {!isLoading &&
            (hasAccountDetails ? ungroupedIdentifiers : identifiers).map((item, idx) => (
              <div
                key={idx}
                className="flex items-center justify-between px-3 py-2 hover:bg-muted/50 border-b last:border-b-0"
              >
                <span className="text-sm truncate flex-1 mr-2">
                  {item.identifier}
                </span>
                <div className="flex items-center gap-2 shrink-0">
                  {item.total_interactions > 0 && (
                    <span className="text-[10px] font-medium tabular-nums">
                      {formatMessageCount(item.total_interactions)} msgs
                    </span>
                  )}
                  {item.operator_name && (
                    <span className="text-[10px] text-muted-foreground">
                      via {item.operator_name}
                    </span>
                  )}
                </div>
              </div>
            ))}
        </div>
      </HoverCardContent>
    </HoverCard>
  );
}

/**
 * HoverCard badge for merged "Messages" channel.
 *
 * Shows combined message count on the badge.
 * On hover, lazy-loads per-operator interaction counts (across WhatsApp + iMessage)
 * and displays operators ordered by message volume.
 */
function MessagesSourceBadge({
  source,
  config,
  compact,
  setId,
  personId,
}: {
  source: AggregatedSource;
  config: (typeof SOURCE_CONFIG)[string];
  compact: boolean;
  setId?: string | null;
  personId?: string | null;
}) {
  const [operatorCounts, setOperatorCounts] = useState<
    { operator: string; interactions: number }[]
  >([]);
  const [isLoading, setIsLoading] = useState(false);
  const [hasFetched, setHasFetched] = useState(false);

  const Icon = config.icon;
  const showCount = config.showMessageCount && source.total_interactions > 0;
  const canLazyLoad = !!(setId && personId);

  const handleOpenChange = useCallback(
    async (open: boolean) => {
      if (!open || hasFetched || !canLazyLoad) return;

      const channels = Object.keys(source.channel_breakdown || {}).filter((ch) =>
        MESSAGE_CHANNELS.has(ch)
      );
      if (channels.length === 0) {
        setHasFetched(true);
        return;
      }

      setIsLoading(true);
      try {
        const byChannel = await Promise.all(
          channels.map((ch) =>
            fetchSourceIdentifiers(setId!, personId!, ch).catch(() => [])
          )
        );
        const flattened = byChannel.flat();

        const map = new Map<string, number>();
        for (const item of flattened) {
          const operator = item.operator_name || "Unknown";
          map.set(operator, (map.get(operator) || 0) + (item.total_interactions || 0));
        }

        const rows = [...map.entries()]
          .map(([operator, interactions]) => ({ operator, interactions }))
          .sort((a, b) => b.interactions - a.interactions);
        setOperatorCounts(rows);
      } catch (err) {
        console.error("Failed to fetch message operator counts:", err);
      } finally {
        setIsLoading(false);
        setHasFetched(true);
      }
    },
    [hasFetched, canLazyLoad, source.channel_breakdown, setId, personId]
  );

  return (
    <HoverCard openDelay={100} closeDelay={200} onOpenChange={handleOpenChange}>
      <HoverCardTrigger asChild>
        <span className="inline-flex">
          <Badge
            variant="outline"
            className={cn(
              "flex items-center gap-1.5 px-2 py-1 text-xs font-normal border cursor-pointer hover:opacity-80 transition-opacity",
              config.color
            )}
          >
            <Icon className="h-3.5 w-3.5" />
            {!compact && <span>{config.label}</span>}
            {showCount && (
              <span className="font-medium">
                {formatMessageCount(source.total_interactions)}
              </span>
            )}
          </Badge>
        </span>
      </HoverCardTrigger>
      <HoverCardContent className="w-72 p-0" align="start">
        <div className="p-3 border-b">
          <div className="flex items-center gap-2">
            <MessageCircle className="h-4 w-4 text-emerald-400" />
            <span className="font-medium text-sm">Messages</span>
            {source.total_interactions > 0 && (
              <span className="text-xs text-muted-foreground ml-auto">
                {formatMessageCount(source.total_interactions)} total
              </span>
            )}
          </div>
        </div>
        <div className="max-h-64 overflow-y-auto">
          {isLoading && (
            <div className="flex items-center justify-center py-4">
              <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
              <span className="ml-2 text-xs text-muted-foreground">Loading...</span>
            </div>
          )}
          {!isLoading && !canLazyLoad && (
            <div className="px-3 py-3 text-xs text-muted-foreground">
              Operator breakdown unavailable
            </div>
          )}
          {!isLoading && canLazyLoad && hasFetched && operatorCounts.length === 0 && (
            <div className="px-3 py-3 text-xs text-muted-foreground">
              No operator message data
            </div>
          )}
          {!isLoading &&
            operatorCounts.map((row) => (
              <div
                key={row.operator}
                className="flex items-center justify-between px-3 py-2 hover:bg-muted/50 border-b last:border-b-0"
              >
                <span className="text-sm truncate pr-2">{row.operator}</span>
                <span className="text-[10px] font-medium tabular-nums shrink-0">
                  {formatMessageCount(row.interactions)} msgs
                </span>
              </div>
            ))}
        </div>
      </HoverCardContent>
    </HoverCard>
  );
}

export default PersonSourceBadges;
