import { RefreshCcw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { GmailOnboardingSection, LinkedInOnboardingSection, MessagesOnboardingSection } from "./onboarding-v2";

export function LocalOnboardingV2Page() {
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-2xl font-semibold">Onboarding v2</h2>
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
            One-button verticals that run discovery, enrichment, and indexing in a single shot. Each source imports its ingestion steps directly, writes people into the local lake, then reuses the existing indexing wrapper.
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={() => window.location.reload()}>
          <RefreshCcw className="mr-2 h-4 w-4" /> Refresh
        </Button>
      </div>

      <LinkedInOnboardingSection />

      <div className="pt-2">
        <h3 className="text-lg font-semibold">Gmail</h3>
        <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
          One button to sync, discover, enrich, and index your linked Gmail contacts.
        </p>
      </div>
      <GmailOnboardingSection />

      <div className="pt-2">
        <h3 className="text-lg font-semibold">Messages</h3>
        <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
          Import contacts from iMessage and WhatsApp with AI-assisted review.
        </p>
      </div>
      <MessagesOnboardingSection />
    </div>
  );
}
