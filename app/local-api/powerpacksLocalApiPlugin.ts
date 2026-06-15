import type { Plugin } from "vite";

import { sendJson } from "./lib/http";
import { handleRunsRoutes } from "./routes/runs";
import { handleSetupRoutes } from "./routes/setup";
import { handleEnvRoutes } from "./routes/env";
import { handleOnboardingRoutes } from "./routes/onboarding";
import { handleMessagesRoutes } from "./routes/messages";
import { handleContactsRoutes } from "./routes/contacts";
import { handlePowersetRoutes } from "./routes/powerset";
import { handlePersonDetailsRoutes } from "./routes/personDetails";
import { handleCompaniesRoutes } from "./routes/companies";
import { handleLocalSearchRoutes } from "./routes/localSearch";

// Each handler inspects the request and returns true once it has written a
// response, or false to let the next handler (and ultimately Vite) take over.
export type LocalApiRouteHandler = (req: any, res: any, url: URL) => boolean | Promise<boolean>;

const routeHandlers: LocalApiRouteHandler[] = [
  handleRunsRoutes,
  handleSetupRoutes,
  handleEnvRoutes,
  handleOnboardingRoutes,
  handleMessagesRoutes,
  handleContactsRoutes,
  handlePowersetRoutes,
  handlePersonDetailsRoutes,
  handleCompaniesRoutes,
  handleLocalSearchRoutes,
];

export function powerpacksLocalApiPlugin(): Plugin {
  return {
    name: "powerpacks-local-api",
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        try {
          const url = new URL(req.url || "/", "http://localhost");
          for (const handler of routeHandlers) {
            if (await handler(req, res, url)) return;
          }
          return next();
        } catch (err) {
          console.error("[powerpacks-local-api]", err);
          return sendJson(res, { error: err instanceof Error ? err.message : String(err) }, 500);
        }
      });
    },
  };
}
