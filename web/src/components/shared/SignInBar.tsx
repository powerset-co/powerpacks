import { closeSignIn, useSignIn } from "@/lib/signin"

import { Button } from "../ui/button"

/** The strip above the desktop app's sign-in pane: who it is for, and Cancel. */
export function SignInBar() {
  const signIn = useSignIn()
  if (signIn === null) return null
  return (
    <div
      role="status"
      className="fixed inset-x-0 top-topbar z-50 flex h-11 items-center gap-3 border-b border-line bg-card px-5"
    >
      <span aria-hidden className="size-2 rounded-full bg-primary" />
      <span className="min-w-0 flex-1 truncate text-[13px]">
        <span className="font-semibold">Signing in to {signIn.title}.</span>{" "}
        <span className="text-muted-foreground">Powerpacks continues as soon as you finish.</span>
      </span>
      <Button size="sm" variant="ghost" onClick={() => void closeSignIn()}>
        Cancel
      </Button>
    </div>
  )
}
