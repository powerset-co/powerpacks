import { useMemo, useState } from "react";
import { format, isSameMonth, isSameWeek, isSameYear, isToday, isYesterday } from "date-fns";
import { Building2, ContactRound, Cpu, KeyRound, Loader2, Mail, MessageCircle, Plus, Search, Settings2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import { cn } from "@/lib/utils";
import type { LocalProfileResponse, LocalRunSummary } from "./types";

interface LocalRunSidebarProps {
  activeView: "contacts" | "companies" | "setup" | "env" | "system" | "runs";
  runs: LocalRunSummary[];
  operatorEmail?: string;
  accountSources?: LocalProfileResponse["accounts"]["sources"];
  selectedTaskId?: string | null;
  isLoading?: boolean;
  search: string;
  onSearchChange: (value: string) => void;
  onNewSearch: () => void;
  onSelectContacts: () => void;
  onSelectCompanies: () => void;
  onSelectSetup: () => void;
  onSelectEnv: () => void;
  onSelectSystem: () => void;
  onSelectLinkSetup: () => void;
  onSelectSource?: (id: string) => void;
  onSelect: (run: LocalRunSummary) => void;
}

const DEFAULT_VISIBLE_RUNS = 16;

function dateForRun(run: LocalRunSummary): Date {
  return new Date(run.updatedAt || run.createdAt || run.mtimeMs);
}

function groupLabel(date: Date): string {
  if (isToday(date)) return "Today";
  if (isYesterday(date)) return "Yesterday";
  if (isSameWeek(date, new Date())) return "This Week";
  if (isSameMonth(date, new Date())) return "This Month";
  if (isSameYear(date, new Date())) return format(date, "MMMM");
  return format(date, "yyyy");
}

function groupRank(label: string): number {
  if (label === "Today") return 0;
  if (label === "Yesterday") return 1;
  if (label === "This Week") return 2;
  if (label === "This Month") return 3;
  return 10;
}

function SourceStatusButton({
  source,
}: {
  source?: LocalProfileResponse["accounts"]["sources"][number];
}) {
  if (source?.linked) {
    return (
      <span className="relative flex items-center gap-1.5 py-2">
        <span className="h-2 w-2 rounded-full bg-green-600 shadow-[0_0_0_3px_rgba(21,128,61,0.15)]" />
        <span className="text-[10.5px] font-semibold text-green-700">Connected</span>
      </span>
    );
  }
  if (source?.skipped) {
    return (
      <span className="relative text-[10.5px] font-semibold text-muted-foreground bg-muted border border-border rounded-md px-2.5 py-1">
        Skipped
      </span>
    );
  }
  return (
    <span className="relative text-[10.5px] font-semibold text-muted-foreground bg-muted border border-border rounded-md px-2.5 py-1">
      Connect
    </span>
  );
}

export function LocalRunSidebar({
  activeView,
  runs,
  operatorEmail,
  accountSources = [],
  selectedTaskId,
  isLoading,
  search,
  onSearchChange,
  onNewSearch,
  onSelectContacts,
  onSelectCompanies,
  onSelectSetup,
  onSelectEnv,
  onSelectSystem,
  onSelectLinkSetup,
  onSelectSource,
  onSelect,
}: LocalRunSidebarProps) {
  const [showAllRuns, setShowAllRuns] = useState(false);

  const visibleRuns = useMemo(() => {
    if (showAllRuns || runs.length <= DEFAULT_VISIBLE_RUNS) return runs;
    const next = runs.slice(0, DEFAULT_VISIBLE_RUNS);
    const selected = runs.find((run) => run.taskId === selectedTaskId || run.conversationId === selectedTaskId);
    if (selected && !next.some((run) => run.taskId === selected.taskId)) next.push(selected);
    return next;
  }, [runs, selectedTaskId, showAllRuns]);

  const grouped = visibleRuns.reduce<Record<string, { latest: number; items: LocalRunSummary[] }>>((acc, run) => {
    const label = groupLabel(dateForRun(run));
    acc[label] = acc[label] || { latest: 0, items: [] };
    acc[label].latest = Math.max(acc[label].latest, dateForRun(run).getTime());
    acc[label].items.push(run);
    return acc;
  }, {});
  const orderedGroups = Object.entries(grouped).sort(([labelA, groupA], [labelB, groupB]) => {
    const rankA = groupRank(labelA);
    const rankB = groupRank(labelB);
    if (rankA !== rankB) return rankA - rankB;
    return groupB.latest - groupA.latest;
  });
  const accountLabel = operatorEmail || "Local operator";
  const sourceById = new Map(accountSources.map((source) => [source.id, source]));
  const connectionRows = [
    { id: "linkedin_csv", label: "LinkedIn" },
    // Temporarily hidden until these source pages are fast enough for the sidebar.
    // { id: "gmail", label: "Gmail" },
    // { id: "messages", label: "Messages" },
    // { id: "twitter", label: "Socials" },
  ].map((item) => ({ ...item, source: sourceById.get(item.id) }));

  return (
    <aside className="flex h-dvh w-64 shrink-0 flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground min-w-0">
      <div className="flex flex-col gap-2 p-2">
        <div className="flex items-center justify-between px-2">
          <button
            type="button"
            onClick={onSelectContacts}
            className="flex items-center transition-opacity hover:opacity-80"
          >
            <span className="text-xl font-semibold text-foreground">POWER</span>
            <span className="text-xl font-semibold text-primary">PACKS</span>
          </button>
        </div>

        <div className="space-y-1 px-2 py-1">
          {connectionRows.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => (onSelectSource ? onSelectSource(item.id) : onSelectLinkSetup())}
              className="flex w-full items-center justify-between rounded-md p-1.5 text-left transition-colors hover:bg-accent"
            >
              <div className="flex min-w-0 items-center">
                {item.id === "linkedin_csv" ? (
                  <span className="mr-1.5 flex h-[15px] w-[15px] shrink-0 items-center justify-center rounded-[2px] bg-[#0A66C2] text-[9px] font-bold leading-none text-white">in</span>
                ) : item.id === "gmail" ? (
                  <Mail className="mr-1.5 h-[15px] w-[15px] shrink-0 text-[#EA4335]" />
                ) : item.id === "messages" ? (
                  <MessageCircle className="mr-1.5 h-[15px] w-[15px] shrink-0 text-[#15803D]" />
                ) : (
                  <span className="mr-1.5 flex h-[15px] w-[15px] shrink-0 items-center justify-center text-xs font-semibold">X</span>
                )}
                <span className="truncate text-xs font-medium">{item.label}</span>
              </div>
              <SourceStatusButton source={item.source} />
            </button>
          ))}
        </div>

        <Separator className="mx-2 my-1 w-auto bg-sidebar-border" />

        <button
          type="button"
          data-testid="local-new-search"
          onClick={onNewSearch}
          className="flex w-full items-center justify-center gap-2 rounded-md border border-input bg-background px-3 py-1.5 text-sm font-medium shadow-sm hover:bg-accent hover:text-accent-foreground"
        >
          <Plus size={16} />
          <span>New Search</span>
        </button>

        <div className="relative mt-1.5">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={search}
            onChange={(event) => onSearchChange(event.target.value)}
            placeholder="Search conversations..."
            className="pl-9"
          />
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        <div className="mb-2">
          <div className="flex h-8 shrink-0 items-center px-2 text-xs font-medium text-muted-foreground">Data</div>
          <button
            type="button"
            onClick={onSelectContacts}
            className={cn(
              "flex h-8 w-full items-center gap-2 rounded-md px-2 text-sm font-medium transition-colors",
              activeView === "contacts" ? "bg-accent text-accent-foreground" : "hover:bg-accent hover:text-accent-foreground"
            )}
          >
            <ContactRound className="h-4 w-4" />
            My Contacts
          </button>
          <button
            type="button"
            onClick={onSelectCompanies}
            className={cn(
              "flex h-8 w-full items-center gap-2 rounded-md px-2 text-sm font-medium transition-colors",
              activeView === "companies" ? "bg-accent text-accent-foreground" : "hover:bg-accent hover:text-accent-foreground"
            )}
          >
            <Building2 className="h-4 w-4" />
            Companies
          </button>
        </div>

        {isLoading ? (
          <div className="flex items-center justify-center gap-2 p-6 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading conversations...
          </div>
        ) : runs.length === 0 ? (
          <div className="p-4 text-sm text-muted-foreground">No conversations found.</div>
        ) : (
          <>
            {orderedGroups.map(([label, group]) => (
              <div key={label} className="mb-3">
                <div className="flex h-8 shrink-0 items-center px-2 text-xs font-medium text-muted-foreground">
                  {label}
                </div>
                <div className="space-y-1">
                  {group.items.map((run) => (
                    <button
                      key={run.taskId}
                      onClick={() => onSelect(run)}
                      className={cn(
                        "w-full rounded-md border p-2 text-left transition-colors hover:bg-accent min-w-0",
                        selectedTaskId === run.taskId || selectedTaskId === run.conversationId ? "border-primary bg-primary/5" : "border-transparent"
                      )}
                    >
                      <div className="min-w-0">
                        <div className="truncate text-sm font-medium">{run.query || "Untitled search"}</div>
                      </div>
                      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                        <span>{format(dateForRun(run), "h:mm a")}</span>
                        {run.rowCount != null && <Badge variant="secondary" className="text-[10px]">{run.rowCount.toLocaleString()} results</Badge>}
                      </div>
                    </button>
                  ))}
                </div>
              </div>
            ))}
            {runs.length > visibleRuns.length && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="mb-2 w-full justify-center"
                onClick={() => setShowAllRuns(true)}
              >
                Show more ({runs.length - visibleRuns.length})
              </Button>
            )}
          </>
        )}
      </div>

      <div className="border-t p-2">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              className="flex w-full items-center gap-2 rounded-md px-2 py-2 text-left transition-colors hover:bg-accent"
            >
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-muted text-xs font-semibold">
                {accountLabel.slice(0, 1).toUpperCase()}
              </div>
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium">{accountLabel}</div>
                <div className="truncate text-xs text-muted-foreground">Local workspace</div>
              </div>
              <Settings2 className="h-4 w-4 shrink-0 text-muted-foreground" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" side="top" className="w-56">
            <DropdownMenuItem onClick={onSelectSetup}>
              <Settings2 className="mr-2 h-4 w-4" />
              Setup
            </DropdownMenuItem>
            <DropdownMenuItem onClick={onSelectEnv}>
              <KeyRound className="mr-2 h-4 w-4" />
              Environment
            </DropdownMenuItem>
            <DropdownMenuItem onClick={onSelectSystem}>
              <Cpu className="mr-2 h-4 w-4" />
              System
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </aside>
  );
}
