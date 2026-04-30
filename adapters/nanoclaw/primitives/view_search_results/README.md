# view_search_results

Show search results in a terminal-first review surface.

V1 has two modes:

- `results_io.py view` renders a compact table for final answers, logs, and
  simple shells.
- `search_tui.py` opens a keyboard-first review workspace with chat messages on
  the left, a persistent input box at the bottom, and a right pane that shows
  search runs or candidates.

Open the TUI in search-run browser mode:

```bash
./powerclaw
```

Open a specific result set:

```bash
./powerclaw \
  --state .powerpacks/runs/search-network-<uuid>-<query>.json
```

Resume the latest chat thread, or a specific chat thread:

```bash
./powerclaw --resume
./powerclaw --resume thread-...
```

Resume the newest result set:

```bash
./powerclaw --resume-run
```

Plain text input is sent to NanoClaw when an agent bridge is configured. By
default the TUI uses:

```bash
pnpm --silent -C <nanoclaw-dir> run chat <message>
```

Override it with:

```bash
POWERPACKS_AGENT_COMMAND='pnpm -C /path/to/nanoclaw run chat' \
  ./powerclaw
```

If the command needs the prompt in the middle, use `{prompt}`:

```bash
./powerclaw \
  --agent-command 'my-agent --message {prompt}'
```

Validate without curses:

```bash
./powerclaw \
  --state .powerpacks/runs/search-network-<uuid>-<query>.json \
  --dump --limit 5
```

Replay commands without curses:

```bash
./powerclaw \
  --state .powerpacks/runs/search-network-<uuid>-<query>.json \
  --review-log /tmp/powerpacks-review.jsonl \
  --command "/filter cursor" \
  --command "/keep strong match"
```

Supported TUI commands:

- `/skills`
- `/session`
- `/resume`
- `/runs`
- `/back`
- `/reload`
- `/start-nanoclaw`
- `/filter text`
- `/clear`
- `/select N`
- `/open`
- `/keep [note]`
- `/reject [note]`
- `/tag tag1 tag2`
- `/note text`
- `/quit`

Skill invocations use slash form. The primary Powerpacks entrypoint is:

```bash
/search-network who are software engineers in sf
```

Typing `$` as the first input character is normalized to `/` for compatibility.
The area below the input shows fuzzy autocomplete suggestions only while input
starts with `/`. Press Tab to accept the top suggestion.

Exit behavior:

- `/quit` exits immediately.
- Ctrl-C must be pressed twice within 3 seconds to exit.
- Escape does not exit; it only prints an instruction message.
- On exit, the CLI prints commands for resuming chat or the latest result set.

In search-run browser mode, Enter or `/open` loads the selected run. In results
mode, Enter describes the selected candidate and `/open` shows their LinkedIn
URL. Plain text chats with NanoClaw and includes the active Powerpacks state
path, query, review log, visible candidate count, and selected candidate as
context.

Session/resume behavior:

- PowerClaw chat uses the installed `cli-threaded` NanoClaw channel at
  `data/cli-threaded.sock`.
- `./powerclaw` resumes the newest local PowerClaw thread by default.
- `./powerclaw --new` creates a new NanoClaw-backed thread.
- `./powerclaw --resume <id>` resumes a specific thread.
- Resumed threads replay prior user/agent chat from NanoClaw's session
  `inbound.db` and `outbound.db` into the TUI chat pane.
- `powerclaw --resume-run` and `/resume` resume the newest Powerpacks
  search-result artifact in the TUI.
- `/session` shows active NanoClaw session rows from `data/v2.db`.

Keyboard shortcuts:

- Left arrow returns to the search-run browser.
- Up/down changes the selected search or candidate.
- PageUp/PageDown moves by 10 rows.

NanoClaw requirements:

- `powerclaw` can start the daemon, which must expose `data/cli-threaded.sock`
  for threaded chat and `data/cli.sock` for the stock CLI channel.
- The CLI channel must be wired to an agent group.
- The expected agent Docker image must exist.
- Anthropic credentials must be available to the NanoClaw runtime, usually via
  OneCLI setup.

Rules:

- Do not require the agent to paste hundreds of candidates into chat.
- Prefer showing artifact paths and a bounded terminal table.
- Any user decisions in the TUI should become structured review events that a
  later refinement task can consume.
