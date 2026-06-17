import { useState, type ReactNode } from "react";

import { TooltipProvider } from "@/components/ui/tooltip";

import { LocalRunSidebar } from "./LocalRunSidebar";

// Storybook-only preview utilities. Product UI changes should be made in the
// imported app components, not in these wrappers.
const storybookRuns = [
  {
    taskId: "storybook-run-1",
    conversationId: "storybook-run-1",
    fileName: "sf-engineers.json",
    query: "software engineers in SF who worked at early-stage startups",
    status: "completed",
    createdAt: "2026-06-17T17:30:00.000Z",
    updatedAt: "2026-06-17T17:45:00.000Z",
    mtimeMs: Date.parse("2026-06-17T17:45:00.000Z"),
    rowCount: 42,
    hydratedCount: 42,
    hasArtifacts: true,
    artifactDir: ".powerpacks/runs/storybook-run-1",
  },
  {
    taskId: "storybook-run-2",
    conversationId: "storybook-run-2",
    fileName: "founders.json",
    query: "founders in my network who know infra tooling",
    status: "completed",
    createdAt: "2026-06-16T15:20:00.000Z",
    updatedAt: "2026-06-16T15:31:00.000Z",
    mtimeMs: Date.parse("2026-06-16T15:31:00.000Z"),
    rowCount: 18,
    hydratedCount: 18,
    hasArtifacts: true,
    artifactDir: ".powerpacks/runs/storybook-run-2",
  },
];

const storybookSources = [
  {
    id: "linkedin_csv",
    label: "LinkedIn",
    status: "linked",
    linked: true,
    skipped: false,
    usernames: ["Connections.csv"],
  },
];

export function SourceOfTruthNotice() {
  return (
    <div className="mb-4 rounded-md border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-950">
      <div className="font-semibold">Storybook preview only</div>
      <div className="mt-1">
        Edit main app components if you need to change anything. Onboarding copy/layout lives in{" "}
        <code>app/src/local/LocalOnboardingPage.tsx</code> and{" "}
        <code>app/src/local/LocalOnboardingV2Page.tsx</code>; the sidebar lives in{" "}
        <code>app/src/local/LocalRunSidebar.tsx</code>; Gmail controls live in{" "}
        <code>app/src/local/GmailSyncPanel.tsx</code>. These stories only provide mocked API data and preview wrappers.
      </div>
    </div>
  );
}

export function StorybookPreview({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-dvh bg-background px-6 py-5 text-foreground">
      <SourceOfTruthNotice />
      {children}
    </div>
  );
}

export function StorybookAppShell({ children }: { children: ReactNode }) {
  const [search, setSearch] = useState("");

  return (
    <TooltipProvider delayDuration={100} skipDelayDuration={300}>
      <div className="flex h-dvh overflow-hidden bg-background text-foreground">
        <LocalRunSidebar
          activeView="runs"
          runs={storybookRuns}
          operatorEmail="arthur@powerset.co"
          accountSources={storybookSources}
          selectedTaskId="storybook-run-1"
          isLoading={false}
          search={search}
          onSearchChange={setSearch}
          onNewSearch={() => {}}
          onSelectContacts={() => {}}
          onSelectCompanies={() => {}}
          onSelectEnv={() => {}}
          onSelectSystem={() => {}}
          onSelectLinkSetup={() => {}}
          onSelectSource={() => {}}
          onSelect={() => {}}
        />
        <main className="min-w-0 flex-1 overflow-y-auto">
          <div className="mx-auto max-w-5xl px-8 py-8">
            <SourceOfTruthNotice />
            {children}
          </div>
        </main>
      </div>
    </TooltipProvider>
  );
}

export function linkedinConnectionsCsv(count: number) {
  const header = "First Name,Last Name,URL,Email Address,Company,Position,Connected On";
  const rows = Array.from({ length: count }, (_, index) => {
    const n = index + 1;
    return `Person${n},Example,https://www.linkedin.com/in/person-${n},person${n}@example.com,Example Co,Engineer,17 Jun 2026`;
  });
  return [
    "Notes:",
    "This archive is a Storybook fixture.",
    "",
    header,
    ...rows,
  ].join("\n");
}

export function HighlightStyles() {
  return (
    <style>
      {`
        [data-story-highlight="process-button"] {
          box-shadow: 0 0 0 4px hsl(var(--ring) / 0.32), 0 0 0 8px hsl(var(--primary) / 0.12);
          transform: translateY(-1px);
        }
      `}
    </style>
  );
}
