import { spawnSync } from "child_process";

import { powerpacksRepoRoot } from "../lib/paths";
import { setupProcessEnv } from "../lib/env";
import { readRequestJson, sendJson } from "../lib/http";
import { parseLastJsonFragment } from "../lib/subprocess";
import { startSetupJob } from "../jobs";

const AUTH_SCRIPT = "packs/powerset/primitives/auth/auth.py";

function authCommand(sub: string): string[] {
  return ["uv", "run", "--project", ".", "python", AUTH_SCRIPT, sub];
}

export async function handlePowersetRoutes(req: any, res: any, url: URL): Promise<boolean> {
  // Quick, non-refreshing credential check for the onboarding status pill.
  if (url.pathname === "/local-api/powerset/whoami") {
    const result = spawnSync("uv", ["run", "--project", ".", "python", AUTH_SCRIPT, "whoami"], {
      cwd: powerpacksRepoRoot,
      env: setupProcessEnv(),
      encoding: "utf8",
      timeout: 30000,
    });
    const parsed = parseLastJsonFragment(result.stdout || "") || {};
    sendJson(res, {
      status: parsed.status || "anonymous",
      email: parsed.email || null,
      expired: parsed.expired ?? null,
      secondsRemaining: parsed.seconds_remaining ?? null,
    });
    return true;
  }

  // Browser-based Auth0 login. Long-running and interactive (opens a browser,
  // catches the callback), so it runs as a polled setup job rather than inline.
  if (url.pathname === "/local-api/powerset/login" && req.method === "POST") {
    await readRequestJson(req).catch(() => ({}));
    const job = startSetupJob("powerset-login", authCommand("login"), 10 * 60 * 1000);
    sendJson(res, { job });
    return true;
  }

  return false;
}
