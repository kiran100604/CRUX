#!/usr/bin/env python3
"""Seed a believable demo project into your CRUX, so a live Claude Code demo has
real context to pull. Run once:  python scripts/seed_demo_project.py

Creates:
  • verified KB facts (team truth the agent should honor)
  • an active project "Data sync feature" with working memory + an open question
After this, `get_context` / the hook will return a rich, real context block, and
the project shows up in the CRUX dashboard's Working tab.
"""
from crux.config import Config
from crux.store import Store

def main():
    s = Store(Config.load())

    # 1) verified, team-wide knowledge (the "main" graph the agent must respect)
    kb = [
        ("decision",    "We use PostgreSQL for all persistence."),
        ("constraint",  "Never deploy on Fridays."),
        ("constraint",  "All API responses must return in under 200ms."),
        ("reference",   "Auth tokens expire after 15 minutes."),
    ]
    for typ, text in kb:
        it = s.capture(text, type_hint=typ, scope="individual", proposed=False)
        s.promote(it.id)  # graduate into verified main graph

    # 2) the active project, with real working memory captured "as you worked"
    t = s.create_thread("Data sync feature", "Build a fast, reliable data-sync feature")
    for text in [
        "We decided to sync incrementally, not full-table — much faster.",
        "Skipping Friday deploys per the team rule.",
        "How should we resolve write conflicts between devices?",
    ]:
        s.add_step(text, source="agent", source_ref="claude-code",
                   thread_id=t["id"], route=True)
    s.set_current_thread(t["id"])

    # 3) show what a live get_context will return
    pkg = s.assemble_context(t["id"], query="implement write-conflict resolution")
    s.close()
    print("\n✓ Seeded demo project 'Data sync feature' (set as current).\n")
    print("Open the dashboard:  crux start   → Working tab\n")
    print("=" * 64)
    print("This is what Claude Code will pull (get_context / hook):")
    print("=" * 64)
    print(pkg["brief"])

if __name__ == "__main__":
    main()
