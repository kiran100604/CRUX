# CRUX for a team

CRUX is a shared, curated **intent graph** for AI-built software. The team's
*why / what / how* lives in one place; every developer's AI agent pulls it so it
builds what was actually intended; and the team leader keeps that knowledge true
and non-contradictory.

> The honest one-liner: **AI lets people build fast, but it widens the gap between
> what was supposed to be built and what got built.** CRUX closes that gap by
> steering every prompt with the team's verified intent — and by letting the
> leader own and validate what counts as truth.

## Roles

| | Can do |
|---|---|
| **Leader** | Owns the KB. Adds/edits knowledge, **validates** proposals, resolves contradictions, sets tiers. |
| **Member** | **Uses** the KB (their agents pull it) and **proposes** to it. Cannot promote — proposals go to Review for the leader. |

Enforcement is server-side via an admin token (see below), not just hidden UI.

## The flow

```
        capture / propose            validate              pull (forward)
  member ───────────────►  Review  ──────────►  Knowledge Base  ──────────►  agents
  agent  (log_work)        (leader)   promote     (verified,        directive    build
                                                   tiered, graph)    brief
```

1. **Leader runs the shared server** (one machine the team can reach):
   ```bash
   crux serve
   ```
   It prints the member connect command and a leader token URL.

2. **Members connect** their machine to it:
   ```bash
   crux connect http://<server-host>:7432 --user <your-name>
   ```
   Now their hook, `crux capture`, and `crux enhance` all use the team graph.

3. **Knowledge gets in**, two ways:
   - **People:** capture as you work (hotkey → green flash) or `crux add "…" --type decision`. Lands in **Review** as a proposal.
   - **Agents (automatic):** via the MCP `log_work` tool — at the end of a task the agent records the decisions/constraints/knowledge it produced, straight into Review.

4. **The leader validates** in the dashboard's **Review** tab: promote good proposals into the KB, resolve any contradictions, set the tier (Core / Mid / Leaf). Only verified knowledge reaches agents.

5. **Agents pull intent** automatically:
   - **Claude Code:** the `UserPromptSubmit` hook injects a directive brief on every prompt (`crux install-hook`).
   - **Any MCP agent:** calls `get_context(task)` before working, `log_work(...)` after.
   - **Any tool / manual:** `crux enhance "build the login flow"` → paste the enriched prompt.

## Capturing without the dashboard (per OS)

| OS | How |
|---|---|
| **Windows** | `crux app` (runs in the tray, owns the hotkey). Select text → **Ctrl+C** → press your chord. |
| **Linux (Wayland)** | `crux bind` once → registers a GNOME shortcut. Select → **Ctrl+C** → chord. |
| **macOS** | `crux app`, or bind `crux capture` in Raycast/Hammerspoon. |

> Copy first (Ctrl+C), then press the chord — Windows and Wayland block apps from
> auto-copying, so CRUX files whatever you just copied.

## Leader: real enforcement (the admin token)

- The person who runs `crux serve` is the leader on that machine (localhost = full control).
- To validate the KB **from another machine**, use the token:
  ```bash
  crux connect http://<host>:7432 --leader <TOKEN>     # token printed by `crux serve`
  # then open  http://<host>:7432/?token=<TOKEN>
  ```
- Members (no token) get a read-only KB + a "Propose" box; the server returns **403** on any KB mutation they attempt.

> Assumes a direct connection (no reverse proxy rewriting client IPs). Behind a
> proxy, require the token for everyone instead of trusting localhost.

## Quality (turns the heuristics smart)

CRUX runs fully offline with deterministic stand-ins. Add keys to make extraction,
tier classification, contradiction-detection and "see also" genuinely smart — no
code changes, same flow:

```bash
crux setup            # paste Anthropic (enrichment/judge) and/or OpenAI (embeddings)
```

## Try it on CRUX itself (dogfood)

Seed a CRUX instance with CRUX's own product knowledge and explore it:

```bash
bash docs/seed-demo.sh     # loads decisions/constraints/context about CRUX
crux serve                 # open http://127.0.0.1:7432 → Knowledge Base
crux enhance "add a new capture surface"   # see the brief it produces
```
