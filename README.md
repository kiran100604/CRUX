# CRUX

Local-first context layer for AI coding agents. Capture what you decide and learn;
CRUX **automatically injects** the relevant pieces into your coding agent so you
stop re-explaining context and agents stop contradicting decisions you already made.

> **Using it as a team?** See [`docs/TEAM.md`](docs/TEAM.md) — leader serves, members
> connect, agents pull + log, leader validates.
> **Connect Claude Code:** `crux install-mcp`. **Try it on CRUX itself:** `crux seed-demo`.

> Design rationale, architecture decisions, and the build roadmap live in
> [`docs/BUILD_PLAN.md`](docs/BUILD_PLAN.md). Read that first.

## Why it's different
- **Two-tier memory (a trust gate).** Captures land in a **working layer**
  (what you're currently doing — transient, fades). You **promote** the true,
  durable bits into the **main graph**, which agents prioritize. Only verified
  things enter trusted context.
- **Ambient by default — zero extra steps.** CRUX rides Claude Code's hooks:
  it injects your resume brief at **SessionStart**, the relevant context on every
  **UserPromptSubmit**, and quietly captures the turn's decisions/results into
  working memory at **Stop**. You just code; it captures and injects itself. Open
  the dashboard only when you want to review or curate. (MCP tools are the
  fallback for agents without hooks.)
- **Hybrid retrieval (vector + keyword, fused with RRF)**, not vector-only — so it
  surfaces the *decision* and the *constraint*, not abandoned exploratory notes.
- **Supersession, never delete.** Newer decisions demote older contradictory ones
  but keep full history and provenance.
- **One SQLite file.** No second vector DB to keep in sync. Trivial to back up.
- **Runs fully offline by default** (deterministic fake embedding/enrichment), so
  you can wire and test the whole loop before adding any API key.

## Install in one line
**macOS / Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/kiran100604/crux/claude/pensive-bell-jj7bqr/install/install.sh | bash
```
**Windows (PowerShell):**
```powershell
irm https://raw.githubusercontent.com/kiran100604/crux/claude/pensive-bell-jj7bqr/install/install.ps1 | iex
```
The installer does **everything on its own** — installs CRUX, puts `crux` on your
PATH, configures it offline, **adds the "Capture to CRUX" taskbar icon, installs
the Claude Code hooks**, and launches the dashboard. Nothing else to run.

After that, the only command you ever need is bare **`crux`** — it opens the
dashboard (and on first launch automatically wires up anything not already set).
To capture: **copy text anywhere → click the "Capture to CRUX" icon → pick a tag.**

## Quickstart (offline, zero keys)
```bash
pip install -e .
crux add "Leaning toward Stripe but still comparing fees."   # -> working layer
crux add "All payment errors must be logged to Sentry." --type constraint
crux list --scope individual                                  # see working items + short ids
crux promote <id> --summary "Payments via Stripe; errors to Sentry." --type decision
crux query "building the payment webhook handler"             # both tiers; main ranks first
crux query "payment provider" --scope main                   # verified truth only
crux status
```

## The app (simplest experience)
```bash
pip install -e ".[app]"
crux app       # runs in your tray; owns the global hotkey; opens the dashboard
```
On **first launch** the app opens an in-browser setup wizard — pick your capture
hotkey, optionally paste API keys, toggle the Claude Code hook, click **Finish**.
No terminal setup needed. (You can also run the wizard any time at
`http://127.0.0.1:7432/setup`, or use the terminal flow `crux setup`.)

With `crux app` running: **select text in any app → your chosen chord → it's in CRUX.**
No manual key binding. (macOS needs Accessibility permission for the global hotkey.)

## One-command setup
```bash
crux setup     # guided: API keys (saved locally), global Claude Code hook, capture hotkey
```
Does everything below in one interactive flow. Keys persist in `~/.crux/config.env`
(no shell-rc editing). Then `crux serve` and restart Claude Code.

`crux ctx "<task>"` prints the context block for any tool without a hook (paste into Cursor/ChatGPT).

## Turn on real models
```bash
export CRUX_PROCESSING_PROVIDER=anthropic ANTHROPIC_API_KEY=...   # Haiku enrichment
export CRUX_EMBEDDING_PROVIDER=openai     OPENAI_API_KEY=...      # real embeddings
```

## Automatic capture + injection in Claude Code
```bash
crux install-hook        # explicit; prints exactly the 3 hooks it writes to .claude/settings.json
```
This installs three hooks so CRUX works with **zero extra steps**:
- **SessionStart** → injects your resume brief (intent + working memory + open
  questions + relevant KB) so you pick up where you left off.
- **UserPromptSubmit** → injects a `[CRUX CONTEXT]` block relevant to each prompt.
- **Stop** → captures the turn's decisions/requirements/results into the active
  project's working memory, tagged `via claude-code` (only when a project is
  active, so it never pollutes; deduped per session).

