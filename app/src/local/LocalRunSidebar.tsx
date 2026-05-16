import { format, isSameYear, isToday, isYesterday } from "date-fns";
import { Search, Database, Loader2, Users, Building2, ListFilter } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import type { LocalRunSummary } from "./types";

interface LocalRunSidebarProps {
  runs: LocalRunSummary[];
  selectedTaskId?: string | null;
  activeRoute?: "results" | "contacts" | "companies";
  isLoading?: boolean;
  search: string;
  onSearchChange: (value: string) => void;
  onNavigateResults?: () => void;
  onNavigateContacts?: () => void;
  onNavigateCompanies?: () => void;
  onSelect: (run: LocalRunSummary) => void;
}

function dateForRun(run: LocalRunSummary): Date {
  return new Date(run.updatedAt || run.createdAt || run.mtimeMs);
}

function groupLabel(date: Date): string {
  if (isToday(date)) return "Today";
  if (isYesterday(date)) return "Yesterday";
  if (isSameYear(date, new Date())) return format(date, "MMMM d");
  return format(date, "MMM d, yyyy");
}

export function LocalRunSidebar({
  runs,
  selectedTaskId,
  activeRoute = "results",
  isLoading,
  search,
  onSearchChange,
  onNavigateResults,
  onNavigateContacts,
  onNavigateCompanies,
  onSelect,
}: LocalRunSidebarProps) {
  const grouped = runs.reduce<Record<string, LocalRunSummary[]>>((acc, run) => {
    const label = groupLabel(dateForRun(run));
    acc[label] = acc[label] || [];
    acc[label].push(run);
    return acc;
  }, {});

  return (
    <aside className="flex h-dvh w-[360px] shrink-0 flex-col border-r bg-card min-w-0">
      <div className="border-b p-4">
        <div className="flex items-center gap-2">
          <div className="flex h-8 w-8 items-center justify-center rounded-md bg-primary/10">
            <Database className="h-4 w-4 text-primary" />
          </div>
          <div className="min-w-0">
            <h1 className="truncate text-base font-semibold">Powerpacks Viewer</h1>
            <p className="text-xs text-muted-foreground">../powerpacks/.powerpacks</p>
          </div>
        </div>
        <div className="relative mt-3">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input
            value={search}
            onChange={(event) => onSearchChange(event.target.value)}
            placeholder="Search runs..."
            className="pl-8"
          />
        </div>
        <div className="mt-3 grid gap-1">
          <button
            onClick={onNavigateResults}
            className={cn(
              "flex w-full items-center gap-2 rounded-md border px-2.5 py-2 text-left text-sm transition-colors hover:bg-accent",
              activeRoute === "results" ? "border-primary bg-primary/5 text-primary" : "border-transparent"
            )}
          >
            <ListFilter className="h-4 w-4" />
            Search Results
          </button>
          <button
            onClick={onNavigateContacts}
            className={cn(
              "flex w-full items-center gap-2 rounded-md border px-2.5 py-2 text-left text-sm transition-colors hover:bg-accent",
              activeRoute === "contacts" ? "border-primary bg-primary/5 text-primary" : "border-transparent"
            )}
          >
            <Users className="h-4 w-4" />
            My Contacts
          </button>
          <button
            onClick={onNavigateCompanies}
            className={cn(
              "flex w-full items-center gap-2 rounded-md border px-2.5 py-2 text-left text-sm transition-colors hover:bg-accent",
              activeRoute === "companies" ? "border-primary bg-primary/5 text-primary" : "border-transparent"
            )}
          >
            <Building2 className="h-4 w-4" />
            Company Directory
          </button>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {isLoading ? (
          <div className="flex items-center justify-center gap-2 p-6 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading runs...
          </div>
        ) : runs.length === 0 ? (
          <div className="p-4 text-sm text-muted-foreground">No runs found.</div>
        ) : (
          Object.entries(grouped).map(([label, items]) => (
            <div key={label} className="mb-3">
              <div className="px-2 pb-1 pt-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                {label}
              </div>
              <div className="space-y-1">
                {items.map((run) => (
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
          ))
        )}
      </div>
    </aside>
  );
}
