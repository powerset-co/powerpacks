// How Deep Context works: what stays on the Mac, what goes out to the internet and what comes back,
// drawn as a nine-step animation for the Accounts dialog. The shared preview page is
// claude.ai/artifact/NQbvNCv5gjQAwFbriZwVPm; keep both in step.
//
// Every move runs through the Web Animations API on transform and opacity. Curves are sampled into
// keyframes; the springy settle is a CSS linear() curve.
import openaiLogo from "@/assets/providers/openai.png?inline"
import parallelLogo from "@/assets/providers/parallel.svg?inline"

export interface Step {
  title: string
  text: string
  foot: string
}

const PROXY = "Goes through Powerset's proxy. We pass it along and don't keep a copy."
export const STEPS: readonly Step[] = [
  {
    title: "Your messages stay on your Mac",
    text: "Gmail, iMessage, WhatsApp and your LinkedIn connections are read locally. Nothing is uploaded to read them.",
    foot: "Reading happens on your Mac. Nothing leaves at this step.",
  },
  {
    title: "Short excerpts, not whole inboxes",
    text: "For each person, we pull a few short excerpts from your conversations and save them on your Mac.",
    foot: "Excerpts are saved in your people database, on your Mac.",
  },
  {
    title: "OpenAI writes a dossier",
    text: "A copy of the excerpts goes to OpenAI, which sends back a short dossier.",
    foot: "Sent with storage off, so OpenAI doesn't keep it for later. We don't log it on our side.",
  },
  {
    title: "Only the professional picture",
    text: "The dossier keeps work, school and topics. Personal details are left out.",
    foot: "",
  },
  {
    title: "OpenAI adds tags",
    text: "From the dossier, OpenAI returns tags: how you know them, how warm it is, whether they're worth keeping.",
    foot: "Same as the dossier: storage off, nothing logged on our side.",
  },
  {
    title: "Parallel looks them up",
    text: "A short lookup goes to Parallel. It returns their LinkedIn, or a stand-in profile built from the web.",
    foot: PROXY,
  },
  {
    title: "Fetch the public profile",
    text: "Only the profile link goes out. Their public LinkedIn profile comes back.",
    foot: PROXY,
  },
  {
    title: "Is it the same person?",
    text: "The dossier and the profile go to OpenAI side by side. Only a confident match is kept.",
    foot: "Storage off at OpenAI, nothing logged on our side.",
  },
  {
    title: "Everything is saved on your Mac",
    text: "Dossiers, tags, profiles and matches live in your people database on your Mac. That's what you search.",
    foot: "To build search, your people list (names, emails, phones, profiles; never messages) is processed on Modal through Powerset. It's a faster way to compute; you can run it on your own machine instead.",
  },
]

const LINKEDIN_PATH =
  "M20.5 2h-17A1.5 1.5 0 002 3.5v17A1.5 1.5 0 003.5 22h17a1.5 1.5 0 001.5-1.5v-17A1.5 1.5 0 0020.5 2zM8 19H5v-9h3zM6.5 8.25A1.75 1.75 0 118.3 6.5a1.78 1.78 0 01-1.8 1.75zM19 19h-3v-4.74c0-1.42-.6-1.93-1.38-1.93A1.74 1.74 0 0013 14.19a.66.66 0 000 .14V19h-3v-9h2.9v1.3a3.11 3.11 0 012.7-1.4c1.55 0 3.36.86 3.36 3.66z"
const WHATSAPP_PATH =
  "M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413Z"

// The stage, drawn in HTML on a 1000 x 560 board that zooms to fit. Icons are the app's own
// (components/shared/icons/channels.tsx, assets/providers).
const icon = (name: string, style = "") =>
  `<svg class="dck-i"${style ? ` style="${style}"` : ""}><use href="#dck-${name}"/></svg>`
const SYMBOLS = `
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <symbol id="dck-gmail" viewBox="0 0 24 24"><rect width="20" height="16" x="2" y="4" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/></symbol>
  <symbol id="dck-imessage" viewBox="0 0 24 24"><path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/></symbol>
  <symbol id="dck-whatsapp" viewBox="0 0 24 24"><path fill="currentColor" stroke="none" d="${WHATSAPP_PATH}"/></symbol>
  <symbol id="dck-linkedin" viewBox="0 0 24 24"><path fill="currentColor" stroke="none" d="${LINKEDIN_PATH}"/></symbol>
  <symbol id="dck-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="6"/><path d="m20 20-4.5-4.5"/></symbol>
  <symbol id="dck-link" viewBox="0 0 24 24"><path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/></symbol>
  <symbol id="dck-globe" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3z"/></symbol>
  <symbol id="dck-laptop" viewBox="0 0 24 24"><rect x="4" y="5" width="16" height="11" rx="1.5"/><path d="M2 19h20"/></symbol>
  <symbol id="dck-check" viewBox="0 0 24 24"><path d="m5 12.5 4.5 4.5L19 7.5"/></symbol>
  <symbol id="dck-eyeoff" viewBox="0 0 24 24"><path d="M3 3l18 18"/><path d="M10.6 6.1A9.7 9.7 0 0 1 12 6c5 0 8.5 4.5 9.5 6-.5.8-1.6 2.3-3.2 3.6M6.6 7.6C4.6 8.9 3.2 10.8 2.5 12c1 1.5 4.5 6 9.5 6 1.4 0 2.6-.3 3.7-.8"/></symbol>
</svg>`

