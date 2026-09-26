import { EmptyState } from "@/components/shared";
import { usePeopleQuery } from "@/hooks/usePeopleQuery";

import { PeopleLoading } from "./PeopleLoading";
import { PeopleShell } from "./PeopleShell";
import { PeopleWorkspace } from "./PeopleWorkspace";

export function PeoplePage() {
  const people = usePeopleQuery();
  if (people.data) return <PeopleWorkspace rows={people.data} />;
  if (people.error) {
    return (
      <PeopleShell
        drawerOpen={false}
        rail={null}
        main={<EmptyState data-empty>{people.error.message}</EmptyState>}
      />
    );
  }
  return <PeopleLoading />;
}
