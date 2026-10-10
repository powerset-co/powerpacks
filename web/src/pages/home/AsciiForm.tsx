import { useEffect, useRef } from "react"

import { KNOT, type Form } from "./forms"

const WIDTH = 960
const HEIGHT = 580
const COLS = 120
const ROWS = 58
const GLYPHS = "powerset%$"

/** A sampled torus knot, projected into a character grid with a depth buffer. */
export function AsciiForm({
  form = KNOT,
  paused = false,
  speed = 1,
}: {
  form?: Form
  paused?: boolean
  speed?: number
}) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const elapsed = useRef(0)
  useEffect(() => {
    const element = canvas.current
    if (!element) return
    const context = element.getContext("2d")
    if (!context) return
    const motion = window.matchMedia("(prefers-reduced-motion: reduce)")
    const depth = new Float32Array(COLS * ROWS)
    const light = new Float32Array(COLS * ROWS)
    const pointer = { x: 0, y: 0 }
    const target = { x: 0, y: 0 }
    let frame = 0
    let last = -Infinity
    const move = (event: PointerEvent) => {
      const rect = element.getBoundingClientRect()
      target.x = (event.clientX - rect.left) / rect.width - 0.5
      target.y = (event.clientY - rect.top) / rect.height - 0.5
    }
    const leave = () => {
      target.x = 0
      target.y = 0
    }
    const draw = (now: number) => {
      frame = requestAnimationFrame(draw)
      if (document.hidden || now - last < 33 || ((motion.matches || paused) && last !== -Infinity)) return
      const delta = last === -Infinity ? 0 : Math.min(now - last, 50)
      elapsed.current += delta / 1000
      const blend = 1 - Math.exp(-delta / 160)
      pointer.x += (target.x - pointer.x) * blend
      pointer.y += (target.y - pointer.y) * blend
      last = now
      const t = elapsed.current * speed * 0.7
      depth.fill(-Infinity)
      const yaw = t * 0.38 + pointer.x * 1.1
      const pitch = 0.55 + Math.sin(t * 0.37) * 0.4 + pointer.y * 0.8
      const cy = Math.cos(yaw),
        sy = Math.sin(yaw)
      const cx = Math.cos(pitch),
        sx = Math.sin(pitch)
      for (let i = 0; i < 260; i++) {
        const u = (i / 260) * Math.PI * 2
        for (let j = 0; j < 22; j++) {
          const v = (j / 22) * Math.PI * 2
          const [x, y, z] = form.point(u, v, t)
          const rx = x * cy + z * sy
          const rz = z * cy - x * sy
          const ry = y * cx - rz * sx
          const zz = y * sx + rz * cx
          const scale = 1 / (5 - zz)
          const col = Math.floor(COLS / 2 + rx * scale * 68)
          const row = Math.floor(ROWS / 2 + ry * scale * 44)
          if (col < 0 || col >= COLS || row < 0 || row >= ROWS) continue
          const cell = row * COLS + col
          if (zz <= (depth[cell] ?? -Infinity)) continue
          depth[cell] = zz
          light[cell] = Math.max(0, Math.min(1, 0.45 + zz * 0.16 + Math.sin(v + u - t * 1.8) * 0.24))
        }
      }
      context.clearRect(0, 0, WIDTH, HEIGHT)
      context.font = "11px ui-monospace, Menlo, monospace"
      context.textAlign = "center"
      context.textBaseline = "middle"
      for (let cell = 0; cell < depth.length; cell++) {
        if (depth[cell] === -Infinity) continue
        const brightness = light[cell] ?? 0
        const x = (cell % COLS) * 8 + 4
        const y = Math.floor(cell / COLS) * 10 + 5
        context.fillStyle =
          brightness > 0.82
            ? `rgba(255,220,185,${brightness})`
            : `rgba(242,80,42,${0.22 + brightness * 0.78})`
        context.fillText(GLYPHS.charAt((cell % COLS) % GLYPHS.length), x, y)
      }
    }
    const changeMotion = () => {
      last = -Infinity
    }
    element.addEventListener("pointermove", move)
    element.addEventListener("pointerleave", leave)
    motion.addEventListener("change", changeMotion)
    frame = requestAnimationFrame(draw)
    return () => {
      cancelAnimationFrame(frame)
      element.removeEventListener("pointermove", move)
      element.removeEventListener("pointerleave", leave)
      motion.removeEventListener("change", changeMotion)
    }
  }, [form, paused, speed])
  return (
    <canvas
      ref={canvas}
      width={WIDTH}
      height={HEIGHT}
      aria-hidden
      className="w-full max-w-[960px] shrink-0"
    />
  )
}
