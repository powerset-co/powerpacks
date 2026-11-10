// The Companies page's route on the local server: the company search, for one scope (the local
// index or a Powerset set). No name lists every company in the scope.

import { body, failure } from "@/lib/api/http"
import type { CompanySearchRequest, CompanySearchResponse } from "@/types/companies"

export async function searchCompanies(request: CompanySearchRequest): Promise<CompanySearchResponse> {
  const response = await fetch("/api/companies/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  })
  if (!response.ok) throw await failure(response, "Couldn't search companies.")
  return body<CompanySearchResponse>(response)
}