Nothing is silent — the install prints every block, and you can remove any of them
by editing `.claude/settings.json`.

## MCP (fallback / other agents)
```bash
pip install -e ".[mcp]"
crux mcp                 # exposes get_context / add_context
```

## Working threads — an intent + a living context
Working memory is organized as **threads** — a unit of active work. A thread is
just two things: your **intent** (what you're trying to do) and a single living
**context** (a refined statement of where you're headed). Start a thread with the
intent; CRUX seeds the context from your knowledge base. Then you just **dump**
things in as you work — a prompt, a link, a result, a chunk of chat, an idea —
and CRUX continuously **re-refines the context** so any AI instantly knows your
direction. You never organize approaches or document what you tried; if you pivot,
the context follows.

- The **context box** is what gets pasted into any tool — it captures the vision,
  requirements, constraints, and what you've learned (incl. what *not* to do).
- Below it, your **dumps** are a simple list (summary → expand for the full thing),
  each tagged by **kind** (reference / prompt / result / insight / …).
- The only control is **scope**: toggle a card out of the context (or delete it)
  and the context stops talking about it. The context is AI-maintained but yours
  to edit — your edits stick until you hit **↻ refine**.

> The refinement needs an API key to be good — set `CRUX_PROCESSING_PROVIDER=anthropic`
> and `ANTHROPIC_API_KEY`. Offline it falls back to a plain synthesis so the loop still runs.

Switch AI tools mid-task without re-explaining:
```bash
crux brief               # prints the current thread's context — paste into any tool
```
When a thread wraps, promote its durable learnings into the knowledge base for
Review; the thread itself stays put.

## Ask your knowledge (Knowledge Base tab)
The Knowledge Base is **chat-first**: ask a question in plain language and CRUX
retrieves your top verified facts, synthesizes a **grounded answer with [n]
citations**, and surfaces the ranked source facts — click any one to see its raw
text, filing, provenance, and related knowledge. Multi-turn: follow-ups remember
the conversation. A **Browse all** mode (by topic/level/tag + collections) is the
secondary way to wander. Synthesis needs an API key; offline it returns the ranked
facts with an extractive summary.

## Dashboard
```bash
pip install -e ".[server]"
crux serve               # http://127.0.0.1:7432
```

## Global hotkey capture
`crux capture` reads your clipboard and captures it. Bind it to a system-wide key:
```bash
crux hotkey --install     # writes platform snippets to ~/.crux/hotkey/ + prints setup steps
```
- **macOS:** Hammerspoon snippet (copies your selection, then captures) or a Raycast script command.
- **Windows:** an AutoHotkey `.ahk`.
- **Linux:** a script that grabs the X selection → `crux add` (bind via GNOME custom shortcuts or sxhkd).

Default chord is `Cmd/Ctrl + Shift + Space`. Select text anywhere → press it → it's in your working layer.

## Eval
```bash
python -m eval.run_eval    # recall@k / MRR over eval/queries.json
pytest                     # core loop tests, fully offline
```
