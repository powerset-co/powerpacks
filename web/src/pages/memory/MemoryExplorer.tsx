import { useQuery } from "@tanstack/react-query"
import { useCallback, useEffect, useRef, useState } from "react"

import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import { fetchMemoryGraph } from "@/lib/api/memory"
import type { MemorySelector } from "@/types/memory"

import { MemoryGraph } from "./graph"
import { MemorySearch } from "./MemorySearch"

export function MemoryExplorer({ running }: { running: boolean }) {
  const [selection, setSelection] = useState<MemorySelector | null>(null)
  const [topic, setTopic] = useState<string | null>(null)
  const [parent, setParent] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [motion, setMotion] = useState(() => !matchMedia("(prefers-reduced-motion: reduce)").matches)
  const [depth, setDepth] = useState(true)
  const canvas = useRef<HTMLCanvasElement>(null)
  const renderer = useRef<MemoryGraph | null>(null)
  const graph = useQuery({
    queryKey: ["memory-graph", selection, parent, topic],
    queryFn: ({ signal }) => fetchMemoryGraph(selection, parent, topic, signal),
    refetchInterval: running ? 30_000 : false,
  })
  const data = graph.data
  const focusedId = selectedId ?? (data?.selected_ids.length === 1 ? (data.selected_ids[0] ?? null) : null)
  const selectNode = useCallback((id: string) => {
    setSelectedId(id)
    if (id.startsWith("topic:")) {
      setTopic(id.slice(6))
      setSelection(null)
      setParent(null)
    } else {
      setParent(id)
      setTopic(null)
    }
  }, [])
  const selectMeaning = (selector: MemorySelector | null) => {
    setSelection(selector)
    setTopic(null)
    setParent(null)
    setSelectedId(selector?.origin === "topic" ? `topic:${selector.value}` : null)
  }
  useEffect(() => {
    if (!canvas.current || !data?.nodes.length) return
    const instance = new MemoryGraph(canvas.current, data, selectNode)
    renderer.current = instance
    return () => {
      instance.destroy()
      renderer.current = null
    }
  }, [data, selectNode])
  useEffect(() => {
    renderer.current?.select(focusedId)
    renderer.current?.highlight(data?.selected_ids ?? [])
  }, [focusedId, data])
  useEffect(() => {
    if (renderer.current) renderer.current.motion = motion
  }, [motion, data])
  useEffect(() => {
    if (renderer.current) renderer.current.depth = depth
  }, [depth, data])
  const selected = data?.nodes.find((node) => node.id === focusedId)
  const neighbors = new Set(
    data?.edges.flatMap((edge) =>
      edge.source === focusedId ? [edge.target] : edge.target === focusedId ? [edge.source] : [],
    ),
  )
  const connected = data?.nodes.filter((node) => neighbors.has(node.id)) ?? []
  const reset = () => {
    setSelectedId(null)
    setTopic(null)
    setSelection(null)
    setParent(null)
  }
  return (
    <section
      className="grid min-h-[560px] overflow-hidden rounded-[10px] border border-border lg:grid-cols-[minmax(0,1fr)_340px]"
      aria-label="Explore memory"
    >
      <div className="relative min-w-0 bg-[#141619]">
        <canvas
          ref={canvas}
          role="img"
          aria-label="Memory graph. Select a person or topic using search or the connection list."
          className="block h-[65vh] min-h-[560px] w-full touch-none"
        />
        <div className="pointer-events-none absolute left-5 right-5 top-5">
          <h2 className="m-0 text-[15px] font-semibold">{selected?.label ?? "Your connections"}</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            {selected
              ? `${connected.length} visible connections · click to follow`
              : "People and the topics that connect them"}
          </p>
        </div>
        <div className="absolute bottom-5 left-5 right-5 flex flex-wrap items-center gap-2">
          <Button type="button" onClick={reset}>
            Show all
          </Button>
          <Button type="button" aria-label="Zoom out" onClick={() => renderer.current?.zoom(1 / 1.3)}>
            −
          </Button>
          <Button type="button" aria-label="Zoom in" onClick={() => renderer.current?.zoom(1.3)}>
            +
          </Button>
          <Button type="button" aria-pressed={motion} onClick={() => setMotion(!motion)}>
            {motion ? "Pause motion" : "Animate"}
          </Button>
          <Button type="button" aria-pressed={depth} onClick={() => setDepth(!depth)}>
            3D
          </Button>
          <span className="ml-auto text-xs text-muted-foreground">Drag to rotate · scroll to zoom</span>
        </div>
        {graph.isPending ? (
          <p className="absolute left-5 top-24 text-xs text-muted-foreground">Loading connections…</p>
        ) : null}
        {graph.error ? (
          <p role="alert" className="absolute left-5 top-24 text-xs text-bad">
            {errorText(graph.error)}
          </p>
        ) : null}
      </div>
      <aside
        aria-label="Quick lookup"
        className="flex max-h-[80vh] flex-col gap-5 overflow-y-auto border-t border-border bg-card p-5 lg:max-h-[max(560px,65vh)] lg:border-l lg:border-t-0"
      >
        <MemorySearch selection={selection} onSelectionChange={selectMeaning} onPersonSelect={selectNode} />
        {selected ? (
          <section aria-label="Selected memory" className="border-t border-border pt-4">
            <h3 className="m-0 text-[15px] font-semibold">{selected.label}</h3>
            <p className="mt-1 text-xs text-muted-foreground">
              {selected.kind === "person" ? "Saved topics" : "Connected people"}
            </p>
            <ul className="m-0 list-none space-y-1 p-0">
              {connected.map((node) => (
                <li key={node.id}>
                  <button
                    type="button"
                    onClick={() => selectNode(node.id)}
                    className="w-full rounded-md px-2 py-1.5 text-left text-[13px] hover:bg-secondary"
                  >
                    {node.label}
                  </button>
                </li>
              ))}
            </ul>
          </section>
        ) : (
          <p className="m-0 text-xs text-muted-foreground">
            Select a result or a node to highlight its connections. Links reflect saved evidence, not
            similarity alone.
          </p>
        )}
        {data ? (
          <p className="mt-auto text-xs text-muted-foreground">
            {data.nodes.length.toLocaleString()} nodes · {data.edges.length.toLocaleString()} links
            {data.truncated ? " · Showing a subset; search to focus" : ""}
          </p>
        ) : null}
      </aside>
    </section>
  )
}
