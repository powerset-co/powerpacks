import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { StrictMode } from "react"
import { createRoot } from "react-dom/client"

import mark from "@/assets/powerset-mark.png"
import "@/styles/index.css"
import { desktopPlatform } from "@/lib/desktop"

import { App } from "./App"

// The tab's icon is the Powerset mark; the bundle inlines it, so the server serves no image.
const icon = document.createElement("link")
icon.rel = "icon"
icon.href = mark
document.head.append(icon)

const root = document.getElementById("root")
if (!root) throw new Error("The app needs a #root element")

// The desktop app's OS, for the few rules that differ there (the macOS window buttons).
const platform = desktopPlatform()
if (platform) document.documentElement.dataset.desktop = platform

const queryClient = new QueryClient()

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
)