const savedRow = (key: string, label: string) =>
  `<div class="dck-srow" data-k="${key}"><span>${label}</span>${icon("check")}</div>`
const serviceCard = (key: string, top: number, logo: string, name: string, what: string, pill: string) => `
  <div class="dck-svc" data-k="svc-${key}" data-state="idle" style="top: ${top}px">
    <div class="dck-sv-top"><div class="dck-ic">${logo}</div><div><b>${name}</b><small>${what}</small></div></div>
    <div class="dck-sv-meta">${pill ? `<span class="dck-pill">${pill}</span>` : "<span></span>"}<span class="dck-status"><i></i><em>Idle</em></span></div>
    <div class="dck-bar"><b></b></div>
  </div>`
const tick = (key: string) => `<svg class="dck-i dck-ok" data-k="${key}"><use href="#dck-check"/></svg>`
const travelling = (key: string, kind: "out" | "back") =>
  `<div class="dck-obj dck-pk dck-${kind}" data-k="${key}"><s></s><s></s><s></s></div>`

const MARKUP = `${SYMBOLS}
<div class="dck-inner" data-k="inner">
  <svg class="dck-bg" viewBox="0 0 1000 560" aria-hidden="true">
    <defs><linearGradient id="dck-alu" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#E3E5E9"/><stop offset="1" stop-color="#C4C8CF"/></linearGradient></defs>
    <g transform="translate(0 30)">
      <ellipse cx="220" cy="380" rx="205" ry="7" fill="rgba(20,20,30,.08)"/>
      <rect x="64" y="150" width="312" height="206" rx="12" fill="#1D1F23"/>
      <rect x="74" y="160" width="292" height="186" rx="3" fill="#fff"/>
      <path d="M36 356 H404 Q410 356 410 362 V364 Q410 372 402 373 H38 Q30 372 30 364 V362 Q30 356 36 356 Z" fill="url(#dck-alu)" stroke="#BCC1C9"/>
      <rect x="188" y="356" width="64" height="4" rx="2" fill="#B3B8C0"/>
    </g>
  </svg>

  <div class="dck-where" data-k="maclabel">${icon("laptop")}On your Mac</div>
  <div class="dck-legend">
    <span>${icon("laptop")}Stays on your Mac</span>
    <span><i class="dck-lgdoc dck-out"></i>Sent out</span>
    <span><i class="dck-lgdoc dck-back"></i>Comes back</span>
  </div>

  <div class="dck-screen">
    <div class="dck-sbar"><i></i><i></i><i></i><span>Powerpacks</span></div>
    <div class="dck-sbody">
      <div class="dck-slabel">Sources</div>
      <div class="dck-schips"><span class="dck-chip" data-k="chip0">Gmail</span><span class="dck-chip" data-k="chip1">iMessage</span><span class="dck-chip" data-k="chip2">WhatsApp</span><span class="dck-chip" data-k="chip3">LinkedIn</span></div>
      <div data-k="saved">
        <div class="dck-slabel">Saved on this Mac</div>
        ${savedRow("row0", "Excerpts")}${savedRow("row1", "Dossier")}${savedRow("row2", "Tags")}${savedRow("row3", "LinkedIn profile")}${savedRow("row4", "Match")}
      </div>
    </div>
    <div class="dck-search" data-k="search">
      <div class="dck-sinput">${icon("search")}<span data-k="qtext"></span><b class="dck-caret"></b></div>
      <div class="dck-result" data-k="result"><div class="dck-av dck-sm">JB</div><div><b>Jordan Bravo</b><small>VP Eng, Acme · ex-coworker · warm</small></div></div>
    </div>
    <div class="dck-glow" data-k="glow"></div>
  </div>

  <div class="dck-panel">
    <div class="dck-ptitle">${icon("globe")}Internet</div>
    ${serviceCard("openai", 58, `<img src="${openaiLogo}" alt="">`, "OpenAI", "Writes dossiers, tags and matches", "Not stored")}
    ${serviceCard("parallel", 198, `<img src="${parallelLogo}" alt="">`, "Parallel", "Looks people up on the web", "")}
    ${serviceCard("linkedin", 338, `<svg style="color: #0A66C2"><use href="#dck-linkedin"/></svg>`, "LinkedIn data", "Public profiles", "")}
  </div>

  <svg class="dck-routes" data-k="routes" viewBox="0 0 1000 560" aria-hidden="true"><defs data-k="rdefs"></defs></svg>

  <div data-k="layer">
    <div class="dck-obj dck-tile" data-k="src0">${icon("gmail", "color: #EA4335")}Gmail</div>
    <div class="dck-obj dck-tile" data-k="src1">${icon("imessage", "color: #34C759")}iMessage</div>
    <div class="dck-obj dck-tile" data-k="src2">${icon("whatsapp", "color: #25D366")}WhatsApp</div>
    <div class="dck-obj dck-tile" data-k="src3">${icon("linkedin", "color: #0A66C2")}LinkedIn</div>

    <div class="dck-obj dck-sheet" data-k="excerpts">
      <h4>Excerpts</h4><small>Jordan Bravo · 3 threads</small>
      <div class="dck-q"><b class="dck-hl" style="width: 120px"></b><s style="width: 114px"></s></div>
      <div class="dck-q"><s style="width: 88px"></s></div>
      <div class="dck-q"><b class="dck-hl" style="width: 108px"></b><s style="width: 102px"></s></div>
      <div class="dck-q"><s style="width: 76px"></s></div>
      <div class="dck-q"><s style="width: 112px"></s></div>
      <div class="dck-q"><s style="width: 64px"></s></div>
      <div class="dck-q"><s style="width: 96px"></s></div>
    </div>

    <div class="dck-obj dck-card dck-form" data-k="lookup">
      <div class="dck-fh">${icon("search")}Lookup</div>
      <div class="dck-ff"><span>Name</span><b>Jordan Bravo</b></div>
      <div class="dck-ff"><span>Company</span><b>Acme</b></div>
      <div class="dck-ff"><span>School</span><b>Stanford</b></div>
      <div class="dck-ff"><span>City</span><b>San Francisco</b></div>
    </div>

    <div class="dck-obj dck-card dck-dossier" data-k="dossier">
      <div class="dck-dh"><div class="dck-av">JB</div><div><b>Jordan Bravo</b><small>Dossier</small></div></div>
      <div class="dck-f"><span>Work</span><b>VP Eng, Acme</b>${tick("ok0")}</div>
      <div class="dck-f"><span>School</span><b>Stanford</b>${tick("ok1")}</div>
      <div class="dck-f"><span>Topics</span><b>LLM infra, hiring</b>${tick("ok2")}</div>
      <div class="dck-pers">
        <div class="dck-f" data-k="p0"><span>Birthday</span><b>March 3</b><em class="dck-red" data-k="r0"></em></div>
        <div class="dck-f" data-k="p1"><span>Trip</span><b>Tulum in May</b><em class="dck-red" data-k="r1"></em></div>
        <div class="dck-pnote" data-k="pnote"><b class="dck-pbg" data-k="pbg"></b><b class="dck-pring" data-k="pring"></b><div class="dck-ptext" data-k="ptext">${icon("eyeoff")}Personal details left out</div></div>
      </div>
      <div class="dck-tagrow"><span class="dck-tag" data-k="t0">Ex-coworker</span><span class="dck-tag" data-k="t1">Warm</span><span class="dck-tag" data-k="t2">Worth keeping</span></div>
    </div>

    <div class="dck-obj dck-card dck-linkcard" data-k="linkcard">
      <div class="dck-lk">${icon("linkedin", "color: #0A66C2")}<b>linkedin.com/in/jordanbravo</b></div>
      <small>or a stand-in profile built from the web</small>
    </div>

    <div class="dck-obj dck-card dck-profile" data-k="profile">
      <div class="dck-ph"></div>
      <div class="dck-pbody">
        <div class="dck-av">JB</div>
        <b>Jordan Bravo</b><small>VP Engineering · Acme</small>
        <div class="dck-pf"><span>Experience</span><b>Acme, Stripe</b></div>
        <div class="dck-pf"><span>Education</span><b>Stanford</b></div>
      </div>
    </div>

    <div class="dck-obj dck-verdict" data-k="verdict">
      <i class="dck-con dck-top" data-k="con0"></i><i class="dck-con dck-bottom" data-k="con1"></i>
      <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9.5"/><path class="dck-vcheck" data-k="vcheck" d="m7.8 12.4 2.9 2.9 5.6-6"/></svg>
      <b>Same person</b><span>96% match</span>
    </div>

    <div class="dck-obj dck-linkchip" data-k="linkchip">${icon("link")}linkedin.com/in/jordanbravo</div>

    ${travelling("pk1", "out")}${travelling("pk2", "out")}${travelling("pkb", "back")}
  </div>
</div>`

