import { useState } from "react"

import { readStored, writeStored } from "@/lib/storage"

/** A size the user dragged, kept on this machine under `key`; `initial` until they drag. */
export function useStoredSize(key: string, initial: number): [number, (size: number) => void] {
  const [size, setSize] = useState(
    () =>
      readStored("local", key, (raw) => (typeof raw === "number" && Number.isFinite(raw) ? raw : null)) ??
      initial,
  )
  const keep = (next: number) => {
    setSize(next)
    writeStored("local", key, next)
  }
  return [size, keep]
}
