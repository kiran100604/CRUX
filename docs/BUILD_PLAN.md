# CORTEX — Build Plan & Architecture

**Status:** authoritative. This is the doc we build against. If code and this doc disagree, fix one of them on purpose.
**Version:** 0.2 (supersedes spec 0.1)
**Scope:** Single-user, local-first, automatic context injection into AI coding agents.

---

## 0. North star (read this first)

> Every AI coding agent starts from zero. CORTEX is the one queryable context layer that every agent pulls from automatically, so you stop re-explaining decisions and agents stop contradicting things you already decided.

The product is judged on **one thing**: when you start a task, does the right context show up in the agent *without you doing anything*, and is it *trustworthy*?

Everything below serves that. Capture, storage, dashboard — all plumbing. **Retrieval quality + automatic injection is the product.**

---

## 1. The five problems that actually decide whether this works

Most of the build is easy CRUD. These five are where CORTEX lives or dies. We design for them on day one.

### P1 — Injection must not depend on the model remembering to ask
The naive design ("expose an MCP `get_context` tool and hope the agent calls it") fails in practice. The agent forgets, or queries badly. A YC team building the same product at company scale (Hyper) abandoned MCP-as-primary for exactly this reason and moved to **lifecycle hooks**.

**Decision:** Injection is **hooks-first, MCP-fallback.**
- Claude Code: a `UserPromptSubmit` hook runs on *every* prompt, retrieves relevant context, and returns it via `additionalContext`. The model never has to decide to call anything. (Confirmed contract: hook reads JSON on stdin, prints `{"additionalContext": "..."}` to stdout, exits 0.)
- MCP `get_context` / `add_context` tools remain, as a fallback for agents/tools that don't expose hooks, and for explicit on-demand queries.

