import { useEffect, useRef } from "react"

// One character's cell, in canvas pixels; the grid is the home page's unless a page asks smaller.
const CELL_W = 8
const CELL_H = 10
const COLS = 120
const ROWS = 58
const GLYPHS = "powerset%$"

const { sin, cos } = Math

/** The living knot: breathing and twisting through itself. */
function knot(u: number, v: number, t: number): [number, number, number] {
  const r = 1.65 + 0.55 * cos(3 * u + t * 0.65) + (0.32 + 0.1 * sin(4 * u - t * 1.4)) * cos(v)
  const bend = 0.1 * sin(2 * u + t * 0.35)
  return [
    r * cos(2 * u) * (1 + 0.035 * sin(t * 0.4)),
    r * sin(2 * u) + bend,
    0.65 * sin(3 * u + t * 0.65) + 0.35 * sin(v) + bend,
  ]
}

interface AsciiFormProps {
  cols?: number
  rows?: number
  className?: string
}

/** A sampled torus knot, projected into a character grid with a depth buffer. A smaller grid
 *  draws the same knot in fewer, coarser characters. */
export function AsciiForm({ cols = COLS, rows = ROWS, className = "w-full max-w-[960px]" }: AsciiFormProps) {
  const width = cols * CELL_W
  const height = rows * CELL_H
  const canvas = useRef<HTMLCanvasElement>(null)
  const elapsed = useRef(0)
  useEffect(() => {
    const element = canvas.current
    if (!element) return
    const context = element.getContext("2d")
    if (!context) return
    const motion = window.matchMedia("(prefers-reduced-motion: reduce)")
    const depth = new Float32Array(cols * rows)
    const light = new Float32Array(cols * rows)
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
      if (document.hidden || now - last < 33 || (motion.matches && last !== -Infinity)) return
      const delta = last === -Infinity ? 0 : Math.min(now - last, 50)
      elapsed.current += delta / 1000
      const blend = 1 - Math.exp(-delta / 160)
      pointer.x += (target.x - pointer.x) * blend
      pointer.y += (target.y - pointer.y) * blend
      last = now
      const t = elapsed.current * 0.7
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
          const [x, y, z] = knot(u, v, t)
          const rx = x * cy + z * sy
          const rz = z * cy - x * sy
          const ry = y * cx - rz * sx
          const zz = y * sx + rz * cx
          const scale = 1 / (5 - zz)
          const col = Math.floor(cols / 2 + ((rx * scale * 68) / COLS) * cols)
          const row = Math.floor(rows / 2 + ((ry * scale * 44) / ROWS) * rows)
          if (col < 0 || col >= cols || row < 0 || row >= rows) continue
          const cell = row * cols + col
          if (zz <= (depth[cell] ?? -Infinity)) continue
          depth[cell] = zz
          light[cell] = Math.max(0, Math.min(1, 0.45 + zz * 0.16 + Math.sin(v + u - t * 1.8) * 0.24))
        }
      }
      context.clearRect(0, 0, width, height)
      context.font = "11px ui-monospace, Menlo, monospace"
      context.textAlign = "center"
      context.textBaseline = "middle"
      for (let cell = 0; cell < depth.length; cell++) {
        if (depth[cell] === -Infinity) continue
        const brightness = light[cell] ?? 0
        const x = (cell % cols) * CELL_W + CELL_W / 2
        const y = Math.floor(cell / cols) * CELL_H + CELL_H / 2
        context.fillStyle =
          brightness > 0.82
            ? `rgba(255,220,185,${brightness})`
            : `rgba(242,80,42,${0.22 + brightness * 0.78})`
        context.fillText(GLYPHS.charAt((cell % cols) % GLYPHS.length), x, y)
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
  }, [cols, rows, width, height])
  return <canvas ref={canvas} width={width} height={height} aria-hidden className={`shrink-0 ${className}`} />
}
