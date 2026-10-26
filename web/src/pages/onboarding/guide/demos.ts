// The connect guides: what the person sees when a source connects (macOS Full Disk Access, Google
// sign-in, WhatsApp on the phone), drawn in HTML so the onboarding can show it in its own window.
// Ported from powerpacks-onboarding-animations/demos.js (the preview page); keep both in step.
export type GuideSource = "imessage" | "gmail" | "whatsapp"
export interface Person {
  name: string
  email: string
}
export interface Flow {
  title: string
  eyebrow: string
  intro: string
  steps: readonly string[]
  captions: readonly string[]
  note: string
  duration: number
}
export const flows: Record<GuideSource, Flow> = {
  imessage: {
    title: "Bring your conversations along.",
    eyebrow: "MESSAGES · MAC",
    intro: "Give Powerpacks access to Messages with one macOS permission.",
    steps: [
      "Drag Powerpacks into Full Disk Access.",
      "Turn on the switch next to Powerpacks.",
      "When macOS asks to quit, click Later.",
    ],
    captions: [
      "Drag the Powerpacks icon into the list.",
      "Turn on Full Disk Access for Powerpacks.",
      "macOS asks to quit. Click Later: Powerpacks keeps going without a restart.",
    ],
    note: "macOS may ask for your password before the switch moves. Later is the right answer to the quit question.",
    duration: 15,
  },
  gmail: {
    title: "Connect your Gmail.",
    eyebrow: "GMAIL · GOOGLE SIGN-IN",
    intro:
      "Sign in to the Google account you want to connect. Powerpacks takes care of the setup.",
    steps: [
      "Choose your Google account.",
      "Complete sign-in and any verification.",
      "Allow the Gmail access (Select all, then Continue).",
    ],
    captions: [
      "Choose the mailbox you want to connect.",
      "Complete any verification Google asks for.",
      "Tick Select all, then Continue. Powerpacks usually does this for you.",
    ],
    note: "Powerpacks ticks the two Gmail permissions and continues for you. If Google hands them to you instead, tick Select all, then Continue.",
    duration: 15,
  },
  whatsapp: {
    title: "Link your WhatsApp.",
    eyebrow: "WHATSAPP · ON YOUR IPHONE",
    intro: "Keep the QR code open on your computer, then open WhatsApp on your iPhone.",
    steps: [
      "In WhatsApp, tap Settings, then Linked devices.",
      "Tap Link a device.",
      "Scan the code shown in Powerpacks.",
    ],
    captions: [
      "On your phone, open WhatsApp Settings, then Linked devices.",
      "Tap Link a device on your phone.",
      "Point your phone at the QR code on your computer.",
    ],
    note: "In WhatsApp, tap Settings at the bottom right.",
    duration: 15,
  },
};
export const SAMPLE_PERSON: Person = { name: "Jordan Bravo", email: "jordan.bravo@gmail.com" };
const mark = '<b class="mark">P</b>';
const cursor =
  '<svg class="cursor" viewBox="0 0 30 36"><path d="M3 2v28l7-7 6 11 5-3-6-10h10z" fill="white" stroke="#181818" stroke-width="2"/></svg>';
const toggle = '<span class="switch"><i></i></span>';

