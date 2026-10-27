// Node 25 ships a global localStorage that shadows jsdom's and lacks clear/getItem/setItem
// without --localstorage-file. Vitest's jsdom environment exposes the jsdom instance as `jsdom`.
declare const jsdom: { window: Window }

Object.defineProperty(globalThis, "localStorage", { value: jsdom.window.localStorage, configurable: true })
