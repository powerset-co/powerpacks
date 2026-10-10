export interface Form {
  name: string
  author: "Codex" | "Opus 5.5"
  description: string
  point: (u: number, v: number, t: number) => [number, number, number]
}

const { sin, cos, PI } = Math
export const KNOT: Form = {
  name: "Living knot",
  author: "Codex",
  description: "The original knot, breathing and twisting through itself.",
  point(u, v, t) {
    const r = 1.65 + 0.55 * cos(3 * u + t * 0.65) + (0.32 + 0.1 * sin(4 * u - t * 1.4)) * cos(v)
    return [r * cos(2 * u), r * sin(2 * u), 0.65 * sin(3 * u + t * 0.65) + 0.35 * sin(v)]
  },
}
export const CODEX_FORMS: Form[] = [
  KNOT,
  {
    name: "Infinity engine",
    author: "Codex",
    description: "A liquid figure eight folding between two loops.",
    point(u, v, t) {
      const tube = 0.25 + 0.1 * sin(u * 3 - t)
      return [2 * sin(u) + tube * cos(v), 1.15 * sin(2 * u) + tube * sin(v), 0.7 * cos(u + t * 0.7)]
    },
  },
  {
    name: "Double helix",
    author: "Codex",
    description: "Two interwoven strands climbing an endless spiral.",
    point(u, v, t) {
      const strand = v < PI ? 0 : PI
      const angle = u * 2 + t + strand
      return [(1 + 0.18 * cos(v * 2)) * cos(angle), (u / PI - 1) * 2, (1 + 0.18 * cos(v * 2)) * sin(angle)]
    },
  },
  {
    name: "Metal flower",
    author: "Codex",
    description: "Five rippling petals open, close, and curl toward you.",
    point(u, v, t) {
      const r = (1.25 + 0.65 * cos(5 * u + t * 0.6)) * sin(v / 2)
      return [r * cos(u), r * sin(u), 0.8 * cos(v) + 0.45 * sin(5 * u - t) * sin(v)]
    },
  },
  {
    name: "Möbius current",
    author: "Codex",
    description: "One impossible ribbon, with a wave chasing its edge.",
    point(u, v, t) {
      const w = (v / PI - 1) * 0.65
      const r = 1.5 + w * cos(u / 2 + t * 0.3)
      return [r * cos(u), r * sin(u), w * sin(u / 2 + t * 0.3) + 0.25 * sin(3 * u - t)]
    },
  },
  {
    name: "Event horizon",
    author: "Codex",
    description: "A spinning funnel pulling a sheet of type into its throat.",
    point(u, v, t) {
      const r = 0.2 + v / PI
      const a = u + t * 0.5 + 1.8 / (r + 0.3)
      return [r * cos(a), r * sin(a), 0.8 * cos(v / 2) + 0.2 * sin(6 * u + v * 2 - t * 2)]
    },
  },
  {
    name: "Signal planet",
    author: "Codex",
    description: "A spherical signal with traveling ridges and elastic poles.",
    point(u, v, t) {
      const r = 1.5 + 0.24 * sin(6 * u + 3 * v - t * 1.5) * sin(v / 2)
      return [r * sin(v / 2) * cos(u), r * sin(v / 2) * sin(u), r * cos(v / 2)]
    },
  },
]

export const OPUS_FORMS: Form[] = [
  {
    name: "Harmonic bloom",
    author: "Opus 5.5",
    description: "A breathing orb grows lobes like a pulsing sea urchin.",
    point(u, v, t) {
      const a = v / 2,
        r = 1.6 * (1 + 0.3 * sin(4 * a + t) * cos(3 * u + 1.3 * t))
      return [r * sin(a) * cos(u), r * cos(a), r * sin(a) * sin(u)]
    },
  },
  {
    name: "Möbius ribbon",
    author: "Opus 5.5",
    description: "A half-twist rolls around a single-sided band.",
    point(u, v, t) {
      const s = (v / PI - 1) * 0.6,
        w = u / 2 + t,
        r = 1.5 + s * cos(w)
      return [r * cos(u), s * sin(w), r * sin(u)]
    },
  },
  {
    name: "Klein figure eight",
    author: "Opus 5.5",
    description: "A glassy impossible bottle passes through itself.",
    point(u, v, t) {
      const w = v + t,
        q = 2 + 0.3 * sin(t) + cos(u / 2) * sin(w) - sin(u / 2) * sin(2 * w)
      return [0.65 * q * cos(u), 0.65 * (sin(u / 2) * sin(w) + cos(u / 2) * sin(2 * w)), 0.65 * q * sin(u)]
    },
  },
  {
    name: "Nautilus",
    author: "Opus 5.5",
    description: "An expanding spiral shell curls out along a cone.",
    point(u, v, t) {
      const a = u * 3,
        g = 0.15 * Math.exp(0.1 * a)
      return [
        g * (1.2 + cos(v)) * cos(a + t),
        g * sin(v) + 0.12 * (a - 3 * PI),
        g * (1.2 + cos(v)) * sin(a + t),
      ]
    },
  },
  {
    name: "Sinc ripple",
    author: "Opus 5.5",
    description: "Concentric waves spread across a plane of characters.",
    point(u, v, t) {
      const x = (u / PI - 1) * 2,
        z = (v / PI - 1) * 2,
        r = Math.hypot(x, z)
      return [x, (0.8 * sin(4 * r - 3 * t)) / (1 + 2 * r), z]
    },
  },
  {
    name: "Twisting hourglass",
    author: "Opus 5.5",
    description: "A cage pinches from a cylinder into an hourglass and back.",
    point(u, v, t) {
      const theta = (Math.floor((v / (2 * PI)) * 24) / 24) * 2 * PI,
        s = u / PI - 1,
        a = (s + 1) / 2,
        twist = 1.3 * sin(0.7 * t)
      return [
        1.5 * ((1 - a) * cos(theta - twist) + a * cos(theta + twist)),
        1.8 * s,
        1.5 * ((1 - a) * sin(theta - twist) + a * sin(theta + twist)),
      ]
    },
  },
  {
    name: "DNA ladder",
    author: "Opus 5.5",
    description: "A spinning double helix with rungs threaded between its strands.",
    point(u, v, t) {
      const s = (u / PI - 1) * 1.9,
        rung = v > PI / 2 && v < PI * 1.5
      const y = rung ? Math.round(s / 0.16) * 0.16 : s
      const radius = rung ? cos(v) : Math.sign(cos(v))
      return [0.9 * radius * cos(4 * y + t), y, 0.9 * radius * sin(4 * y + t)]
    },
  },
]
export const FORMS = [...CODEX_FORMS, ...OPUS_FORMS]
