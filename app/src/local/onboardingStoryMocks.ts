import { HttpResponse, delay, http } from "msw";

type OnboardingMockOptions = {
  loggedIn?: boolean;
  envReady?: boolean;
  linkedinStatus?: "missing" | "running" | "completed";
  gmail?: "empty" | "pending" | "connected";
};

const now = "2026-06-17T18:30:00.000Z";

function job(action: string, output: Record<string, unknown> = {}) {
  return {
    id: `storybook-${action}`,
    action,
    actionKey: action,
    status: "completed",
    startedAt: now,
    completedAt: now,
    command: ["storybook", action],
    code: 0,
    stdout: "Storybook mock completed.",
    stderr: "",
    output,
  };
}

function envStatus(ready: boolean) {
  const keys = [
    {
      key: "OPENAI_API_KEY",
      label: "OpenAI API key",
      provider: "OpenAI",
      description: "Used for search and ranking.",
      required: true,
      getUrl: "https://platform.openai.com/api-keys",
      status: ready ? "present" : "missing",
      satisfied: ready,
      valuePreview: ready ? "sk-...storybook" : undefined,
      aliases: [],
    },
    {
      key: "RAPIDAPI_LINKEDIN_KEY",
      label: "RapidAPI LinkedIn key",
      provider: "RapidAPI",
      description: "Used for LinkedIn profile enrichment.",
      required: true,
      getUrl: "https://rapidapi.com/",
      status: ready ? "present" : "missing",
      satisfied: ready,
      valuePreview: ready ? "rap...mock" : undefined,
      aliases: [],
    },
    {
      key: "PARALLEL_API_KEY",
      label: "Parallel API key",
      provider: "Parallel.ai",
      description: "Used to resolve LinkedIn URLs from email metadata.",
      required: true,
      getUrl: "https://parallel.ai/",
      status: ready ? "present" : "missing",
      satisfied: ready,
      valuePreview: ready ? "par...mock" : undefined,
      aliases: [],
    },
  ];
  return {
    path: "/Users/arthur/workspace/powerpacks/.env",
    exists: true,
    updatedAt: now,
    sizeBytes: 512,
    keys,
    summary: {
      total: keys.length,
      required: keys.length,
      ready,
      missingRequired: ready ? 0 : keys.length,
      present: ready ? keys.length : 0,
      empty: 0,
      missing: ready ? 0 : keys.length,
    },
  };
}

function linkedinStatus(status: "missing" | "running" | "completed") {
  if (status === "missing") {
    return {
      schema_version: 1,
      vertical: "linkedin_modal",
      status: "missing",
      progress: 0,
      current_stage: "upload",
      updated_at: now,
      stage_order: [
        { id: "importing", label: "Importing contacts" },
        { id: "indexing", label: "Building search index" },
      ],
      stages: {},
      result: {},
      active_job: null,
      stale: false,
    };
  }

  return {
    schema_version: 1,
    vertical: "linkedin_modal",
    status,
    progress: status === "completed" ? 1 : 0.58,
    current_stage: status === "completed" ? "indexing" : "importing",
    updated_at: now,
    stage_order: [
      { id: "importing", label: "Importing contacts" },
      { id: "indexing", label: "Building search index" },
    ],
    stages: {
      importing: {
        status: "completed",
        label: "Importing contacts",
        count: 428,
        updated_at: now,
      },
      indexing: {
        status: status === "completed" ? "completed" : "running",
        label: "Building search index",
        count: status === "completed" ? 428 : 219,
        updated_at: now,
      },
    },
    result: {
      csv: ".powerpacks/ingestion/uploads/linkedin/storybook_connections.csv",
      people_csv: ".powerpacks/network-import/merged/people.csv",
      duckdb: ".powerpacks/search-index/local-search.duckdb",
      connections: 428,
      indexed: status === "completed" ? 428 : 219,
    },
    active_job: status === "running" ? "storybook-linkedin" : null,
    stale: false,
  };
}

function gmailAccounts(mode: OnboardingMockOptions["gmail"]) {
  if (mode !== "connected") {
    return { status: "completed", accounts: [] };
  }
  return {
    status: "completed",
    accounts: [
      {
        email: "arthur@powerset.co",
        message_count: 12855,
        last_sync: "2026-06-17 12:30:00",
      },
    ],
  };
}

