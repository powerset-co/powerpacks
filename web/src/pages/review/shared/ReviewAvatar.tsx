import { Avatar } from "@/components/shared"
import { avatarName } from "@/lib/review/person"
import type { ReviewCandidate, ReviewPerson } from "@/types/review"

interface ReviewAvatarProps {
  person: ReviewPerson
  /** The profile whose picture and name the avatar draws; a researched one has no picture. */
  candidate: ReviewCandidate | null
}

// A person's avatar: initials, with the profile's own picture over them once it loads (nothing
// when it fails or the profile has none). The shared Avatar draws it; .avatar (styles/base.css)
// sets its size.
export function ReviewAvatar({ person, candidate }: ReviewAvatarProps) {
  const picture = candidate?.avatar_url ?? ""
  return (
    <span className="avatar">
      <Avatar name={avatarName(person, candidate)} src={picture === "" ? undefined : picture} size={40} />
    </span>
  )
}
