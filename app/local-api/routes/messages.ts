import { messagesReviewPrimitive, messagesReviewResponse } from "../lib/messagesReview";
import { readRequestJson, sendJson } from "../lib/http";

export async function handleMessagesRoutes(req: any, res: any, url: URL): Promise<boolean> {
  if (url.pathname === "/local-api/messages/review") {
    const offset = Math.max(0, Number(url.searchParams.get("offset") || 0) || 0);
    const limit = Math.min(500, Math.max(1, Number(url.searchParams.get("limit") || 100) || 100));
    const filter = (url.searchParams.get("filter") || "all").trim().toLowerCase();
    const query = url.searchParams.get("q") || "";
    sendJson(res, messagesReviewResponse(filter, query, offset, limit));
    return true;
  }

  if (url.pathname === "/local-api/messages/review/toggle" && req.method === "POST") {
    const body = await readRequestJson(req);
    sendJson(res, messagesReviewPrimitive("toggle", [
      "--row", String(body.row ?? body.index ?? ""),
      "--selected", body.selected === true || String(body.selected).toLowerCase() === "true" ? "true" : "false",
    ]));
    return true;
  }

  if (url.pathname === "/local-api/messages/review/hint" && req.method === "POST") {
    const body = await readRequestJson(req);
    sendJson(res, messagesReviewPrimitive("hint", [
      "--row", String(body.row ?? body.index ?? ""),
      "--hint", String(body.hint || ""),
    ]));
    return true;
  }

  if (url.pathname === "/local-api/messages/review/bulk-toggle" && req.method === "POST") {
    const body = await readRequestJson(req);
    sendJson(res, messagesReviewPrimitive("bulk-toggle", [
      "--tab", String(body.tab || "in_network"),
      "--selected", body.selected === true || String(body.selected).toLowerCase() === "true" ? "true" : "false",
    ]));
    return true;
  }

  return false;
}
