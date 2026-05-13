import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/** Round a count to an approximate display value: 641→600, 1531→1500, 12345→12000 */
export function approxCount(n: number): number {
  if (n >= 10000) return Math.round(n / 1000) * 1000;
  if (n >= 1000) return Math.round(n / 500) * 500;
  if (n >= 100) return Math.round(n / 100) * 100;
  if (n >= 10) return Math.round(n / 10) * 10;
  return n;
}