interface Pose {
  x: number
  y: number
  r: number
  s: number
  sx: number
  sy: number
  o: number
}
interface Frame extends Partial<Pose> {
  t?: number
}
interface Point {
  x: number
  y: number
}
interface Thing {
  el: HTMLElement
  w: number
  h: number
  pose: Pose
}
interface Timing {
  dur?: number
  ease?: string
  delay?: number
}
interface Scene {
  play: () => Promise<void>
  settle: () => void
}

const BASE: Pose = { x: 0, y: 0, r: 0, s: 1, sx: 1, sy: 1, o: 1 }
const ORANGE = "#E2622F"
const BLUE = "#2F6FDB"
const NS = "http://www.w3.org/2000/svg"
// Where things sit on the board.
const A: Point = { x: 528, y: 170 }
const B: Point = { x: 528, y: 414 }
const SCREEN: Point = { x: 220, y: 284 }
const SERVICES: Record<string, Point> = {
  openai: { x: 678, y: 132 },
  parallel: { x: 678, y: 272 },
  linkedin: { x: 678, y: 412 },
}
const TILES = [104, 182, 260, 338]
const LABELS = { idle: "Idle", working: "Working", done: "Sent back" }
// Every step plays to its end, then the next one starts this much later.
const BETWEEN_MS = 500

