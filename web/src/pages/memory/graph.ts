// Canvas renderer adapted from our Memory Atlas prototype: depth, hover, animated focus.
import type { MemoryGraphData, MemoryGraphNode } from "@/types/memory"

interface Node extends MemoryGraphNode {
  x: number
  y: number
  z: number
  sx: number
  sy: number
  radius: number
  alpha: number
}
interface Camera {
  x: number
  y: number
  zoom: number
}
const colors = { person: "#ffb37a", topic: "#82b5e9" }
const clamp = (value: number, low: number, high: number) => Math.max(low, Math.min(high, value))
function random(id: string, salt = 0) {
  let hash = 2166136261 + salt
  for (const char of id) hash = Math.imul(hash ^ char.charCodeAt(0), 16777619)
  return (hash >>> 0) / 4294967296
}

export class MemoryGraph {
  private ctx: CanvasRenderingContext2D
  private nodes: Node[]
  private byId: Map<string, Node>
  private edges: { a: Node; b: Node }[] = []
  private adjacent = new Map<string, Set<string>>()
  private observer: ResizeObserver
  private listeners = new AbortController()
  private frame = 0
  private width = 1
  private height = 1
  private dpr = 1
  private scale = 1
  private extent = 1
  private angle = 0
  private tilt = 0.12
  private camera: Camera = { x: 0, y: 0, zoom: 1 }
  private target = { ...this.camera }
  private matches = new Set<string>()
  private matchNeighbors = new Set<string>()
  private selected: string | null = null
  private hover: Node | null = null
  private hoverSince = 0
  private elapsed = 0
  private lastTime = 0
  private reduced = matchMedia("(prefers-reduced-motion: reduce)").matches
  motion = !this.reduced
  depth = true

