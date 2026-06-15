# CORTEX

Local-first context layer for AI coding agents. Capture what you decide and learn;
CORTEX **automatically injects** the relevant pieces into your coding agent so you
stop re-explaining context and agents stop contradicting decisions you already made.

> Design rationale, architecture decisions, and the build roadmap live in
> [`docs/BUILD_PLAN.md`](docs/BUILD_PLAN.md). Read that first.

## Why it's different
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
cortex add "We decided to use Stripe over Razorpay for better international SDK support."
cortex add "All payment errors must be logged to Sentry." --type constraint
cortex query "building the payment webhook handler"
cortex status
```

## Turn on real models
```bash
export CORTEX_PROCESSING_PROVIDER=anthropic ANTHROPIC_API_KEY=...   # Haiku enrichment
export CORTEX_EMBEDDING_PROVIDER=openai     OPENAI_API_KEY=...      # real embeddings
```

## Automatic injection into Claude Code
```bash
cortex install-hook        # explicit; prints exactly what it writes to .claude/settings.json
```
From then on, every prompt in that project gets a `[CORTEX CONTEXT]` block injected
automatically. Remove it any time by editing `.claude/settings.json`.

## MCP (fallback / other agents)
```bash
pip install -e ".[mcp]"
cortex mcp                 # exposes get_context / add_context
```

## Dashboard
```bash
pip install -e ".[server]"
cortex serve               # http://127.0.0.1:7432
```

## Eval
```bash
python -m eval.run_eval    # recall@k / MRR over eval/queries.json
pytest                     # core loop tests, fully offline
```
