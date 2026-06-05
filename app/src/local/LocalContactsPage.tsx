import { ContactRound } from "lucide-react";

import { Card, CardContent } from "@/components/ui/card";

export function LocalContactsPage() {
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <div className="flex h-9 w-9 items-center justify-center rounded-md bg-primary/10">
          <ContactRound className="h-4 w-4 text-primary" />
        </div>
        <div>
          <h2 className="text-2xl font-semibold">My Contacts</h2>
          <p className="text-sm text-muted-foreground">.powerpacks/network-import/directory.csv</p>
        </div>
      </div>

      <Card>
        <CardContent className="py-10 text-sm text-muted-foreground">
          Directory view placeholder. This page will read the canonical local directory and show attributed Gmail, LinkedIn, iMessage, and WhatsApp contacts.
        </CardContent>
      </Card>
    </div>
  );
}
