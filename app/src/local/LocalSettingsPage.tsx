import { ChevronRight, Cpu, KeyRound, Mail, MessageCircle, Zap } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { LocalEnvPage } from "./LocalEnvPage";
import { LocalSystemPage } from "./LocalSystemPage";
import type { LocalProfileResponse } from "./types";

export type SettingsSection = "integrations" | "environment" | "system";

interface LocalSettingsPageProps {
  section: SettingsSection;
  sources: LocalProfileResponse["accounts"]["sources"];
  navigate: (path: string) => void;
}

const NAV_ITEMS: Array<{ id: SettingsSection; label: string; icon: typeof Zap; group: string }> = [
  { id: "integrations", label: "Integrations", icon: Zap, group: "Features" },
  { id: "environment", label: "Environment", icon: KeyRound, group: "Machines" },
  { id: "system", label: "System", icon: Cpu, group: "Machines" },
];

// Sources that have a dedicated status page, in display order. Maps the profile
// source id to its /sources/<route> page.
const ACCOUNT_ROWS: Array<{ id: string; route: string; label: string }> = [
  { id: "linkedin_csv", route: "linkedin", label: "LinkedIn" },
  { id: "gmail", route: "gmail", label: "Gmail" },
  { id: "messages", route: "messages", label: "Messages" },
];

function AccountIcon({ id }: { id: string }) {
  if (id === "linkedin_csv") {
    return (
      <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-[3px] bg-[#0A66C2] text-[11px] font-bold leading-none text-white">
        in
      </span>
    );
  }
  if (id === "gmail") return <Mail className="h-6 w-6 shrink-0 text-[#EA4335]" />;
  if (id === "messages") return <MessageCircle className="h-6 w-6 shrink-0 text-[#15803D]" />;
  return <Zap className="h-6 w-6 shrink-0 text-muted-foreground" />;
}

function StatusBadge({
  source,
}: {
  source?: LocalProfileResponse["accounts"]["sources"][number];
}) {
  if (source?.linked) {
    return (
      <Badge variant="outline" className="gap-1.5 border-green-600/30 bg-green-600/10 text-green-700">
        <span className="h-1.5 w-1.5 rounded-full bg-green-600" /> Connected
      </Badge>
    );
  }
  if (source?.skipped) {
    return <Badge variant="outline" className="text-muted-foreground">Skipped</Badge>;
  }
  return <Badge variant="outline" className="text-muted-foreground">Not connected</Badge>;
}

function IntegrationsPanel({
  sources,
  navigate,
}: {
  sources: LocalProfileResponse["accounts"]["sources"];
  navigate: (path: string) => void;
}) {
  const sourceById = new Map(sources.map((source) => [source.id, source]));
  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-2xl font-semibold">Integrations</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Connect data sources and review their status. Click a source to manage it.
        </p>
      </div>
      <Card>
        <CardContent className="divide-y p-0">
          {ACCOUNT_ROWS.map((row) => {
            const source = sourceById.get(row.id);
            return (
              <button
                key={row.id}
                type="button"
                onClick={() => navigate(`/sources/${row.route}`)}
                className="flex w-full items-center gap-3 px-4 py-3.5 text-left transition-colors hover:bg-accent"
              >
                <AccountIcon id={row.id} />
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-medium">{row.label}</div>
                </div>
                <StatusBadge source={source} />
                <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
              </button>
            );
          })}
        </CardContent>
      </Card>
    </div>
  );
}

export function LocalSettingsPage({ section, sources, navigate }: LocalSettingsPageProps) {
  const groups = NAV_ITEMS.reduce<Record<string, typeof NAV_ITEMS>>((acc, item) => {
    (acc[item.group] = acc[item.group] || []).push(item);
    return acc;
  }, {});

  return (
    <div className="flex min-h-full">
      {/* Settings sub-sidebar — the "second sidebar", flush against the main rail.
          Extensible: add sections to NAV_ITEMS. */}
      <nav className="w-56 shrink-0 space-y-5 border-r px-3 py-5">
        <div className="px-2 text-base font-semibold">Settings</div>
        {Object.entries(groups).map(([group, items]) => (
          <div key={group} className="space-y-1">
            <div className="px-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              {group}
            </div>
            {items.map((item) => {
              const Icon = item.icon;
              const active = item.id === section;
              return (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => navigate(`/settings/${item.id}`)}
                  className={cn(
                    "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-sm font-medium transition-colors",
                    active ? "bg-accent text-accent-foreground" : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
                  )}
                >
                  <Icon className="h-4 w-4" />
                  {item.label}
                </button>
              );
            })}
          </div>
        ))}
      </nav>

      <div className="min-w-0 flex-1 px-8 py-6">
        <div className="mx-auto max-w-4xl">
          {section === "integrations" ? (
            <IntegrationsPanel sources={sources} navigate={navigate} />
          ) : section === "environment" ? (
            <LocalEnvPage />
          ) : (
            <LocalSystemPage />
          )}
        </div>
      </div>
    </div>
  );
}