  constructor(
    private canvas: HTMLCanvasElement,
    data: MemoryGraphData,
    onSelect: (id: string) => void,
  ) {
    const context = canvas.getContext("2d")
    if (!context) throw new Error("Canvas is unavailable")
    this.ctx = context
    this.nodes = data.nodes.map((node) => ({ ...node, x: 0, y: 0, z: 0, sx: 0, sy: 0, radius: 0, alpha: 1 }))
    this.byId = new Map(this.nodes.map((node) => [node.id, node]))
    for (const node of this.nodes) this.adjacent.set(node.id, new Set())
    for (const edge of data.edges) {
      const a = this.byId.get(edge.source),
        b = this.byId.get(edge.target)
      if (!a || !b) continue
      this.edges.push({ a, b })
      this.adjacent.get(a.id)?.add(b.id)
      this.adjacent.get(b.id)?.add(a.id)
    }
    this.layout()
    this.observer = new ResizeObserver(() => this.resize())
    this.observer.observe(canvas)
    this.resize()
    this.bind(onSelect)
    this.frame = requestAnimationFrame((time) => this.draw(time))
  }
  destroy() {
    cancelAnimationFrame(this.frame)
    this.observer.disconnect()
    this.listeners.abort()
  }
  private layout() {
    const hubs = this.nodes.filter((node) => node.kind === "topic")
    const depth = 75 * Math.sqrt(hubs.length + 1) * 0.65
    hubs.forEach((node, index) => {
      const angle = index * 2.399963,
        radius = 75 * Math.sqrt(index + 1)
      node.x = Math.cos(angle) * radius
      node.y = Math.sin(angle) * radius
      node.z = (random(node.id, 3) * 2 - 1) * depth * Math.sqrt(1 - (index + 1) / (hubs.length + 1))
    })
    for (const node of this.nodes.filter((node) => node.kind === "person")) {
      const hub = [...(this.adjacent.get(node.id) ?? [])]
        .map((id) => this.byId.get(id))
        .find((n) => n?.kind === "topic")
      const angle = random(node.id) * Math.PI * 2
      const radius = hub
        ? 30 + Math.sqrt(random(node.id, 8)) * 100
        : (75 * Math.sqrt(hubs.length + 1) + 100) * (0.7 + random(node.id, 8) * 0.3)
      node.x = (hub?.x ?? 0) + Math.cos(angle) * radius
      node.y = (hub?.y ?? 0) + Math.sin(angle) * radius
      node.z = (hub?.z ?? 0) + (random(node.id, 4) - 0.5) * 180
    }
    this.extent = Math.max(100, ...this.nodes.map((node) => Math.hypot(node.x, node.y, node.z))) + 100
  }
  private resize() {
    this.width = this.canvas.clientWidth
    this.height = this.canvas.clientHeight
    this.dpr = Math.min(devicePixelRatio, 2)
    this.canvas.width = Math.round(this.width * this.dpr)
    this.canvas.height = Math.round(this.height * this.dpr)
    this.scale = Math.min(this.width, this.height) / (this.extent * 2.4)
  }
  private project(node: Node) {
    const angle = this.depth ? this.angle : 0,
      tilt = this.depth ? this.tilt : 0
    const z = node.z * Math.cos(angle) - node.x * Math.sin(angle)
    const rotatedZ = node.y * Math.sin(tilt) + z * Math.cos(tilt)
    const perspective = this.depth ? 2400 / Math.max(700, 2400 + rotatedZ) : 1
    return {
      x: (node.x * Math.cos(angle) + node.z * Math.sin(angle)) * perspective,
      y:
        (node.y * Math.cos(tilt) -
          z * Math.sin(tilt) +
          (this.motion ? Math.sin(this.elapsed * 0.3 + random(node.id) * 6) * 3 : 0)) *
        perspective,
      perspective,
    }
  }
  highlight(ids: string[]) {
    this.matches = new Set(ids)
    this.matchNeighbors = new Set(ids.flatMap((id) => [...(this.adjacent.get(id) ?? [])]))
  }
  select(id: string | null) {
    this.selected = id && this.byId.has(id) ? id : null
    if (!this.selected) {
      this.target = { x: 0, y: 0, zoom: 1 }
      return
    }
    const ids = new Set([this.selected, ...(this.adjacent.get(this.selected) ?? [])])
    const points = this.nodes.filter((node) => ids.has(node.id)).map((node) => this.project(node))
    const left = Math.min(...points.map((p) => p.x)),
      right = Math.max(...points.map((p) => p.x))
    const top = Math.min(...points.map((p) => p.y)),
      bottom = Math.max(...points.map((p) => p.y))
    const fit = Math.min((this.width - 80) / (right - left + 180), (this.height - 100) / (bottom - top + 180))
    this.target = { x: (left + right) / 2, y: (top + bottom) / 2, zoom: clamp(fit / this.scale, 0.55, 6) }
  }
  zoom(factor: number) {
    this.target.zoom = clamp(this.target.zoom * factor, 0.5, 12)
  }
  private hit(x: number, y: number) {
    let best: Node | null = null,
      distance = Infinity
    for (const node of this.nodes) {
      const d = Math.hypot(node.sx - x, node.sy - y)
      if (d < Math.max(8, node.radius + 4) && d < distance) {
        best = node
        distance = d
      }
    }
    return best
  }
  private bind(onSelect: (id: string) => void) {
    const options = { signal: this.listeners.signal }
    let drag: { x: number; y: number; moved: number } | null = null
    this.canvas.addEventListener(
      "pointerdown",
      (event) => {
        drag = { x: event.offsetX, y: event.offsetY, moved: 0 }
        this.canvas.setPointerCapture(event.pointerId)
      },
      options,
    )
    this.canvas.addEventListener(
      "pointermove",
      (event) => {
        if (drag) {
          const dx = event.offsetX - drag.x,
            dy = event.offsetY - drag.y
          drag.moved += Math.abs(dx) + Math.abs(dy)
          drag.x = event.offsetX
          drag.y = event.offsetY
          if (this.depth && !event.shiftKey) {
            this.angle += dx * 0.004
            this.tilt = clamp(this.tilt + dy * 0.004, -1.2, 1.2)
          } else {
            this.target.x -= dx / (this.scale * this.camera.zoom)
            this.target.y -= dy / (this.scale * this.camera.zoom)
          }
          this.camera = { ...this.target }
        } else {
          const hovered = this.hit(event.offsetX, event.offsetY)
          if (hovered !== this.hover) this.hoverSince = performance.now()
          this.hover = hovered
          this.canvas.style.cursor = hovered ? "pointer" : "grab"
          this.canvas.title = hovered?.label ?? ""
        }
      },
      options,
    )
    this.canvas.addEventListener(
      "pointerup",
      (event) => {
        if (drag && drag.moved < 5) {
          const node = this.hit(event.offsetX, event.offsetY)
          if (node) onSelect(node.id)
        }
        drag = null
      },
      options,
    )
    this.canvas.addEventListener(
      "pointercancel",
      () => {
        drag = null
      },
      options,
    )
    this.canvas.addEventListener(
      "pointerleave",
      () => {
        this.hover = null
      },
      options,
    )
    this.canvas.addEventListener(
      "wheel",
      (event) => {
        event.preventDefault()
        this.zoom(Math.exp(-event.deltaY * 0.001))
      },
      { ...options, passive: false },
    )
  }
  private draw(time: number) {
    const dt = this.lastTime ? Math.min(time - this.lastTime, 50) : 16
    this.lastTime = time
    this.elapsed += dt / 1000
    if (this.motion && !this.selected && !this.hover) this.angle += dt * 0.000009
    const easing = this.reduced ? 1 : 1 - Math.exp(-dt / 120)
    for (const key of ["x", "y", "zoom"] as const)
      this.camera[key] += (this.target[key] - this.camera[key]) * easing
    const focus = this.hover && time - this.hoverSince >= 120 ? this.hover.id : this.selected
    const neighbors = focus ? this.adjacent.get(focus) : null
    const groupFocus = !focus && this.matches.size > 0
    const active = (node: Node) =>
      focus
        ? node.id === focus || !!neighbors?.has(node.id)
        : !groupFocus || this.matches.has(node.id) || this.matchNeighbors.has(node.id)
    const edgeActive = (a: Node, b: Node) =>
      focus ? a.id === focus || b.id === focus : this.matches.has(a.id) || this.matches.has(b.id)
    this.canvas.setAttribute(
      "aria-description",
      focus
        ? `Highlighting ${this.byId.get(focus)?.label ?? "memory"} and ${neighbors?.size ?? 0} connections`
        : groupFocus
          ? `Highlighting ${this.matches.size} search matches`
          : "All memories",
    )
    const ctx = this.ctx
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0)
    ctx.globalAlpha = 1
    ctx.fillStyle = "#141619"
    ctx.fillRect(0, 0, this.width, this.height)
    for (const node of this.nodes) {
      const p = this.project(node)
      node.sx = this.width / 2 + (p.x - this.camera.x) * this.scale * this.camera.zoom
      node.sy = this.height / 2 + (p.y - this.camera.y) * this.scale * this.camera.zoom
      node.radius = (node.kind === "person" ? 1.8 : 3.3) * Math.sqrt(this.camera.zoom) * p.perspective
    }
    ctx.strokeStyle = "#8194af"
    ctx.lineWidth = 0.6
    ctx.globalAlpha = focus || groupFocus ? 0.025 : 0.11
    ctx.beginPath()
    for (const { a, b } of this.edges) {
      ctx.moveTo(a.sx, a.sy)
      ctx.lineTo(b.sx, b.sy)
    }
    ctx.stroke()
    if (focus || groupFocus) {
      ctx.strokeStyle = "#b9c8e0"
      ctx.lineWidth = 1
      ctx.globalAlpha = 0.6
      ctx.beginPath()
      for (const { a, b } of this.edges) {
        if (!edgeActive(a, b)) continue
        ctx.moveTo(a.sx, a.sy)
        ctx.lineTo(b.sx, b.sy)
      }
      ctx.stroke()
      if (this.motion)
        for (const { a, b } of this.edges) {
          if (!edgeActive(a, b)) continue
          const progress = (this.elapsed * 0.18 + random(a.id + b.id)) % 1
          ctx.globalAlpha = Math.sin(progress * Math.PI) * 0.7
          ctx.fillStyle = "#eee7ff"
          ctx.beginPath()
          ctx.arc(a.sx + (b.sx - a.sx) * progress, a.sy + (b.sy - a.sy) * progress, 1.4, 0, Math.PI * 2)
          ctx.fill()
        }
    }
    for (const node of this.nodes) {
      node.alpha += ((active(node) ? 1 : 0.08) - node.alpha) * easing
      ctx.globalAlpha = node.alpha
      ctx.fillStyle = colors[node.kind]
      ctx.shadowColor = colors[node.kind]
      ctx.shadowBlur = active(node) ? 10 : 0
      ctx.beginPath()
      ctx.arc(node.sx, node.sy, node.radius * (node.id === focus ? 1.6 : 1), 0, Math.PI * 2)
      ctx.fill()
    }
    ctx.shadowBlur = 0
    const labels = this.nodes.filter((node) => (focus || groupFocus ? active(node) : node.kind === "topic"))
    labels.sort((a, b) => Number(b.id === focus) - Number(a.id === focus))
    const boxes: { x: number; y: number; w: number }[] = []
    ctx.font = "11px -apple-system, sans-serif"
    ctx.textAlign = "center"
    for (const node of labels) {
      if (node.sx < 35 || node.sx > this.width - 35 || node.sy < 60 || node.sy > this.height - 45) continue
      const label = node.label.length > 32 ? node.label.slice(0, 31) + "…" : node.label
      const w = ctx.measureText(label).width + 8,
        y = node.sy + node.radius + 15,
        x = node.sx - w / 2
      if (boxes.some((box) => x < box.x + box.w && x + w > box.x && Math.abs(y - box.y) < 16)) continue
      boxes.push({ x, y, w })
      ctx.globalAlpha = node.id === focus ? 1 : 0.8
      ctx.fillStyle = "#141619"
      ctx.fillRect(x, y - 11, w, 15)
      ctx.fillStyle = "#e4dfd9"
      ctx.fillText(label, node.sx, y)
    }
    ctx.globalAlpha = 1
    this.frame = requestAnimationFrame((next) => this.draw(next))
  }
}
