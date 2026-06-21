# CRUX live demo — runbook (real, on your laptop)

Goal: record Claude Code pulling **real** context from CRUX as a visible step you
can expand, then cut to the CRUX dashboard and show the same session. ~90 seconds.

---

## 1. One-time setup (≈5 min)

```bash
# from the CRUX repo folder
pip install -e ".[mcp,anthropic,server]"

# (recommended) crisp synthesis + classification on camera:
export ANTHROPIC_API_KEY=sk-ant-...        # works offline too, just less polished

# seed a believable demo project (writes to ~/.crux)
python scripts/seed_demo_project.py        # prints exactly what the agent will pull

# register CRUX as an MCP server in Claude Code, then restart Claude Code
crux install-mcp
```

After restarting Claude Code, run `/mcp` — you should see **crux** with the tools
`get_context` and `log_work`.

> Optional (the "it's automatic" talking point): `crux install-hook` also wires the
> SessionStart / UserPromptSubmit / Stop hooks so context flows with no tool call.
> For the *visible, expandable* demo we use the MCP tool instead.

Open the dashboard in another window so it's ready to cut to:
```bash
crux start          # opens http://localhost:7432  → Working tab → "Data sync feature"
```

---

## 2. The prompt (paste this into Claude Code)

> I'm picking the **data-sync feature** back up. Use the CRUX `get_context` tool to
> load what we've already decided, then propose how to implement **write-conflict
> resolution** — and make sure it respects our existing constraints.

This reliably makes the agent call `get_context`, which shows up as an **expandable
step**, and then visibly honor the constraints (Postgres, incremental sync, under
200ms, no Friday deploys).

> Reliability booster: drop a `CLAUDE.md` in the project with one line —
> `Always call the CRUX get_context tool before starting a task.` — so it never skips it.

---

## 3. Recording flow (what to do on camera)

1. **Type the prompt.** Let the agent call `crux - get_context(...)` — a step appears.
2. **Expand the step** (the context block) — point out it pulled your real
   decisions, the open question, and the team constraints. *"This came from CRUX,
   not from me re-typing it."*
3. **Let it answer** — it proposes conflict resolution while respecting the
   constraints. Call that out: *"It knew we're on Postgres, syncing incrementally,
   under 200ms, and no Friday deploys — I never said any of that."*
4. **Cut to the dashboard** (`localhost:7432` → Working → "Data sync feature").
   Show: the **Intent**, the **Working memory**, the captured **cards** tagged
   `via claude-code`, and the **Sessions** timeline. *"And here's that same project
   memory, building itself as I work."*
5. *(optional)* Mention: *"In normal use this is automatic — a hook injects it on
   every prompt and captures decisions at the end of each turn. No tool call needed."*

---

## 4. Reset between takes

The seed adds a fresh "Data sync feature" project each run. For a clean take:

```bash
rm -rf ~/.crux            # wipe everything, then re-seed
python scripts/seed_demo_project.py
```

Or just delete the thread from the dashboard (open it → Finish/⋯ → delete).

---

## 5. If something misbehaves

- **`/mcp` doesn't show crux** → restart Claude Code; or run `crux install-mcp`
  inside the project folder (writes a local `.mcp.json`) and approve it.
- **Agent doesn't call the tool** → use the `CLAUDE.md` one-liner above, or say
  "call get_context first" explicitly.
- **Working memory looks like a bullet list, not a paragraph** → set
  `ANTHROPIC_API_KEY` (offline mode can't synthesize prose) and re-seed.
- **Dashboard empty** → make sure you didn't set `CRUX_HOME` differently in the
  terminal you seeded vs. the one running `crux start`.
