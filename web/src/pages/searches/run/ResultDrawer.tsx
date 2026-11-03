import { CompanyLogo, ProxiedLinkedinAvatar, Drawer, DrawerClose } from "@/components/shared"
import { useDrawerSwap } from "@/hooks/useDrawerSwap"
import { teamLikeness } from "@/lib/searches/copy"
import { currentRole, roleLine, shownHeadline } from "@/lib/searches/person"
import type { ResultRow } from "@/lib/searches/ranking"

import { SearchFeedbackDialog } from "../dialogs/SearchFeedbackDialog"
import { Career } from "./Career"
import { Evidence } from "./Evidence"
import { JudgeBadges } from "./JudgeBadges"
import { LinkedInLink } from "./LinkedInLink"
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

// One candidate in the shared drawer: who they are (as the row shows them), the feedback flag and Close, the saved
// overall and the judges' labels, then the evidence, their career, who they came through and
// their team likeness.
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
  const { row, candidate } = result
  const current = currentRole(row)
  const role = roleLine(current)
  const headline = shownHeadline(row, current)
  const similarity = candidate?.team_similarity
  return (
    <>
      <div className="drawer-top">
        <ProxiedLinkedinAvatar
          name={row.name}
          size={40}
          src={row.avatar_url || undefined}
          badge={
            current.company ? (
              <CompanyLogo
                company={current.company}
                src={current.logo || undefined}
                size={14}
                className="avatar-logo"
              />
            ) : null
          }
        />
        <div className="who">
          <div className="review-name">
            <h2>{row.name}</h2>
            <LinkedInLink row={row} />
          </div>
          <div className="review-role">
            <RoleLine title={current.title} company={current.company} />
          </div>
          {headline ? <div className="sub review-headline">{headline}</div> : null}
          {row.location ? <div className="sub review-location">{row.location}</div> : null}
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
      {ranked ? (
        <div className="review-scores">
          <ScoreCell result={result} ranked={ranked} />
          <JudgeBadges candidate={candidate} shown={labels} />
        </div>
      ) : null}
      <Evidence result={result} ranked={ranked} />
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
