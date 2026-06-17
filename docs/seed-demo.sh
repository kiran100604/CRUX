#!/usr/bin/env bash
# Dogfood: document CRUX *inside* CRUX. Loads the product's own knowledge into the
# verified Knowledge Base so the dashboard explains CRUX, and `enhance`/the hook
# pull real intent. Run it, then: crux serve  →  open the Knowledge Base tab.
set -e
CRUX="${CRUX:-crux}"   # override with CRUX='python -m crux.cli' if not on PATH

# shellcheck disable=SC2086  # $CRUX may be a multi-word command (e.g. "python -m crux.cli")
add() { $CRUX add "$1" --type "$2" --main --source "crux-docs" >/dev/null; echo "  + [$2] $1"; }

echo "Seeding CRUX with its own product knowledge…"

# Core — why it exists / what it is / who it's for
add "CRUX is a shared intent graph for AI-built software." decision
add "The problem: AI builds fast but widens the gap between intended and built." constraint
add "Primary users are software teams with a lead and AI-using developers." reference
add "The leader owns verified truth; members propose and use it." decision

# Mid — product decisions / architecture
add "Two-tier memory: working layer for proposals, main graph for verified truth." architecture
add "Promotion is the human validation gate; nothing reaches agents unverified." decision
add "Knowledge is tiered by altitude: Core strategy, Mid planning, Leaf operational." architecture
add "Steer prompts with a directive brief instead of auditing built code." decision
add "Team mode shares one graph via a small server; leader role enforced by token." architecture

# Leaf — operational rules / facts
add "Capture flow is copy-first then hotkey; Windows and Wayland block auto-copy." constraint
add "On Linux use 'crux bind' (GNOME shortcut); on Windows run 'crux app'." reference
add "Agents log decisions via the MCP 'log_work' tool; they land in Review." reference

echo "Done. Now: $CRUX serve  →  open http://127.0.0.1:7432  →  Knowledge Base"
