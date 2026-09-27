/** `value`, or a thrown Error when it is null or undefined: an assertion, never a fallback. */
export function must<T>(value: T | null | undefined, what = "value"): T {
  if (value === null || value === undefined) throw new Error(`missing ${what}`)
  return value
}
