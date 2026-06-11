import { useEffect, useState } from "react";
import { ArrowLeft, Building2, Globe, MapPin, Users } from "lucide-react";

import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { LinkedinLink } from "@/components/ui/social-link";
import {
  entityLabel,
  fetchLocalCompanyDetails,
  fundingStageLabel,
  sectorLabel,
  type LocalCompanyDetailsResponse,
  type LocalCompanyPerson,
} from "./companiesApi";

// Position epochs come back from the local API as unix seconds.
const formatEpoch = (epochSeconds?: number | null): string => {
  if (!epochSeconds) return "";
  try {
    const date = new Date(epochSeconds * 1000);
    const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    return `${months[date.getMonth()]} ${date.getFullYear()}`;
  } catch {
    return "";
  }
};

// SPA-friendly back navigation: the local app shell re-derives its view from
// the path on popstate, so push the new path and fire the event.
const navigateTo = (path: string) => {
  window.history.pushState({}, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
};

function personDateRange(person: LocalCompanyPerson): string {
  const start = formatEpoch(person.start_date_epoch);
  const end = person.end_date_epoch ? formatEpoch(person.end_date_epoch) : person.is_current ? "Present" : "";
  if (!start && !end) return "";
  return `${start}${start || end ? " – " : ""}${end}`;
}

function personInitials(person: LocalCompanyPerson): string {
  const parts = (person.full_name || "").trim().split(/\s+/).filter(Boolean);
  const first = parts[0]?.[0] || "";
  const last = parts.length > 1 ? parts[parts.length - 1][0] : "";
  return (first + last).toUpperCase() || "?";
}

export function LocalCompanyDetailsPage({ companyId }: { companyId: string }) {
  const [details, setDetails] = useState<LocalCompanyDetailsResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    setError(null);
    setDetails(null);
    if (!companyId) {
      setIsLoading(false);
      return () => undefined;
    }
    fetchLocalCompanyDetails(companyId)
      .then((response) => {
        if (!cancelled) setDetails(response);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load company details");
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [companyId]);

  if (isLoading) {
    return (
      <div className="animate-pulse space-y-4 p-6">
        <div className="h-8 w-48 bg-muted rounded"></div>
        <div className="h-64 bg-muted rounded"></div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="text-center py-12">
        <h2 className="text-2xl font-bold mb-2">Something went wrong</h2>
        <p className="text-muted-foreground mb-4">{error}</p>
        <Button onClick={() => navigateTo("/companies")}>Back to Companies</Button>
      </div>
    );
  }

  if (!details) {
    return (
      <div className="text-center py-12">
        <h2 className="text-2xl font-bold mb-2">Company Not Found</h2>
        <p className="text-muted-foreground mb-4">
          The company you're looking for doesn't exist or has been removed.
        </p>
        <Button onClick={() => navigateTo("/companies")}>Back to Companies</Button>
      </div>
    );
  }

  const { company, people } = details;

  const location =
    [company.city, company.state, company.country].filter(Boolean).join(", ") || company.metro_area || null;
  const websiteHref = company.website_domain
    ? company.website_domain.startsWith("http")
      ? company.website_domain
      : `https://${company.website_domain}`
    : null;
  const stageLabel = fundingStageLabel(company);
  const factParts = [
    company.headcount ? `${company.headcount.toLocaleString()} employees` : null,
    stageLabel,
    company.founded_year ? `Founded ${company.founded_year}` : null,
  ].filter(Boolean);

  const currentPeople = people.filter((person) => person.is_current);
  const pastPeople = people.filter((person) => !person.is_current);

  return (
    <div className="p-4 md:p-6 max-w-3xl mx-auto">
      <Button
        variant="ghost"
        size="sm"
        className="flex items-center gap-1 mb-3 -ml-2 text-xs"
        onClick={() => navigateTo("/companies")}
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        Back
      </Button>

      {/* Inset Card — identity + facts */}
      <div className="rounded-lg border bg-muted/30 p-4 mb-6 relative">
        <div className="flex items-start gap-3">
          <Avatar className="h-11 w-11 shrink-0 mt-0.5 rounded-md">
            <AvatarImage src={company.logo_url || ""} alt={company.company_name} className="object-contain" />
            <AvatarFallback className="rounded-md">
              <Building2 className="h-5 w-5 text-muted-foreground" />
            </AvatarFallback>
          </Avatar>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="text-base font-semibold">{company.company_name || "Unknown"}</span>
              {websiteHref && (
                <a
                  href={websiteHref}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center text-muted-foreground/70 hover:text-foreground transition-colors"
                  title={company.website_domain || undefined}
                >
                  <Globe className="h-3.5 w-3.5" />
                </a>
              )}
              {company.linkedin_url && <LinkedinLink href={company.linkedin_url} />}
            </div>
            {factParts.length > 0 && (
              <p className="text-sm text-muted-foreground truncate">{factParts.join(" · ")}</p>
            )}
            {location && (
              <p className="text-xs text-muted-foreground/60 flex items-center gap-1 mt-0.5">
                <MapPin className="h-3 w-3 shrink-0" />{location}
              </p>
            )}
            {(company.entity_types.length > 0 || company.sector_types.length > 0) && (
              <div className="flex flex-wrap gap-1 mt-2">
                {company.entity_types.map((entity) => (
                  <Badge key={entity} variant="outline" className="text-[10px] font-normal">
                    {entityLabel(entity)}
                  </Badge>
                ))}
                {company.sector_types.slice(0, 5).map((sector) => (
                  <Badge key={sector} variant="secondary" className="text-[10px] font-normal">
                    {sectorLabel(sector)}
                  </Badge>
                ))}
              </div>
            )}
          </div>
        </div>

        {company.description && (
          <div className="mt-3 pt-3 border-t border-border/50">
            <p className="text-sm text-muted-foreground whitespace-pre-line">{company.description}</p>
          </div>
        )}
      </div>

      {/* People */}
      <div className="mb-5">
        <h2 className="text-sm font-semibold mb-2 flex items-center gap-1.5 text-muted-foreground uppercase tracking-wide">
          <Users className="h-3.5 w-3.5" />
          People
        </h2>

        {people.length === 0 ? (
          <p className="text-sm text-muted-foreground">No people from your network at this company.</p>
        ) : (
          <>
            {currentPeople.length > 0 && (
              <div className="space-y-3 mb-4">
                {currentPeople.map((person, index) => (
                  <CompanyPersonRow key={`${person.person_id}-current-${index}`} person={person} />
                ))}
              </div>
            )}

            {pastPeople.length > 0 && (
              <div className={currentPeople.length > 0 ? "pt-4 border-t" : ""}>
                <h3 className="text-xs font-semibold mb-2 text-muted-foreground uppercase tracking-wide">
                  Past
                </h3>
                <div className="space-y-3">
                  {pastPeople.map((person, index) => (
                    <CompanyPersonRow key={`${person.person_id}-past-${index}`} person={person} />
                  ))}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

// ============================================================================
// Person Row
// ============================================================================

const CompanyPersonRow = ({ person }: { person: LocalCompanyPerson }) => {
  const profileHref = `/contacts/${encodeURIComponent(person.person_id)}`;
  const dateRange = personDateRange(person);

  return (
    <div className="flex items-start gap-3">
      <Avatar className="h-8 w-8 shrink-0 mt-0.5">
        <AvatarImage src={person.profile_picture_url || undefined} />
        <AvatarFallback className="text-[10px]">{personInitials(person)}</AvatarFallback>
      </Avatar>
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-2">
          <a
            href={profileHref}
            className="text-sm font-medium truncate hover:text-primary hover:underline"
            onClick={(e) => {
              e.preventDefault();
              navigateTo(profileHref);
            }}
          >
            {person.full_name || "Unknown"}
          </a>
          {dateRange && (
            <span className="text-xs text-muted-foreground whitespace-nowrap shrink-0">{dateRange}</span>
          )}
        </div>
        {person.position_title ? (
          <p className="text-xs text-muted-foreground truncate">{person.position_title}</p>
        ) : person.headline ? (
          <p className="text-xs text-muted-foreground truncate">{person.headline}</p>
        ) : null}
      </div>
    </div>
  );
};
