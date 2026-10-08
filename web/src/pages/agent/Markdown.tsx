import { Fragment, type ReactNode } from "react"

import { blocks, type Block } from "@/lib/agent/markdown"

// Codex's replies are Markdown. This renders lib/agent/markdown's blocks, with inline code, bold
// and links, as elements, never as HTML.

const INLINE = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^)\s]+\))/g
const LINK = /^\[([^\]]+)\]\(([^)\s]+)\)$/

function Inline({ text }: { text: string }) {
  return (
    <>
      {text.split(INLINE).map((part, index) => {
        const key = `${index}:${part}`
        if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
          return (
            <code
              key={key}
              className="rounded bg-secondary px-1.5 py-px font-mono text-[12px] text-foreground"
            >
              {part.slice(1, -1)}
            </code>
          )
        }
        if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
          return (
            <strong key={key} className="font-semibold">
              {part.slice(2, -2)}
            </strong>
          )
        }
        const link = LINK.exec(part)
        if (link) {
          return (
            <a
              key={key}
              href={link[2]}
              target="_blank"
              rel="noreferrer"
              className="text-info no-underline hover:underline"
            >
              {link[1]}
            </a>
          )
        }
        return <Fragment key={key}>{part}</Fragment>
      })}
    </>
  )
}

function render(block: Block, key: number): ReactNode {
  switch (block.kind) {
    case "heading":
      return (
        <h3 key={key} className="m-0 mt-1 text-sm font-semibold">
          <Inline text={block.text} />
        </h3>
      )
    case "code":
      return (
        <pre
          key={key}
          className="m-0 overflow-x-auto rounded-[var(--radius-s)] border border-line bg-background px-3 py-2.5 font-mono text-[12px] leading-[1.55] text-foreground"
        >
          {block.text}
        </pre>
      )
    case "list": {
      const List = block.ordered ? "ol" : "ul"
      return (
        <List
          key={key}
          className={`m-0 flex flex-col gap-1 pl-5 ${block.ordered ? "list-decimal" : "list-disc"} marker:text-faint`}
        >
          {block.items.map((item, index) => (
            <li key={`${index}:${item}`} className="pl-1">
              <Inline text={item} />
            </li>
          ))}
        </List>
      )
    }
    case "table":
      return (
        <div key={key} className="overflow-x-auto rounded-[var(--radius-s)] border border-line">
          <table className="w-full border-collapse text-xs">
            <thead>
              <tr>
                {block.head.map((cell, index) => (
                  <th
                    key={`${index}:${cell}`}
                    className="whitespace-nowrap border-b border-line bg-card px-3 py-2 text-left text-[11px] font-semibold uppercase tracking-[.06em] text-faint"
                  >
                    <Inline text={cell} />
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, rowIndex) => (
                <tr key={`${rowIndex}:${row.join("|")}`} className="border-b border-line last:border-b-0">
                  {row.map((cell, index) => (
                    <td key={`${index}:${cell}`} className="px-3 py-2 align-top tabular-nums">
                      <Inline text={cell} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )
    case "paragraph":
      return (
        <p key={key} className="m-0 whitespace-pre-wrap">
          <Inline text={block.text} />
        </p>
      )
  }
}

export function Markdown({ text }: { text: string }) {
  return <div className="flex flex-col gap-3 leading-[1.6]">{blocks(text).map(render)}</div>
}