**Transparency rule (learned from Hyper's launch backlash):** installing a hook that runs on every prompt is invasive. CORTEX **never silently installs hooks.** `cortex install-hook` is an explicit command, it prints exactly what it writes and where, and `cortex status` shows whether the hook is active. No surprises.

### P2 — Retrieval quality is the whole game, and pure vector search is not enough
"Top-k by cosine similarity" surfaces semantically-near *noise* (the exploratory note you abandoned) over the *decision* and the *constraint* you actually need.

**Decision:** **Hybrid retrieval with Reciprocal Rank Fusion (RRF)** from day one — the same approach Hyper converged on (semantic + full-text, fused with RRF).
- **Semantic**: embedding cosine similarity.
- **Lexical**: SQLite FTS5 keyword search (free — we're already on SQLite).
- **Fuse**: RRF combines the two ranked lists, then we apply **boosts**: `type` (a `decision`/`constraint` outranks a `reference`), `pinned`, and **recency**, and a **penalty** for superseded/archived items.
- This is testable, and we will test it (see §6, eval harness). Retrieval tuning is a Week-2 deliverable, not "polish."

### P3 — Stale / contradictory knowledge must resolve, not pile up
Our own pitch is "agents contradict things already decided." If two contradictory decisions both sit in the store as equally true, CORTEX *causes* the bug it claims to fix. "We ship Friday" later becomes "we ship Monday" — the new one must win, but we must still be able to explain how we got to Monday.

**Decision:** **Supersession, never hard-delete** (Hyper's model, and it's the right one).
- New items can mark older ones `superseded_by`. Superseded items are demoted hard in retrieval but **kept** with full history and provenance.
- "Delete" in the UI is **soft** (`archived`), recoverable.
- Conflict heuristic for v1 (deliberately simple): **trust recency.** Newer human-entered info outranks older. Confidence/role-weighting is v2.

### P4 — Provenance, or no trust
If you can't see *where* a piece of injected context came from, you won't trust it, and one bad injection kills the habit. Every item keeps `source`, `captured_at`, and `raw_content` (the original). Injected context lines carry a compact citation so you can trace any claim back.

### P5 — Capture must capture *intent*, or the store rots
The sharpest critique in the Hyper thread: memory systems "fail to capture intent" — throwaway architecture notes from a one-off spike leak into unrelated sessions forever. CORTEX must distinguish "this is a locked decision" from "this was me thinking out loud."

**Decision:** `type` is the intent signal and it drives ranking. `decision`/`constraint`/`architecture` rank high and don't decay; `reference`/`context`/`exploration` rank lower and **decay with age** unless `pinned`. Agent-written items (via `add_context`) default to low trust and are visible in the dashboard for one-click confirm/discard, so an over-eager agent can't quietly pollute the store.

---

## 2. Changes from spec 0.1 (and why)

| Spec 0.1 | Build plan 0.2 | Why |
|---|---|---|
| MCP `get_context` is the injection path | **Hook-first injection**, MCP is fallback | P1 — models forget to call tools |
| "Semantic similarity, top 3-5" | **Hybrid (FTS5 + vector) + RRF + boosts** | P2 — vector-only surfaces noise |
| `version++` on edit | + **`superseded_by`, never hard-delete, soft `archived`** | P3 — contradictions must resolve |
| Individual vs Main + promote toggle | **Dropped for v1.** Replaced by `pinned` + `type`-based ranking | over-build; it's a *team* seam, no second user exists yet |
| SQLite **+ ChromaDB** | **One SQLite file** (FTS5 + embeddings as BLOB, cosine in Python) | one file = no sync bugs, trivial backup, matches "zero ops"; brute-force cosine is fine <~50k items |
| `embedding vector(1536)` hardcoded | **dimension-agnostic BLOB**; provider chosen in config | Anthropic has **no** embeddings API; 1536 is OpenAI-specific |
| `claude-3-haiku` for embeddings | embeddings = pluggable (fake/OpenAI/Voyage); processing = **current Haiku** | factual correctness |
| Two ports (7432/7433) | **one port**; FastAPI serves the dashboard bundle | fewer moving parts |
| MCP server ↔ FastAPI relationship unclear | **MCP server + hooks talk directly to the SQLite file**; FastAPI only powers dashboard/CLI HTTP | the inject path must not depend on a running web server |

What we kept from 0.1 because it was right: tight scope, aggressive deferral of teams/auth/triggers, Haiku for enrichment, the capture→store→inject framing, dogfooding in Week 4.

---

## 3. Architecture

```
            ┌─────────────────────────────────────────────┐
            │            ~/.cortex/cortex.db               │  ← single source of truth
            │   SQLite: items + items_fts(FTS5) + meta     │     (metadata, raw, embeddings as BLOB)
            └─────────────────────────────────────────────┘
                 ▲          ▲              ▲           ▲
                 │          │              │           │
   ┌─────────────┴──┐  ┌────┴──────┐  ┌────┴───────┐  ┌┴────────────────┐
   │ Hook (inject)  │  │ MCP server│  │  CLI        │  │ FastAPI (dash)  │
   │ UserPromptSub. │  │ get/add   │  │ add/query/  │  │ list/search/    │
   │ → additionalCtx│  │ _context  │  │ list/...    │  │ pin/archive     │
   └────────────────┘  └───────────┘  └─────────────┘  └─────────────────┘
        (Claude Code)     (any MCP        (terminal)       (localhost web UI
                           agent)                            + React SPA served here)

   Shared core library (cortex/): db, retrieval(RRF), embeddings, processing(Haiku), models
   Every entrypoint above is a thin shell over the same core. No logic duplicated per surface.
```

Key property: **the inject path (hook) and the MCP path both go straight to the DB file via the core library.** Neither needs the FastAPI server running. The web server exists only for the human dashboard.

### Tech choices (decided)
- **Python 3.11+**, `uv` for env/deps.
- **SQLite** (stdlib `sqlite3`) — metadata + `raw_content` + embedding BLOBs; **FTS5** virtual table for lexical search. One file at `~/.cortex/cortex.db`.
- **Embeddings**: pluggable `EmbeddingProvider`. Default `fake` (deterministic, offline — so the whole loop runs with zero API keys for plumbing/tests). Real options: OpenAI `text-embedding-3-small` (1536) or Voyage `voyage-3` (Anthropic's recommended embeddings partner). Dimension is whatever the provider returns; never hardcoded.
- **Enrichment**: Anthropic **Haiku** (current model id `claude-haiku-4-5`) for title/summary/type/tags. Falls back to a heuristic `fake` processor when no key, so capture never hard-fails.
- **Retrieval**: brute-force cosine in Python over stored vectors + FTS5, fused with RRF. Swap to `sqlite-vec` only if/when item count makes brute force slow (it won't for solo use).
- **MCP**: official Python MCP SDK.
- **Web**: FastAPI + a minimal React SPA, served as static files from the same FastAPI app on one port (`7432`).

---

## 4. Data model

One table, `items`. (No separate Individual/Main tables. No graph yet — v2 adds typed edges.)

| Field | Type | Notes |
|---|---|---|
| `id` | TEXT (uuid) | PK |
| `raw_content` | TEXT | original captured text — **never lost** (this is the "episode") |
| `title` | TEXT | AI-generated, ≤8 words |
| `summary` | TEXT | AI-generated, 1 sentence |
| `type` | TEXT enum | `decision` / `constraint` / `architecture` / `design` / `reference` / `context` / `exploration` |
| `tags` | TEXT (json array) | 2–4 tags |
| `source` | TEXT? | URL or app name |
| `pinned` | INT (0/1) | replaces "promote to main" — pinned = locked truth, ranks high, never decays |
| `confidence` | REAL | 0–1. human capture = high; agent `add_context` = low until confirmed |
| `superseded_by` | TEXT? | id of the item that replaces this one |
| `archived` | INT (0/1) | soft delete |
| `embedding` | BLOB | float32 vector, provider-dependent length |
| `embedding_model` | TEXT | which model produced it (so we can re-embed on provider change) |
| `content_hash` | TEXT | sha256 of normalized raw_content — for dedup |
| `version` | INT | ++ on edit; triggers re-embed |
| `captured_at` | TEXT (iso8601) | |
| `updated_at` | TEXT (iso8601) | |

Plus `items_fts` (FTS5 over `title`, `summary`, `tags`, `raw_content`) kept in sync via triggers.

**Notes**
- `constraint` and `exploration` added to the enum vs spec 0.1: `constraint` because constraints are the highest-value injection ("must log payment errors to Sentry"); `exploration` to mark think-out-loud notes so they rank low (P5).
- Dedup: on capture, if `content_hash` already exists, skip or bump the existing item instead of creating a near-duplicate (Hyper: "eager extraction + dedup keeps the store bounded").

---

## 5. Interfaces

### 5.1 Hook — the primary injection path (Claude Code)
`cortex hook-inject` is the command the `UserPromptSubmit` hook runs.
- **stdin:** JSON from Claude Code (includes the user's prompt + session metadata).
- **does:** runs hybrid retrieval against the prompt, formats the top N items.
- **stdout:** `{"additionalContext": "[CORTEX CONTEXT]\n• ...\n[END CORTEX CONTEXT]"}`, exit 0.
- **must be fast** (hook timeout is ~30s, but target <300ms) and **must never crash the turn** — on any error it prints `{}` and exits 0.

`cortex hook-capture` runs on the `Stop` event: reads `transcript_path`, pulls the last assistant message, and *optionally* extracts candidate facts (low-confidence, staged for review). v1 can ship inject-only and add capture once inject is trusted.

Installed via:
```jsonc
// merged into .claude/settings.json by `cortex install-hook` (explicit, prints a diff)
{
  "hooks": {
    "UserPromptSubmit": [
      { "matcher": "", "statusMessage": "Loading CORTEX context...",
        "hooks": [{ "type": "command", "command": "cortex hook-inject" }] }
    ]
  }
}
```

### 5.2 MCP tools (fallback + explicit use)
- `get_context(query, limit=5, include_archived=false)` → ranked items + how many were searched.
- `add_context(content, type?, pin=false)` → stored item (id, title, summary). Defaults to **low confidence + staged** unless `pin=true`.

### 5.3 CLI
```
cortex add "text"            # capture from terminal
cortex add --file design.md  # ingest a doc (chunked — a whole file as one embedding retrieves badly)
cortex query "..."           # hybrid search, human-readable
cortex list [--type decision] [--pinned] [--archived]
cortex pin <id> / unpin <id>
cortex supersede <old-id> <new-id>
cortex archive <id> / restore <id>
cortex serve                 # start FastAPI + dashboard on :7432
cortex mcp                   # run MCP server (also: python -m cortex.mcp_server)
cortex install-hook / uninstall-hook / status
cortex hook-inject / hook-capture   # invoked by Claude Code, not by humans
```

### 5.4 HTTP (dashboard backend, FastAPI)
`POST /capture`, `GET /search?q=`, `GET /items`, `POST /items/{id}/pin`, `POST /items/{id}/archive`, `POST /items/{id}/supersede`. Dashboard SPA served at `/`.

---

## 6. Retrieval algorithm (the part to get right)

```
input: query string, limit
1. embed(query) with configured provider          → qvec
2. semantic = top 50 items by cosine(qvec, item.embedding), excluding archived
3. lexical  = top 50 items from items_fts MATCH (query), excluding archived
4. fuse with RRF:  score(item) = Σ 1/(k + rank_in_list)   for k=60      # per Hyper/standard RRF
5. apply multipliers:
      pinned                 ×1.5
      type ∈ {decision,constraint,architecture}  ×1.3
      type ∈ {reference,context,exploration}     ×0.9, and ×recency_decay(age)
      superseded_by != null  ×0.15      # demote hard, don't drop
6. return top `limit`, each with title, summary, type, source, captured_at, id
```

**Eval harness (Week 2, mandatory).** `eval/queries.yaml` holds `query → expected_item_ids`. `cortex-eval` reports recall@k / MRR so retrieval changes are measured, not vibes. Reference benchmarks for the technique: **LongMemEval** and **LoCoMo** (what serious memory systems report on). We don't need SOTA — we need *trustworthy*.

---

## 7. Build plan (4 weeks)

### Week 1 — Core loop, end to end in the terminal
- [ ] `cortex/` package skeleton, `uv` project, config (`~/.cortex/`).
- [ ] SQLite schema + FTS5 + triggers; `db.py` CRUD (insert, dedup-by-hash, supersede, archive, pin).
- [ ] `EmbeddingProvider` (fake default + OpenAI) and `Processor` (Haiku + fake fallback).
- [ ] Capture pipeline: text → enrich → embed → store.
- [ ] Hybrid retrieval (`retrieval.py`) with RRF + boosts.
- [ ] CLI: `add`, `query`, `list`, `pin`, `archive`, `supersede`.
- [ ] **Exit criteria:** `cortex add` then `cortex query` returns sensible ranked results, fully offline (fake providers).

### Week 2 — Automatic injection (the differentiator) + eval
- [ ] `cortex hook-inject` (stdin→retrieval→`additionalContext`, fast, crash-safe).
- [ ] `cortex install-hook` / `status` (explicit, prints diff, no silent install).
- [ ] MCP server: `get_context`, `add_context` (talking straight to the DB).
- [ ] Eval harness + seed `queries.yaml`; tune RRF/boosts against it.
- [ ] **Exit criteria:** in a real Claude Code session, starting a task auto-injects relevant context with zero tool calls; recall@5 measured on the eval set.

### Week 3 — Dashboard + ingestion + polish
- [ ] FastAPI HTTP endpoints; minimal React SPA (list, search, pin, archive, supersede, manual add) served on one port.
- [ ] `cortex add --file` with **chunking** (a whole doc as one vector retrieves badly).
- [ ] `cortex hook-capture` (Stop hook) → staged low-confidence items for one-click confirm.
- [ ] Settings screen (API keys, provider choice, hook status).

### Week 4 — Dogfood
- [ ] Use CORTEX to build CORTEX. Log decisions as you make them.
- [ ] Measure against §8. Tune the only metric that matters: trust.

---

## 8. Success metrics

| Metric | Target | Why it matters |
|---|---|---|
| Items captured, week 1 | ≥ 20 | enough corpus to retrieve from |
| Capture latency (find → confirmed) | < 5s | friction kills capture |
| Inject latency (hook) | < 300ms p50 | runs on every prompt |
| Retrieval recall@5 (eval set) | ≥ 0.8 | P2 — the product |
| Bad injections noticed per day | ~0 | **the trust metric. one bad inject kills the habit** |
| Re-explanations to agent (self-rated weekly) | trending down | the original pain |
| Maintenance time / day | < 3 min | UX is the real differentiator (per every memory vendor that has no adoption) |

The last two rows are the point. A memory system that's technically excellent but annoying to maintain does not get adopted — that's the consistent lesson from teams who've shipped this. Optimize trust and low friction over features.

---

## 9. Explicitly NOT in v1
Teams / multi-user, auth / access-control, the Individual→Main promotion hierarchy, knowledge-graph edges, auto-triggers (GitHub/Slack/Linear), EOD review / session recording, confidence-scoring pipeline beyond the simple human/agent split, notifications, mobile, freshness polling/webhooks. These are real (Hyper does them at company scale) — they're just not how a solo tool earns its first day of trust.