function msgvaultStatus(mode: OnboardingMockOptions["gmail"]) {
  const desired = ["arthur@powerset.co"];
  return {
    status: "ok",
    owner_email: "arthur@powerset.co",
    desired_emails: mode === "empty" ? [] : desired,
    accounts:
      mode === "connected"
        ? [{ email: "arthur@powerset.co", message_count: 12855, last_sync: "2026-06-17 12:30:00" }]
        : [],
    config: { oauth_configured: true, exists: true },
    database: { exists: true },
    gcloud: { installed: true, account: "arthur@powerset.co", project: "storybook-local" },
    msgvault: { installed: true },
  };
}

export function onboardingHandlers(options: OnboardingMockOptions = {}) {
  const {
    loggedIn = false,
    envReady = false,
    linkedinStatus: linkedin = "missing",
    gmail = "connected",
  } = options;

  return [
    http.get("/local-api/powerset/whoami", () =>
      HttpResponse.json({
        status: loggedIn ? "logged_in" : "logged_out",
        email: loggedIn ? "arthur@powerset.co" : null,
        expired: loggedIn ? false : null,
        secondsRemaining: loggedIn ? 86400 : null,
      })
    ),
    http.post("/local-api/powerset/login", async () => {
      await delay(250);
      return HttpResponse.json({ job: job("powerset-login") });
    }),
    http.post("/local-api/powerset/pull-keys", async () => {
      await delay(250);
      return HttpResponse.json({ job: job("powerset-pull-keys", { status: "ok", missing: [] }) });
    }),
    http.get("/local-api/env/status", () => HttpResponse.json(envStatus(envReady))),
    http.post("/local-api/env/update", async ({ request }) => {
      const body = (await request.json()) as Record<string, string>;
      return HttpResponse.json({
        written: Object.keys(body),
        rejected: [],
        status: envStatus(true),
      });
    }),
    http.get("/local-api/setup/jobs/:jobId", ({ params }) => {
      const action = String(params.jobId).replace(/^storybook-/, "");
      return HttpResponse.json(job(action, { status: "ok", missing: [] }));
    }),
    http.post("/local-api/setup/linkedin-csv-upload", async ({ request }) => {
      const body = (await request.json()) as { filename?: string };
      return HttpResponse.json({
        path: `.powerpacks/ingestion/uploads/linkedin/${body.filename || "Connections.csv"}`,
      });
    }),
    http.get("/local-api/onboarding/linkedin/status", () => HttpResponse.json(linkedinStatus(linkedin))),
    http.post("/local-api/onboarding/linkedin/run", async () => {
      await delay(250);
      return HttpResponse.json({
        job: job("linkedin-import", { status: "ok" }),
        status: linkedinStatus("completed"),
      });
    }),
    http.get("/local-api/onboarding/gmail/accounts", () => HttpResponse.json(gmailAccounts(gmail))),
    http.get("/local-api/onboarding/gmail/msgvault-status", () => HttpResponse.json(msgvaultStatus(gmail))),
    http.post("/local-api/onboarding/gmail/estimate", async ({ request }) => {
      const body = (await request.json()) as { windows?: string[] };
      const totals: Record<string, { messages: number; est_seconds: number; est_minutes: number }> = {};
      for (const window of body.windows || ["1y", "2y", "5y", "all"]) {
        const messages = window === "all" ? 12855 : window === "5y" ? 9440 : window === "2y" ? 3920 : 1840;
        totals[window] = {
          messages,
          est_seconds: Math.round(messages / 120),
          est_minutes: Math.max(1, Math.round(messages / 7200)),
        };
      }
      return HttpResponse.json({ status: "completed", windows: body.windows || [], totals });
    }),
    http.post("/local-api/onboarding/gmail/sync", async () => {
      await delay(250);
      return HttpResponse.json({ job: job("gmail-sync", { status: "ok" }) });
    }),
    http.post("/local-api/onboarding/gmail/authorize", async () => {
      await delay(250);
      return HttpResponse.json({ job: job("gmail-authorize", { status: "ok" }) });
    }),
    http.post("/local-api/setup/run", async () => {
      await delay(250);
      return HttpResponse.json({ job: job("setup-run", { status: "ok" }) });
    }),
  ];
}
