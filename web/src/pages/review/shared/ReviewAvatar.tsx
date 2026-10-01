import { Avatar } from "@/components/shared"
import { avatarUrl } from "@/lib/api/review"
import { avatarKey, avatarName } from "@/lib/review/person"
import type { ReviewCandidate, ReviewPerson } from "@/types/review"

interface ReviewAvatarProps {
  person: ReviewPerson
  /** The profile whose picture and name the avatar draws; a researched one has no picture. */
  candidate: ReviewCandidate | null
}

// The `avatar` macro: initials, with the candidate's picture over them once it loads (nothing
// when it fails). The shared Avatar draws it; .avatar (styles/base.css) sets the old sizes.
export function ReviewAvatar({ person, candidate }: ReviewAvatarProps) {
  const key = avatarKey(candidate)
  return (
    <span className="avatar">
      <Avatar name={avatarName(person, candidate)} src={key ? avatarUrl(key) : undefined} size={40} />
    </span>
  )
}
