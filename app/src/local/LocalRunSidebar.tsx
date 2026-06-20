import { useEffect, useMemo, useState } from "react";
import { format, isSameMonth, isSameWeek, isSameYear, isToday, isYesterday } from "date-fns";
import { Building2, ContactRound, Loader2, PanelLeft, PanelLeftClose, Plus, Search, Settings } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { LocalRunSummary } from "./types";

const COLLAPSE_KEY = "powerpacks.sidebar.collapsed";
const DEFAULT_VISIBLE_RUNS = 16;

interface LocalRunSidebarProps {
  activeView: "contacts" | "companies" | "settings" | "runs";
  runs: LocalRunSummary[];
  operatorEmail?: string;
  selectedTaskId?: string | null;
  isLoading?: boolean;
  search: string;
  onSearchChange: (value: string) => void;
  onNewSearch: () => void;
  onSelectContacts: () => void;
  onSelectCompanies: () => void;
  onSelectSettings: () => void;
  onSelect: (run: LocalRunSummary) => void;
}

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

function avatarInitials(label: string): string {
  const local = (label.split("@")[0] || label).trim();
  const parts = local.split(/[.\-_+\s]/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return (local.slice(0, 2) || "?").toUpperCase();
}

// Primary nav item that collapses to an icon-only button with a tooltip.
function NavItem({
  icon: Icon,
  label,
  active,
  collapsed,
  onClick,
}: {
  icon: typeof ContactRound;
  label: string;
  active?: boolean;
  collapsed: boolean;
  onClick: () => void;
}) {
  const button = (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "flex h-8 items-center gap-2 rounded-md text-sm font-medium transition-colors",
        collapsed ? "w-8 justify-center" : "w-full px-2",
        active ? "bg-accent text-accent-foreground" : "hover:bg-accent hover:text-accent-foreground",
      )}
    >
      <Icon className="h-4 w-4 shrink-0" />
      {!collapsed && label}
    </button>
  );
  if (!collapsed) return button;
  return (
    <Tooltip>
      <TooltipTrigger asChild>{button}</TooltipTrigger>
      <TooltipContent side="right">{label}</TooltipContent>
    </Tooltip>
  );
}

export function LocalRunSidebar({
  activeView,
  runs,
  operatorEmail,
  selectedTaskId,
  isLoading,
  search,
  onSearchChange,
  onNewSearch,
  onSelectContacts,
  onSelectCompanies,
  onSelectSettings,
  onSelect,
}: LocalRunSidebarProps) {
  const [showAllRuns, setShowAllRuns] = useState(false);
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    try {
      return window.localStorage.getItem(COLLAPSE_KEY) === "1";
    } catch {
      return false;
    }
  });

  useEffect(() => {
    try {
      window.localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "0");
    } catch {
      // localStorage unavailable — collapse state just won't persist.
    }
  }, [collapsed]);

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

  return (
    <aside
      className={cn(
        "flex h-dvh shrink-0 flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground",
        collapsed ? "w-14 items-center" : "w-64 min-w-0",
      )}
    >
      <div className={cn("flex flex-col gap-2 p-2", collapsed && "w-full items-center")}>
        <div className={cn("flex items-center", collapsed ? "justify-center" : "justify-between px-2")}>
          {!collapsed && (
            <button
              type="button"
              onClick={onSelectContacts}
              className="flex items-center transition-opacity hover:opacity-80"
            >
              <span className="text-xl font-semibold text-foreground">POWER</span>
              <span className="text-xl font-semibold text-primary">PACKS</span>
            </button>
          )}
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                onClick={() => setCollapsed((value) => !value)}
                className="flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
                aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
              >
                {collapsed ? <PanelLeft className="h-4 w-4" /> : <PanelLeftClose className="h-4 w-4" />}
              </button>
            </TooltipTrigger>
            <TooltipContent side="right">{collapsed ? "Expand" : "Collapse"}</TooltipContent>
          </Tooltip>
        </div>

        <NavItem icon={Plus} label="New Search" collapsed={collapsed} onClick={onNewSearch} />

        {!collapsed && (
          <div className="relative mt-1">
            <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={search}
              onChange={(event) => onSearchChange(event.target.value)}
              placeholder="Search conversations..."
              className="pl-9"
            />
          </div>
        )}

        <div className={cn("space-y-1", collapsed ? "w-full" : "mt-1")}>
          <NavItem
            icon={ContactRound}
            label="My Contacts"
            active={activeView === "contacts"}
            collapsed={collapsed}
            onClick={onSelectContacts}
          />
          <NavItem
            icon={Building2}
            label="Companies"
            active={activeView === "companies"}
            collapsed={collapsed}
            onClick={onSelectCompanies}
          />
        </div>
      </div>

      {!collapsed && (
        <div className="min-h-0 flex-1 overflow-y-auto p-2">
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
                          selectedTaskId === run.taskId || selectedTaskId === run.conversationId ? "border-primary bg-primary/5" : "border-transparent",
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
      )}

      {collapsed && <div className="flex-1" />}

      {/* Account + settings — always available. */}
      <div className={cn("border-t p-2", collapsed ? "flex w-full flex-col items-center gap-2" : "flex items-center gap-2")}>
        <div
          className={cn(
            "flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-muted text-[11px] font-semibold",
            !collapsed && "ml-1",
          )}
          title={accountLabel}
        >
          {avatarInitials(accountLabel)}
        </div>
        {!collapsed && (
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-medium">{accountLabel}</div>
            <div className="truncate text-xs text-muted-foreground">Local workspace</div>
          </div>
        )}
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              onClick={onSelectSettings}
              aria-label="Settings"
              className={cn(
                "flex h-8 w-8 shrink-0 items-center justify-center rounded-md transition-colors hover:bg-accent hover:text-accent-foreground",
                activeView === "settings" ? "bg-accent text-accent-foreground" : "text-muted-foreground",
              )}
            >
              <Settings className="h-4 w-4" />
            </button>
          </TooltipTrigger>
          <TooltipContent side="right">Settings</TooltipContent>
        </Tooltip>
      </div>
    </aside>
  );
}
