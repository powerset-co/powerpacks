import { Avatar, Drawer, DrawerClose } from "@/components/shared"
import { useDrawerSwap } from "@/hooks/useDrawerSwap"
import { teamLikeness } from "@/lib/searches/copy"
import type { ResultRow } from "@/lib/searches/ranking"

import { Career } from "./Career"
import { Evidence } from "./Evidence"
import { JudgeBadges } from "./JudgeBadges"
import { LinkedInLink } from "./LinkedInLink"
import { ConnectedVia } from "./Operators"
import { ScoreCell } from "./ScoreCell"

interface ResultDrawerProps {
  // The row shown; stays set while the drawer animates shut.
  result: ResultRow | null
  open: boolean
  ranked: boolean
  labels: boolean
  onClose: () => void
}

// One candidate in the shared drawer: who they are, the saved overall and the judges' labels,
// then the evidence, their career, who they came through and their team likeness.
export function ResultDrawer({ result, open, ranked, labels, onClose }: ResultDrawerProps) {
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
      {shown ? <DrawerBody result={shown} ranked={ranked} labels={labels} onClose={onClose} /> : null}
    </Drawer>
  )
}

interface DrawerBodyProps {
  result: ResultRow
  ranked: boolean
  labels: boolean
  onClose: () => void
}

function DrawerBody({ result, ranked, labels, onClose }: DrawerBodyProps) {
  const { row, candidate } = result
  const role = [row.title, row.company].filter(Boolean).join(" · ")
  const similarity = candidate?.team_similarity
  return (
    <>
      <div className="drawer-top">
        <Avatar name={row.name} size={40} src={row.avatar_url || undefined} />
        <div className="who">
          <h2>{row.name}</h2>
          <div className="sub review-role">
            <LinkedInLink row={row} />
            {role || "Current role unknown"}
          </div>
          {row.location ? <div className="sub">{row.location}</div> : null}
        </div>
        <DrawerClose onClose={onClose} />
      </div>
      <div className="review-scores">
        <ScoreCell result={result} ranked={ranked} />
        <JudgeBadges candidate={candidate} shown={labels} />
      </div>
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
