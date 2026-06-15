# CRUX

Local-first context layer for AI coding agents. Capture what you decide and learn;
CRUX **automatically injects** the relevant pieces into your coding agent so you
stop re-explaining context and agents stop contradicting decisions you already made.

> Design rationale, architecture decisions, and the build roadmap live in
> [`docs/BUILD_PLAN.md`](docs/BUILD_PLAN.md). Read that first.

## Why it's different
- **Two-tier memory (a trust gate).** Captures land in a **working layer**
  (what you're currently doing — transient, fades). You **promote** the true,
  durable bits into the **main graph**, which agents prioritize. Only verified
  things enter trusted context.
- **Injection is hook-first, not tool-first.** A Claude Code `UserPromptSubmit`
  hook retrieves and injects context on *every* prompt — the model never has to
  remember to call a tool. (MCP tools remain as a fallback.)
- **Hybrid retrieval (vector + keyword, fused with RRF)**, not vector-only — so it
  surfaces the *decision* and the *constraint*, not abandoned exploratory notes.
- **Supersession, never delete.** Newer decisions demote older contradictory ones
  but keep full history and provenance.
- **One SQLite file.** No second vector DB to keep in sync. Trivial to back up.
- **Runs fully offline by default** (deterministic fake embedding/enrichment), so
  you can wire and test the whole loop before adding any API key.

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
crux setup     # one-time: keys + global hook + hotkey
crux app       # runs in your tray; owns the global hotkey; opens the dashboard
```
With `crux app` running: **select text in any app → Cmd/Ctrl+Shift+Space → it's in CRUX.**
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

## Automatic injection into Claude Code
```bash
crux install-hook        # explicit; prints exactly what it writes to .claude/settings.json
```
From then on, every prompt in that project gets a `[CRUX CONTEXT]` block injected
automatically. Remove it any time by editing `.claude/settings.json`.

## MCP (fallback / other agents)
```bash
pip install -e ".[mcp]"
crux mcp                 # exposes get_context / add_context
```

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