class Stop extends Error {}
const quiet = (promise: Promise<unknown>) => void promise.catch(() => undefined)
const kebab = (key: string) => key.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`)

function spring(stiffness = 10, damping = 7.5, n = 48): string {
  const points: string[] = []
  for (let i = 0; i <= n; i++) {
    const t = i / n
    points.push((t === 1 ? 1 : 1 - Math.exp(-damping * t) * Math.cos(stiffness * t)).toFixed(4))
  }
  return `linear(${points.join(", ")})`
}

/** Mounts the stage into `root` and plays it; `onStep` hears each step as it starts. */
export function mountStory(root: HTMLElement, onStep: (step: number) => void) {
  root.innerHTML = MARKUP
  const speed = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0.01 : 1
  const ease: Record<string, string> = {
    out: "cubic-bezier(.22, 1, .36, 1)",
    inOut: "cubic-bezier(.65, 0, .35, 1)",
    in: "cubic-bezier(.55, 0, 1, .45)",
    linear: "linear",
    spring: CSS.supports("animation-timing-function", "linear(0, 1)")
      ? spring()
      : "cubic-bezier(.34, 1.4, .64, 1)",
  }
  const find = (key: string): HTMLElement | SVGElement => {
    const found = root.querySelector(`[data-k="${key}"]`)
    if (found instanceof HTMLElement || found instanceof SVGElement) return found
    throw new Error(`how-it-works: no ${key} on the stage`)
  }

  // ---- motion
  let epoch = 0
  const live = new Set<Animation>()
  const cancelAll = () => {
    epoch++
    for (const animation of live) animation.cancel()
    live.clear()
  }
  const wait = async (ms: number) => {
    const at = epoch
    await new Promise((resolve) => setTimeout(resolve, ms * speed))
    if (at !== epoch) throw new Stop()
  }
  const play = (
    el: Element,
    keyframes: Keyframe[],
    { dur = 600, ease: curve = "out", delay = 0 }: Timing = {},
  ) => {
    const at = epoch
    const animation = el.animate(keyframes, {
      duration: Math.max(1, dur * speed),
      delay: delay * speed,
      easing: ease[curve] ?? curve,
      fill: "forwards",
    })
    live.add(animation)
    return animation.finished.then(
      () => {
        live.delete(animation)
        if (at !== epoch) throw new Stop()
        return animation
      },
      () => {
        live.delete(animation)
        throw new Stop()
      },
    )
  }
  // Plays keyframes, then keeps the last one as the element's own style.
  const anim = async (key: string, keyframes: Keyframe[], timing?: Timing) => {
    const el = find(key)
    const animation = await play(el, keyframes, timing)
    const last = keyframes.at(-1)
    if (last)
      for (const [name, value] of Object.entries(last))
        if (
          name !== "offset" &&
          name !== "easing" &&
          (typeof value === "string" || typeof value === "number")
        )
          el.style.setProperty(kebab(name), String(value))
    animation.cancel()
  }
  const POP: Keyframe[] = [
    { opacity: 0, transform: "scale(.7)" },
    { opacity: 1, transform: "none" },
  ]
  const SLIDE: Keyframe[] = [
    { opacity: 0, transform: "translateX(-6px)" },
    { opacity: 1, transform: "none" },
  ]
  const BUMP: Keyframe[] = [
    { transform: "scale(1)" },
    { transform: "scale(1.025)", offset: 0.35 },
    { transform: "scale(1)" },
  ]
  const shown = (key: string) => {
    const el = find(key)
    el.style.opacity = "1"
    el.style.transform = "none"
  }

  // ---- things on the board: placed by their centre, plus rotation, scale and opacity
  const things = new Map<string, Thing>()
  for (const el of find("layer").children)
    if (el instanceof HTMLElement && el.dataset.k)
      things.set(el.dataset.k, { el, w: el.offsetWidth, h: el.offsetHeight, pose: { ...BASE, o: 0 } })
  const thing = (key: string): Thing => {
    const found = things.get(key)
    if (!found) throw new Error(`how-it-works: ${key} does not move`)
    return found
  }
  const transform = (item: Thing, pose: Pose) =>
    `translate(${(pose.x - item.w / 2).toFixed(1)}px, ${(pose.y - item.h / 2).toFixed(1)}px) rotate(${pose.r.toFixed(2)}deg) scale(${(pose.s * pose.sx).toFixed(3)}, ${(pose.s * pose.sy).toFixed(3)})`
  const put = (key: string, pose: Partial<Pose>) => {
    const item = thing(key)
    item.pose = { ...item.pose, ...pose }
    item.el.style.transform = transform(item, item.pose)
    item.el.style.opacity = String(item.pose.o)
  }
  const move = async (key: string, frames: Frame[], timing?: Timing) => {
    const item = thing(key)
    let pose = { ...item.pose }
    const keyframes: Keyframe[] = [{ transform: transform(item, pose), opacity: pose.o, offset: 0 }]
    frames.forEach(({ t, ...change }, i) => {
      pose = { ...pose, ...change }
      keyframes.push({
        transform: transform(item, pose),
        opacity: pose.o,
        offset: t ?? (i + 1) / frames.length,
      })
    })
    const animation = await play(item.el, keyframes, timing)
    item.pose = pose
    item.el.style.transform = transform(item, pose)
    item.el.style.opacity = String(pose.o)
    animation.cancel()
  }
  // A gentle curve between two points; bow > 0 bends to the left of the direction of travel.
  const arc = (
    from: Point,
    to: Point,
    bow: number,
    { n = 22, tilt = 0, extra }: { n?: number; tilt?: number; extra?: (t: number) => Partial<Pose> } = {},
  ) => {
    const dx = to.x - from.x
    const dy = to.y - from.y
    const length = Math.hypot(dx, dy) || 1
    const c = { x: (from.x + to.x) / 2 + (dy / length) * bow, y: (from.y + to.y) / 2 - (dx / length) * bow }
    const frames: Frame[] = []
    for (let i = 1; i <= n; i++) {
      const t = i / n
      const u = 1 - t
      const frame: Frame = {
        t,
        x: u * u * from.x + 2 * u * t * c.x + t * t * to.x,
        y: u * u * from.y + 2 * u * t * c.y + t * t * to.y,
      }
      if (tilt) {
        const tx = 2 * u * (c.x - from.x) + 2 * t * (to.x - c.x)
        const ty = 2 * u * (c.y - from.y) + 2 * t * (to.y - c.y)
        frame.r = ((Math.atan2(ty, Math.abs(tx)) * 180) / Math.PI) * tilt
      }
      frames.push(extra ? { ...frame, ...extra(t) } : frame)
    }
    return { c, frames }
  }
  // A dotted route that draws itself along with the paper travelling it.
  let routes = 0
  const route = (from: Point, c: Point, to: Point, color: string, dur: number) => {
    const d = `M${from.x} ${from.y} Q${c.x} ${c.y} ${to.x} ${to.y}`
    const mask = document.createElementNS(NS, "mask")
    mask.id = `dck-route-${++routes}`
    for (const [name, value] of Object.entries({
      maskUnits: "userSpaceOnUse",
      x: 0,
      y: 0,
      width: 1000,
      height: 560,
    }))
      mask.setAttribute(name, String(value))
    const reveal = document.createElementNS(NS, "path")
    for (const [name, value] of Object.entries({ d, stroke: "#fff", "stroke-width": 10, fill: "none" }))
      reveal.setAttribute(name, String(value))
    mask.append(reveal)
    find("rdefs").append(mask)
    const line = document.createElementNS(NS, "path")
    for (const [name, value] of Object.entries({
      d,
      stroke: color,
      "stroke-width": 2,
      "stroke-linecap": "round",
      "stroke-dasharray": "0.5 7",
      fill: "none",
      mask: `url(#${mask.id})`,
    }))
      line.setAttribute(name, String(value))
    find("routes").append(line)
    const length = reveal.getTotalLength()
    reveal.style.strokeDasharray = `${length}`
    reveal.style.strokeDashoffset = `${length}`
    quiet(
      play(reveal, [{ strokeDashoffset: `${length}` }, { strokeDashoffset: "0" }], { dur, ease: "inOut" }),
    )
    return {
      fade: () =>
        quiet(
          play(line, [{ opacity: 1 }, { opacity: 0 }], { dur: 500, delay: 400 }).then(() => {
            line.remove()
            mask.remove()
          }),
        ),
    }
  }

  // ---- the internet
  const status = (key: string, state: keyof typeof LABELS) => {
    const card = find(`svc-${key}`)
    card.dataset.state = state
    const label = card.querySelector(".dck-status em")
    if (label) label.textContent = LABELS[state]
  }
  const work = async (key: string, ms: number) => {
    status(key, "working")
    const fill = find(`svc-${key}`).querySelector(".dck-bar b")
    if (!fill) throw new Error(`how-it-works: ${key} has no progress bar`)
    const animation = await play(fill, [{ transform: "scaleX(0)" }, { transform: "scaleX(1)" }], {
      dur: ms,
      ease: "inOut",
    })
    if (fill instanceof HTMLElement) fill.style.transform = "none"
    animation.cancel()
    status(key, "done")
  }
  const flash = () =>
    quiet(
      anim("glow", [{ opacity: 0 }, { opacity: 0.6, offset: 0.3 }, { opacity: 0 }], {
        dur: 650,
        ease: "linear",
      }),
    )
  const service = (key: string): Point => {
    const found = SERVICES[key]
    if (!found) throw new Error(`how-it-works: no service ${key}`)
    return found
  }
  // A copy of the paper leaves for a service on the internet.
  const send = async (from: Point, key: string, paper = "pk1") => {
    const at = service(key)
    const to = { x: at.x + 10, y: at.y - 16 }
    const { c, frames } = arc(from, to, 70, { tilt: 0.25 })
    put(paper, { ...from, r: 0, s: 0.5, o: 0 })
    await move(paper, [{ s: 1, o: 1 }], { dur: 260 })
    const line = route(from, c, to, ORANGE, 1150)
    await move(paper, frames, { dur: 1150, ease: "inOut" })
    await move(paper, [{ x: to.x + 40, s: 0.5, o: 0 }], { dur: 240, ease: "in" })
    quiet(anim(`svc-${key}`, BUMP, { dur: 420 }))
    line.fade()
  }
  // The answer comes back to a spot on the Mac.
  const receive = async (key: string, to: Point) => {
    const at = service(key)
    const from = { x: at.x + 10, y: at.y + 18 }
    const { c, frames } = arc(from, to, 70, { tilt: 0.25 })
    put("pkb", { ...from, r: 0, s: 0.5, o: 0 })
    await move("pkb", [{ s: 1, o: 1 }], { dur: 240 })
    const line = route(from, c, to, BLUE, 1150)
    await move("pkb", frames, { dur: 1150, ease: "inOut" })
    line.fade()
  }
  // The returned paper opens into what it carried.
  const unfold = async (key: string, at: Point) => {
    put(key, { ...at, r: 0, s: 0.6, o: 0 })
    quiet(move("pkb", [{ s: 1.8, o: 0 }], { dur: 320 }))
    await move(key, [{ s: 1, o: 1 }], { dur: 600, ease: "spring" })
  }
  const fileIntoMac = async (key: string, delay = 0) => {
    if (delay) await wait(delay)
    const pose = { ...thing(key).pose }
    const { frames } = arc(pose, SCREEN, -40, {
      extra: (t) => ({ s: pose.s * (1 - 0.8 * t), o: t > 0.7 ? (1 - t) / 0.3 : 1, r: pose.r * (1 - t) }),
    })
    await move(key, frames, { dur: 850, ease: "inOut" })
    flash()
  }
  const typeText = async (key: string, text: string) => {
    const el = find(key)
    el.textContent = ""
    for (const letter of text) {
      el.textContent += letter
      await wait(45)
    }
  }
  // Where the match lands: halfway between the dossier and the profile.
  const verdictAt = (): Point => ({
    x: A.x,
    y: (A.y + thing("dossier").h / 2 + B.y - thing("profile").h / 2) / 2,
  })
  const gap = verdictAt().y - thing("verdict").h / 2 - (A.y + thing("dossier").h / 2)
  for (const key of ["con0", "con1"]) find(key).style.height = `${gap}px`

  // ---- the story
  const scenes: Scene[] = [
    {
      async play() {
        await Promise.all(
          TILES.map((x, i) => {
            put(`src${i}`, { x, y: 134, o: 0 })
            return move(`src${i}`, [{ y: 122, o: 1 }], { dur: 600, delay: i * 90 })
          }),
        )
        await wait(450)
        await Promise.all(
          TILES.map(async (_, i) => {
            await wait(i * 140)
            const { frames } = arc(thing(`src${i}`).pose, SCREEN, 30, {
              extra: (t) => ({ s: 1 - 0.75 * t, o: t > 0.7 ? (1 - t) / 0.3 : 1 }),
            })
            await move(`src${i}`, frames, { dur: 750, ease: "inOut" })
            flash()
            await anim(`chip${i}`, POP, { dur: 500, ease: "spring" })
          }),
        )
        await anim("maclabel", BUMP, { dur: 500 })
      },
      settle() {
        for (let i = 0; i < 4; i++) shown(`chip${i}`)
      },
    },
    {
      async play() {
        flash()
        put("excerpts", { ...SCREEN, r: 0, s: 0.3, o: 0 })
        const { frames } = arc(SCREEN, A, 50, {
          extra: (t) => ({ s: 0.3 + 0.7 * t, o: Math.min(1, t * 3), r: -2 * t }),
        })
        await move("excerpts", frames, { dur: 900 })
        for (const mark of find("excerpts").querySelectorAll(".dck-hl")) {
          const animation = await play(mark, [{ transform: "scaleX(0)" }, { transform: "scaleX(1)" }], {
            dur: 420,
            ease: "inOut",
          })
          if (mark instanceof HTMLElement) mark.style.transform = "none"
          animation.cancel()
        }
        await anim("row0", SLIDE, { dur: 380 })
      },
      settle() {
        put("excerpts", { ...A, r: -2, o: 1 })
        for (const mark of find("excerpts").querySelectorAll(".dck-hl"))
          if (mark instanceof HTMLElement) mark.style.transform = "none"
        shown("row0")
      },
    },
    {
      async play() {
        await send({ x: A.x + 40, y: A.y - 50 }, "openai")
        quiet(fileIntoMac("excerpts"))
        await work("openai", 1200)
        await receive("openai", A)
        await unfold("dossier", A)
        await anim("row1", SLIDE, { dur: 380 })
      },
      settle() {
        put("excerpts", { o: 0 })
        put("dossier", { ...A, o: 1 })
        shown("row1")
      },
    },
    {
      async play() {
        for (const key of ["r0", "r1"])
          await anim(key, [{ transform: "scaleX(0)" }, { transform: "scaleX(1)" }], {
            dur: 450,
            ease: "inOut",
          })
        await wait(300)
        await Promise.all(
          ["p0", "p1"].map((key) =>
            anim(
              key,
              [
                { opacity: 1, transform: "none" },
                { opacity: 0, transform: "translateY(-4px)" },
              ],
              { dur: 300 },
            ),
          ),
        )
        find("pnote").style.opacity = "1"
        await anim("pbg", [{ transform: "scaleX(0)" }, { transform: "scaleX(1)" }], {
          dur: 480,
          ease: "inOut",
        })
        await anim(
          "ptext",
          [
            { opacity: 0, transform: "translateX(-8px)" },
            { opacity: 1, transform: "none" },
          ],
          { dur: 320 },
        )
        for (let i = 0; i < 2; i++)
          await anim(
            "pring",
            [
              { opacity: 0.9, transform: "scale(1)" },
              { opacity: 0, transform: "scale(1.1, 1.35)" },
            ],
            { dur: 750 },
          )
        await Promise.all(
          [0, 1, 2].map((i) => anim(`ok${i}`, POP, { dur: 500, ease: "spring", delay: i * 110 })),
        )
      },
      settle() {
        for (const key of ["p0", "p1"]) find(key).style.opacity = "0"
        for (const key of ["pnote", "pbg", "ptext", "ok0", "ok1", "ok2"]) shown(key)
      },
    },
    {
      async play() {
        await send({ x: A.x + 40, y: A.y - 50 }, "openai")
        await work("openai", 900)
        await receive("openai", { x: A.x, y: A.y + 78 })
        quiet(move("pkb", [{ s: 1.8, o: 0 }], { dur: 320 }))
        await Promise.all(
          [0, 1, 2].map((i) => anim(`t${i}`, POP, { dur: 500, ease: "spring", delay: i * 110 })),
        )
        await anim("row2", SLIDE, { dur: 380 })
      },
      settle() {
        for (const key of ["t0", "t1", "t2", "row2"]) shown(key)
      },
    },
    {
      async play() {
        const at = { x: A.x, y: 352 }
        put("lookup", { ...A, s: 0.6, o: 0 })
        await move("lookup", [{ ...at, s: 1, o: 1 }], { dur: 650, ease: "spring" })
        await wait(1000)
        quiet(move("lookup", [{ s: 0.4, o: 0 }], { dur: 280, ease: "in" }))
        await send({ x: at.x + 20, y: at.y - 20 }, "parallel")
        await work("parallel", 1300)
        await receive("parallel", B)
        await unfold("linkcard", B)
      },
      settle() {
        put("linkcard", { ...B, o: 1 })
      },
    },
    {
      async play() {
        const at = { x: B.x + 10, y: B.y - 64 }
        put("linkchip", { ...B, s: 0.6, o: 0 })
        await move("linkchip", [{ ...at, s: 1, o: 1 }], { dur: 550, ease: "spring" })
        quiet(fileIntoMac("linkcard", 300))
        await wait(800)
        quiet(move("linkchip", [{ s: 0.4, o: 0 }], { dur: 260, ease: "in" }))
        await send(at, "linkedin")
        await work("linkedin", 1000)
        await receive("linkedin", B)
        await unfold("profile", B)
        await anim("row3", SLIDE, { dur: 380 })
      },
      settle() {
        put("linkcard", { o: 0 })
        put("profile", { ...B, o: 1 })
        shown("row3")
      },
    },
    {
      async play() {
        await Promise.all([
          send({ x: A.x + 40, y: A.y - 50 }, "openai", "pk1"),
          (async () => {
            await wait(350)
            await send({ x: B.x + 40, y: B.y - 40 }, "openai", "pk2")
          })(),
        ])
        await work("openai", 1300)
        await receive("openai", verdictAt())
        quiet(move("pkb", [{ s: 1.8, o: 0 }], { dur: 320 }))
        put("verdict", { ...verdictAt(), s: 0.7, o: 0 })
        await move("verdict", [{ s: 1, o: 1 }], { dur: 550, ease: "spring" })
        await Promise.all(
          ["con0", "con1"].map((key) =>
            anim(key, [{ transform: "scaleY(0)" }, { transform: "scaleY(1)" }], { dur: 320 }),
          ),
        )
        await anim("vcheck", [{ strokeDashoffset: "16" }, { strokeDashoffset: "0" }], { dur: 420 })
        await anim("row4", SLIDE, { dur: 380 })
      },
      settle() {
        put("verdict", { ...verdictAt(), o: 1 })
        for (const key of ["con0", "con1"]) find(key).style.transform = "none"
        find("vcheck").style.strokeDashoffset = "0"
        shown("row4")
      },
    },
    {
      async play() {
        quiet(move("verdict", [{ o: 0, s: 0.9 }], { dur: 250 }))
        await Promise.all(["dossier", "profile"].map((key, i) => fileIntoMac(key, i * 180)))
        await anim("saved", [{ opacity: 1 }, { opacity: 0 }], { dur: 250 })
        await anim("search", [{ opacity: 0 }, { opacity: 1 }], { dur: 300 })
        await typeText("qtext", "Who do I know at Acme?")
        await wait(250)
        await anim(
          "result",
          [
            { opacity: 0, transform: "translateY(6px)" },
            { opacity: 1, transform: "none" },
          ],
          { dur: 420 },
        )
        await wait(1500)
      },
      settle() {
        // the last step leaves nothing behind: the story starts over
      },
    },
  ]

  const reset = () => {
    find("routes")
      .querySelectorAll("path")
      .forEach((path) => path.remove())
    find("rdefs").textContent = ""
    for (const key of things.keys()) put(key, { ...BASE, o: 0 })
    root
      .querySelectorAll<HTMLElement | SVGElement>(
        ".dck-chip, .dck-srow, .dck-ok, .dck-tag, .dck-hl, .dck-red, .dck-pnote, .dck-pbg, .dck-pring, .dck-ptext, [data-k=p0], [data-k=p1], [data-k=saved], .dck-search, .dck-result, .dck-con, .dck-vcheck, .dck-bar b, .dck-svc, [data-k=maclabel], .dck-glow",
      )
      .forEach((el) => {
        el.style.opacity = ""
        el.style.transform = ""
        el.style.strokeDashoffset = ""
      })
    for (const key of Object.keys(SERVICES)) status(key, "idle")
    find("qtext").textContent = ""
  }

  let current = 0
  let hold: ReturnType<typeof setTimeout> | undefined
  const show = (step: number) => {
    cancelAll()
    clearTimeout(hold)
    current = (step + scenes.length) % scenes.length
    reset()
    scenes.slice(0, current).forEach((scene) => scene.settle())
    onStep(current)
    const at = epoch
    const scene = scenes[current]
    if (!scene) return
    scene.play().then(
      () => {
        if (at === epoch) hold = setTimeout(() => show(current + 1), BETWEEN_MS)
      },
      (error: unknown) => {
        if (!(error instanceof Stop)) console.error(error)
      },
    )
  }

  const fit = new ResizeObserver(([entry]) => {
    if (entry) find("inner").style.setProperty("zoom", String(entry.contentRect.width / 1000))
  })
  fit.observe(root)

  return {
    show,
    destroy: () => {
      cancelAll()
      clearTimeout(hold)
      fit.disconnect()
      root.innerHTML = ""
    },
  }
}
