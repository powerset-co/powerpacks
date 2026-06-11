import { useEffect, useState } from "react";
import { ArrowLeft, MapPin, GraduationCap, Briefcase, Phone, Mail, FileText } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { SocialLinks } from "@/components/ui/social-link";
import {
  fetchPersonDetails,
  type LocalPersonDetailsResponse,
  type LocalPersonPosition,
} from "./personApi";

// Position/education epochs come back from the local API as unix seconds.
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

function positionLocation(position: LocalPersonPosition): string {
  return [position.city, position.state, position.country].filter(Boolean).join(", ");
}

export function LocalPersonDetailsPage({ personId }: { personId: string }) {
  const [details, setDetails] = useState<LocalPersonDetailsResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    setError(null);
    setDetails(null);
    if (!personId) {
      setIsLoading(false);
      return () => undefined;
    }
    fetchPersonDetails(personId)
      .then((response) => {
        if (!cancelled) setDetails(response);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load person details");
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [personId]);

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
        <Button onClick={() => navigateTo("/contacts")}>Back to Directory</Button>
      </div>
    );
  }

  if (!details) {
    return (
      <div className="text-center py-12">
        <h2 className="text-2xl font-bold mb-2">Person Not Found</h2>
        <p className="text-muted-foreground mb-4">
          The person you're looking for doesn't exist or has been removed.
        </p>
        <Button onClick={() => navigateTo("/contacts")}>Back to Directory</Button>
      </div>
    );
  }

  const { profile, positions, education } = details;

  const headline = profile.headline || "";
  const currentPosition = positions.find((position) => position.is_current);
  const currentTitle = profile.current_title || currentPosition?.position_title || "";
  const currentCompany = profile.current_company || currentPosition?.company_name || "";
  const phoneNumbers = profile.all_phones.length
    ? profile.all_phones
    : profile.primary_phone
      ? [profile.primary_phone]
      : [];
  const emails = profile.all_emails.length
    ? profile.all_emails
    : profile.primary_email
      ? [profile.primary_email]
      : [];
  const summaryText = details.summary?.summary || profile.summary || "";

  return (
    <div className="p-4 md:p-6 max-w-3xl mx-auto">
      <Button
        variant="ghost"
        size="sm"
        className="flex items-center gap-1 mb-3 -ml-2 text-xs"
        onClick={() => navigateTo("/contacts")}
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        Back
      </Button>

      {/* Inset Card — identity + contact */}
      <div className="rounded-lg border bg-muted/30 p-4 mb-6 relative">
        {/* Row 1: Avatar + identity */}
        <div className="flex items-start gap-3">
          <Avatar className="h-11 w-11 shrink-0 mt-0.5">
            <AvatarImage src={profile.profile_picture_url || ""} alt={`${profile.first_name} ${profile.last_name}`} />
            <AvatarFallback className="text-sm font-semibold">
              {profile.first_name?.[0]}{profile.last_name?.[0]}
            </AvatarFallback>
          </Avatar>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="text-base font-semibold">
                {profile.full_name || `${profile.first_name} ${profile.last_name}`}
              </span>
              <SocialLinks
                linkedinUrl={profile.linkedin_url}
                xTwitterHandle={profile.x_twitter_handle}
                size={14}
              />
            </div>
            {currentTitle ? (
              <p className="text-sm text-muted-foreground truncate">
                {currentTitle}{currentCompany && <> · {currentCompany}</>}
              </p>
            ) : headline ? (
              <p className="text-sm text-muted-foreground truncate">{headline}</p>
            ) : null}
            {profile.location_raw && (
              <p className="text-xs text-muted-foreground/60 flex items-center gap-1 mt-0.5">
                <MapPin className="h-3 w-3 shrink-0" />{profile.location_raw}
              </p>
            )}
          </div>
        </div>

        {/* Row 2: Contact — separated by subtle border */}
        {(phoneNumbers.length > 0 || emails.length > 0) && (
          <div className="mt-3 pt-3 border-t border-border/50">
            {phoneNumbers.map((phone) => (
              <div key={phone} className="mb-1.5 flex items-center gap-1.5">
                <Phone className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
                <a
                  href={`tel:${phone}`}
                  className="text-sm text-muted-foreground hover:text-foreground hover:underline"
                  onClick={(e) => e.stopPropagation()}
                >
                  {phone}
                </a>
              </div>
            ))}
            {emails.map((email) => (
              <div key={email} className="mb-1.5 flex items-center gap-1.5 last:mb-0">
                <Mail className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
                <a
                  href={`mailto:${email}`}
                  className="text-sm text-muted-foreground hover:text-foreground hover:underline"
                  onClick={(e) => e.stopPropagation()}
                >
                  {email}
                </a>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Summary — clean prose only; skill/endorsement chips intentionally not rendered */}
      {summaryText && (
        <div className="mb-5 pb-4 border-b">
          <h2 className="text-sm font-semibold mb-2 flex items-center gap-1.5 text-muted-foreground uppercase tracking-wide">
            <FileText className="h-3.5 w-3.5" />
            Summary
          </h2>
          <p className="text-sm text-muted-foreground whitespace-pre-line">{summaryText}</p>
        </div>
      )}

      {/* Work Experience */}
      {positions.length > 0 && (
        <div className="mb-5 pb-4 border-b">
          <h2 className="text-sm font-semibold mb-2 flex items-center gap-1.5 text-muted-foreground uppercase tracking-wide">
            <Briefcase className="h-3.5 w-3.5" />
            Experience
          </h2>
          <div className="space-y-3">
            {positions.map((exp, index) => {
              const jobTitle = exp.position_title || "Unknown Role";
              const companyName = exp.company_name;
              const startEpoch = exp.start_date_epoch;
              const endEpoch = exp.end_date_epoch;
              const isCurrent = exp.is_current || (!endEpoch && !!startEpoch);
              const location = positionLocation(exp);

              return (
                <div key={exp.position_id || index} className="pl-0">
                  <div className="flex items-baseline justify-between gap-2">
                    <div className="min-w-0">
                      <span className="text-sm font-medium">{jobTitle}</span>
                      {companyName && (() => {
                        const companyUrl = exp.company_domain
                          ? (exp.company_domain.startsWith("http") ? exp.company_domain : `https://${exp.company_domain}`)
                          : exp.company_linkedin_url;
                        return (
                          <span className="text-sm text-muted-foreground">
                            {" · "}
                            {companyUrl ? (
                              <a href={companyUrl} target="_blank" rel="noopener noreferrer" className="underline hover:text-primary transition-colors">
                                {companyName}
                              </a>
                            ) : companyName}
                          </span>
                        );
                      })()}
                    </div>
                    <span className="text-xs text-muted-foreground whitespace-nowrap shrink-0">
                      {formatEpoch(startEpoch)}
                      {(startEpoch || endEpoch) ? " – " : ""}
                      {endEpoch ? formatEpoch(endEpoch) : (isCurrent ? "Present" : "")}
                    </span>
                  </div>
                  {location && (
                    <p className="text-xs text-muted-foreground">{location}</p>
                  )}
                  {(exp.description || null) && (
                    <p className="text-xs text-muted-foreground mt-0.5 line-clamp-2">{exp.description}</p>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Education */}
      {education.length > 0 && (
        <div className="mb-5">
          <h2 className="text-sm font-semibold mb-2 flex items-center gap-1.5 text-muted-foreground uppercase tracking-wide">
            <GraduationCap className="h-3.5 w-3.5" />
            Education
          </h2>
          <div className="space-y-3">
            {education.map((edu, index) => {
              const schoolName = edu.school_name || "Unknown School";
              const startYear = edu.start_year;
              const endYear = edu.end_year || edu.graduation_year;

              return (
                <div key={index} className="pl-0">
                  <div className="flex items-baseline justify-between gap-2">
                    <div className="min-w-0">
                      <span className="text-sm font-medium">{schoolName}</span>
                      {(edu.degree || edu.field_of_study) && (
                        <span className="text-sm text-muted-foreground">
                          {" · "}{[edu.degree, edu.field_of_study].filter(Boolean).join(" in ")}
                        </span>
                      )}
                    </div>
                    {(startYear || endYear) && (
                      <span className="text-xs text-muted-foreground whitespace-nowrap shrink-0">
                        {startYear || ""}
                        {(startYear || endYear) ? " – " : ""}
                        {endYear || ""}
                      </span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
