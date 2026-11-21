import { ProxiedLinkedinAvatar, Drawer, DrawerClose } from "@/components/shared"
import { useDrawerSwap } from "@/hooks/useDrawerSwap"
import { teamLikeness } from "@/lib/searches/copy"
import { currentRole, roleLine, shownHeadline, withProfile } from "@/lib/searches/person"
import type { ResultRow } from "@/lib/searches/ranking"

import { SearchFeedbackDialog } from "../dialogs/SearchFeedbackDialog"
import { Career } from "./Career"
import { Evidence } from "./Evidence"
import { JudgeBadges } from "./JudgeBadges"
import { LinkedInLink } from "./LinkedInLink"
import { useMutuals } from "../hooks/useMutuals"
import { usePersonProfile } from "../hooks/useSearchRun"
import { MutualsLine, MutualsSection } from "./Mutuals"
import { ConnectedVia } from "./Operators"
import { RoleLine } from "./ResultRow"
import type { RowContext } from "./RowActions"
import { ScoreCell } from "./ScoreCell"

interface ResultDrawerProps {
  // The row shown; stays set while the drawer animates shut.
  result: ResultRow | null
  open: boolean
  ranked: boolean
  labels: boolean
  context: RowContext
  onClose: () => void
}

// One candidate in the shared drawer: who they are, the feedback flag and Close, the saved
// overall and the judges' labels, then the evidence, their career, who they came through and
// their team likeness. Get mutuals (LinkedIn) sits under their location; its people list under the header.
// The row shows until the person's full profile (headline, about, every role and school) arrives.
export function ResultDrawer({ result, open, ranked, labels, context, onClose }: ResultDrawerProps) {
  const swap = useDrawerSwap(result?.key ?? null, result, open)
  const shown = swap.content
  return (
    <Drawer
      open={open}
      label="Candidate"
      contentKey={swap.shownId}
      leaving={swap.leaving}
      onTransitionEnd={swap.onTransitionEnd}
    >
      {shown ? (
        <DrawerBody result={shown} ranked={ranked} labels={labels} context={context} onClose={onClose} />
      ) : null}
    </Drawer>
  )
}

interface DrawerBodyProps {
  result: ResultRow
  ranked: boolean
  labels: boolean
  context: RowContext
  onClose: () => void
}

function DrawerBody({ result, ranked, labels, context, onClose }: DrawerBodyProps) {
  const { candidate } = result
  const profile = usePersonProfile(context.runId, result.row.person_id)
  const row = profile.data ? withProfile(result.row, profile.data.profile) : result.row
  const current = currentRole(row)
  const role = roleLine(current)
  const headline = shownHeadline(row, current)
  const similarity = candidate?.team_similarity
  const mutuals = useMutuals(row.linkedin_url)
  return (
    <>
      <div className="drawer-top">
        <ProxiedLinkedinAvatar name={row.name} size={40} src={row.avatar_url || undefined} />
        <div className="who">
          <div className="review-name">
            <h2>{row.name}</h2>
            <LinkedInLink row={row} />
          </div>
          <div className="review-role">
            <RoleLine title={current.title} company={current.company} logo={current.logo} />
          </div>
          {headline ? <div className="sub review-headline">{headline}</div> : null}
          {row.location ? <div className="sub review-location">{row.location}</div> : null}
          {row.linkedin_url ? <MutualsLine mutuals={mutuals} /> : null}
        </div>
        <span className="review-tools">
          {candidate ? (
            <SearchFeedbackDialog
              runId={context.runId}
              title={role}
              candidate={candidate}
              submit={context.submit}
              onToast={context.onToast}
            />
          ) : null}
          <DrawerClose onClose={onClose} />
        </span>
      </div>
      {profile.error ? (
        <p role="alert" className="m-0 text-xs text-bad" data-profile-error>
          {profile.error.message}
        </p>
      ) : null}
      <MutualsSection mutuals={mutuals} />
      {ranked ? (
        <div className="review-scores">
          <ScoreCell result={result} />
          <JudgeBadges candidate={candidate} shown={labels} />
        </div>
      ) : null}
      <Evidence result={{ ...result, row }} ranked={ranked} />
      <Career row={row} />
      <ConnectedVia operators={candidate?.network_attribution?.operators ?? []} />
      {similarity ? (
        <section className="review-sections" data-team-similarity>
          <h4>Team similarity</h4>
          <p>{teamLikeness(similarity)}</p>
        </section>
      ) : null}
    </>
  )
}