// Product-faithful glyphs. macOS System Settings sidebar tiles, the Google "G", WhatsApp's iOS
// Settings tiles and tab bar. White strokes on coloured tiles, as the real apps draw them.
const glyph = (d: string, extra = "") =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" ${extra}>${d}</svg>`;
const tiles = {
  gear: glyph('<circle cx="12" cy="12" r="3.2"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M5.3 18.7l2.1-2.1M16.6 7.4l2.1-2.1"/>'),
  appearance: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" fill="white"/><path d="M12 3a9 9 0 0 1 0 18z" fill="#1c1c1e"/></svg>',
  person: glyph('<circle cx="12" cy="12" r="9.5"/><circle cx="12" cy="9" r="2.6"/><path d="M6.5 18.5c1-3 3-4.5 5.5-4.5s4.5 1.5 5.5 4.5"/>'),
  hand: glyph('<path d="M7.5 11.5V5a1.5 1.5 0 0 1 3 0v6M10.5 10.5V3.5a1.5 1.5 0 0 1 3 0v7M13.5 10.5V5a1.5 1.5 0 0 1 3 0v8M16.5 13V8.5a1.5 1.5 0 0 1 3 0V14c0 4.5-3 7.5-7 7.5-3.3 0-5.3-1.6-6.6-4.2L4 13.5a1.6 1.6 0 0 1 2.7-1.6l.8 1.4"/>'),
  at: glyph('<circle cx="12" cy="12" r="4"/><path d="M16 12v1.5a2.5 2.5 0 0 0 5 0V12a9 9 0 1 0-3.5 7.1"/>'),
  star: glyph('<path d="M12 3.5l2.6 5.4 5.9.8-4.3 4.1 1.1 5.9L12 16.9l-5.3 2.8 1.1-5.9-4.3-4.1 5.9-.8z" fill="white"/>'),
  laptop: glyph('<rect x="4" y="5" width="16" height="11" rx="1.5"/><path d="M2 19h20"/>'),
  key: glyph('<circle cx="8" cy="14" r="4"/><path d="M11 11l9-9M16 6l2.5 2.5M13.5 8.5L16 11"/>'),
  lock: glyph('<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>'),
  bubble: glyph('<path d="M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v8a2.5 2.5 0 0 1-2.5 2.5H9l-5 4z" fill="white"/>'),
  bell: glyph('<path d="M6 17V11a6 6 0 0 1 12 0v6l1.5 2h-15zM10 21h4"/>'),
  arrows: glyph('<path d="M7 3v14M3 13l4 4 4-4M17 21V7M13 11l4-4 4 4"/>'),
  chevron: '<svg class="chev" viewBox="0 0 8 14" fill="none" stroke="#c4c4c6" stroke-width="2"><path d="M1.5 1.5L6.5 7l-5 5.5"/></svg>',
  back: '<svg viewBox="0 0 8 14" fill="none" stroke="currentColor" stroke-width="2"><path d="M6.5 1.5L1.5 7l5 5.5"/></svg>',
  googleG:
    '<svg class="google-g" viewBox="0 0 48 48"><path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/><path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/><path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/><path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg>',
};
const tile = (name: keyof typeof tiles, color: string) => `<i class="tile" style="background:${color}">${tiles[name]}</i>`;
const sideRows: readonly (readonly [string, keyof typeof tiles, string])[] = [
  ["General", "gear", "#8e8e93"],
  ["Appearance", "appearance", "#1c1c1e"],
  ["Accessibility", "person", "#0a7aff"],
  ["Privacy & Security", "hand", "#0a7aff"],
  ["Internet Accounts", "at", "#0a7aff"],
];
function row(icon: string, name: string, extra = "") {
  return `<div class="app-row">${icon}<span>${name}</span>${extra}</div>`;
}
function qr() {
  return `<div class="qr" aria-hidden="true">${Array.from({ length: 121 }, (_, i) => `<i class="${(i * 17 + (i % 7)) % 5 < 3 ? "ink" : ""}"></i>`).join("")}</div>`;
}
function mac() {
  return `<div class="mac-window"><aside><div class="lights"><i></i><i></i><i></i></div><div class="search">⌕ <span>Search</span></div>${sideRows.map(([s, g, c], i) => `<div class="side-row ${i === 3 ? "selected" : ""}">${tile(g, c)}${s}</div>`).join("")}</aside><div class="mac-main"><div class="mac-title"><span class="back">${tiles.back}</span><strong>Full Disk Access</strong></div><p>Allow the applications below to access data like Mail, Messages, Safari, Home, Time Machine backups, and certain administrative settings for all users on this Mac.</p><div class="permission-list">${row('<b class="app-icon terminal">›_</b>', "Terminal", toggle)}<div class="power-row">${row(mark, "Powerpacks", toggle)}</div><div class="add-row">＋ <span>−</span></div></div></div></div><div class="drop-guide"><div>↑ &nbsp; Drag Powerpacks into the list above</div><div class="drag-source">${mark}<span>Powerpacks</span><span class="grip">⠿</span></div></div><div class="drag-copy">${mark}</div><div class="restart-dialog"><div>${mark}</div><strong>“Powerpacks” will not have full disk access until it is quit.</strong><p>You may choose to quit “Powerpacks” now, or do it on your own later.</p><div><span class="pick">Later</span><b>Quit &amp; Reopen</b></div></div>${cursor}`;
}
function gmail(person: Person) {
  return `<div class="browser"><div class="browser-bar"><div class="lights"><i></i><i></i><i></i></div><span>🔒 accounts.google.com</span></div><div class="google-page account-page">${tiles.googleG}<h2>Choose an account</h2><p>to continue to <b>Powerpacks</b></p><div class="account"><b class="avatar">${person.name.slice(0, 1)}</b><div>${person.name}<small>${person.email}</small></div></div><div class="other-account"><b class="avatar plain">⊕</b>Use another account</div><p class="google-foot">To continue, Google will share your name, email address, language preference, and profile picture with Powerpacks.</p></div><div class="google-page verify-page">${tiles.googleG}<h2>Verify it’s you</h2><p>To help keep your account safe, Google wants to make sure it’s really you trying to sign in.</p><div class="verify-phone"><span class="verify-icon">📱</span><span>Tap <b>Yes</b> on your phone or type the code Google sent you.</span></div><small>Not every account asks for this.</small></div><div class="google-page consent-page">${tiles.googleG}<h2>Powerpacks wants access to your Google Account</h2><p class="consent-email"><b class="avatar small">${person.name.slice(0, 1)}</b>${person.email}</p><p class="consent-lead">Select what Powerpacks can access</p><div class="scope select-all"><span class="checkbox">✓</span><span>Select all</span></div><div class="scope"><span class="checkbox">✓</span><span>See your email messages and settings</span></div><div class="scope"><span class="checkbox">✓</span><span>Read, compose, and send emails from your Gmail account</span></div><p class="scope-note">Make sure you trust Powerpacks. You may be sharing sensitive info with this site or app.</p><div class="google-actions"><span>Cancel</span><div class="google-button">Continue</div></div></div><div class="google-page complete-page"><span class="success-check">✓</span><h2>You’re connected.</h2><p>Back to Powerpacks.</p><div class="connected-mail">${mark}<span>Gmail <small>${person.email}</small></span><b>Connected</b></div></div></div>${cursor}`;
}
function whatsapp(person: Person) {
  const waRow = (name: string, g: keyof typeof tiles, c: string, cls = "") => `<div class="wa-row ${cls}">${tile(g, c)}<span>${name}</span>${tiles.chevron}</div>`;
  return `<div class="phone"><div class="phone-top">9:41 <span>●●● ᯤ ▰</span></div><div class="phone-screen wa-home"><h2>Settings</h2><div class="wa-group"><div class="wa-profile"><b class="avatar">${person.name.slice(0, 1)}</b><div>${person.name.split(" ")[0]}<small>Available</small></div>${tiles.chevron}</div></div><div class="wa-group">${waRow("Starred messages", "star", "#ffc100")}${waRow("Linked devices", "laptop", "#1dab61", "linked-choice")}</div><div class="wa-group">${waRow("Account", "key", "#0a7aff")}${waRow("Privacy", "lock", "#32ade6")}${waRow("Chats", "bubble", "#1dab61")}</div><div class="phone-tabs"><span><i>◯</i>Updates</span><span><i>☏</i>Calls</span><span><i>⚇</i>Communities</span><span><i>▭</i>Chats</span><b><i>⚙</i>Settings</b></div></div><div class="phone-screen wa-linked"><div class="wa-back">${tiles.back} Settings</div><h2>Linked devices</h2><div class="devices-art">${tiles.laptop}</div><p><strong>Use WhatsApp on other devices</strong></p><p class="device-note">Your personal messages are end-to-end encrypted on all your devices.</p><div class="link-button">Link a device</div></div><div class="phone-screen wa-scan"><div class="wa-back">${tiles.back} &nbsp; Scan QR code</div><p>Scan the code on your computer.</p><div class="scan-view"><div class="scan-corners">${qr()}<i class="scan-line"></i></div></div><div class="scan-success">✓ &nbsp; Device linked</div></div><div class="home-indicator"></div></div><div class="computer-code">${mark}<strong>Connect WhatsApp</strong>${qr()}<small>Scan from Linked devices</small></div><div class="tap"></div>`;
}
/** Draws one source's guide into `element`; `seek(progress)` moves it (0..1 of the flow's duration).
 *  `person`: the signed-in account (name, email); the app passes its own, the preview a representative one. */
export function mountDemo(
  element: HTMLElement,
  source: GuideSource,
  { consent = true, person = SAMPLE_PERSON }: { consent?: boolean; person?: Person } = {},
): { seek: (progress: number) => void } {
  element.className = `stage ${source}${consent ? " with-consent" : ""}`;
  element.innerHTML =
    source === "imessage"
      ? mac()
      : source === "gmail"
        ? gmail(person)
        : whatsapp(person);
  return {
    seek(progress) {
      element.style.setProperty("--p", String(progress));
      element.dataset.phase = String(Math.min(2, Math.floor(progress * 3)));
      element.dataset.beat = String(Math.min(5, Math.floor(progress * 6)));
    },
  };
}
