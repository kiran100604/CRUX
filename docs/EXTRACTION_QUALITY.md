# Extraction quality: why the KB and working memory were producing noise

Two reported failures, one root cause, plus the harness to stop guessing about it.

## What went wrong

### 1. KB facts that name a category but throw the value away

Dumping the CRUX design system produced facts like:

- *"The design system includes 36 color tokens with specific hex values"*
- *"The design system includes a dark mode toggle with a specific class name"*
- *"line icons with specific stroke and size guidelines"*

Every one of these **promises a specific and then withholds it**. The actual
payload — `#fdf8ec`, `class='crux-dark'`, `1.7px stroke / round caps / ~15px` —
is exactly what a KB exists to remember, and it's the part that got dropped.

Root cause is in the extraction prompt (`crux/processing.py`, `_MULTI_PROMPT`):

- It asked for a **`summary: exactly one sentence`** and a `title <= 8 words`. A
  one-sentence summary of "36 color tokens" can only be a *description of* the
  tokens, not the tokens — so the model wrote the meta-statement.
- It framed the job as pulling "decisions, constraints, architectural choices" —
  a **dense list of reference values doesn't fit any of those buckets**, so the
  model abstracted it up to one vague line instead of enumerating it.

Note: the raw values are *not* lost from storage — `_store_fact` keeps the whole
chunk in `raw_content`, so FTS can still find a hex. The damage is in the
**displayed** title/summary (the KB card) and the **embedded** descriptor, which
is what a human reads and what retrieval ranks on. Vague text there = a useless
card and a weak vector.

### 2. Working memory that invents a journey

A thread with a handful of real captures synthesized into:

> Started with a blank slate, no prior work or context given… Began exploring
> possible approaches… looked into competitor websites for inspiration…
> Identified the need for a clear goal…

None of that happened. It's the model **filling a narrative scaffold**. The
working-memory prompt (`_CONTEXT_PROMPT`) demanded a "TIMELINE" where *"every step
gets 1-3 indented detail lines"* and *"the headline alone is not enough."* When
the captures are thin, the only way to satisfy "every step needs detail" is to
**manufacture** detail — hence the generic-process filler.

Tellingly, the **offline** timeline (`_offline_timeline`, no LLM) never does this
— it just echoes the real captures. That confirms the filler is a prompt artifact,
not a data problem.

## The fix (this change)

Prompt-level, because that's where the damage is:

- **`_MULTI_PROMPT`** now makes carrying the specifics the explicit job: it lists
  the forbidden vague shapes verbatim ("has 36 color tokens with specific hex
  values") and the grounded replacements ("Color tokens: canvas #fdf8ec, ink
  #080808 …"), and says a dense value list becomes **one consolidated reference
  fact that enumerates the values**, not a meta-statement and not dozens of
  fragments. `summary` is now "one tight sentence that NAMES the concrete values"
  instead of "exactly one sentence."
- **`_CONTEXT_PROMPT` / `_CONTEXT_UPDATE_PROMPT`** gain a hard GROUNDING rule:
  use only what the captures say, never narrate generic process (the exact filler
  phrases are named as forbidden), and detail lines appear **only where the
  captures supply specifics** — a thin capture stays a one-line headline. Short
  and honest beats padded.

These are LLM-path changes; validate them with a real key via the harness below.

## The harness (so this is measured, not vibe-checked)

`eval/extract_quality.py` runs representative inputs through the real pipeline and
scores the two failure modes with deterministic metrics:

- **value retention** — fraction of the concrete values (hexes, class names, px,
  identifiers) that survive into the facts' displayed text.
- **vague facts** — facts whose summary leans on a "specific/various/…" promise
  but names no actual value (a bare count like "36" doesn't rescue it).
- **grounding** — forbidden generic-narrative phrases appearing in working memory.
- **mention coverage** — the real captured specifics that made it into the memory.

```bash
python -m eval.extract_quality
```

It runs against whatever `Config.load()` resolves. With **no key** it uses the
offline `fake` providers — a floor, not a verdict (fake summaries are truncations,
so retention sits ~45% and the vague/filler metrics stay at zero because only the
LLM produces those). **Set a real provider key and re-run** to see true numbers
and to measure a prompt change before/after. Fixtures live in
`eval/extract_cases.json`; add cases as you dogfood.

## Tree-structured KB (the foundation, shipped)

The flat `subject` string is now backed by a **taxonomy tree** — the KB grows like
documentation (a product at the root → areas → topics → details), so new knowledge
has a *place* and contradictions are checked *in context*.

- **Schema** (`db.py`): a `nodes` table (`path`, `title`, `parent`, `description`)
  and a `subject_path` column on items (e.g. `crux/design-system/color`). Both
  added via the in-place migration; existing DBs upgrade on open.
- **Placement** (`processing.py::place`): when a fact is stored, the processor
  picks the node it belongs at — **reusing** an existing path when one fits,
  proposing a new one otherwise. Markdown heading trails ("Review › Conflicts")
  are passed as a structural hint, so even **offline** a document builds a properly
  nested tree from its own structure; the LLM placer handles free-form pastes and
  keeps the root stable. A placement failure never blocks the write — the fact
  just lands unplaced.
- **Node-scoped contradiction** (`store.py::_detect_conflicts`): a new fact is
  compared against its **subtree** (same node + descendants), not the whole KB.
  Within a node the candidates are already about the same thing, so a contradiction
  is flagged at a *lower* similarity (0.72 vs the global 0.90) — it catches
  differently-worded opposites ("timeout is 30s" vs "60s") the global scan missed,
  while not firing on lexical lookalikes in unrelated areas.
- **Reading it** (`store.py::tree`, `/tree` + `/tree/facts`): every node with its
  direct and rolled-up (recursive) fact counts, plus a subtree fact view — the
  data a browsable doc-tree UI renders from.
- **Cross-branch relations** still live in `relations` (the existing graph edges):
  the tree is for navigation/placement, the graph for "this also relates to that."

## Candidate next experiments (not done here)

- **Browsable tree UI** in the dashboard (render `/tree`, click a node → its facts).
- **Fix `Save learnings to Review`** (`promote_thread`): it runs the LLM
  **synchronously on the request thread**, so a slow/unreachable model hangs the
  button with no feedback ("nothing happened"), and it collapses the whole thread
  into one over-merged fact. Background it like `/ingest`, show a pending→result
  state, and place each learning in the tree.
- **Visibility**: a pending/result indicator on every slow action (promote, ingest,
  refine) so a click is never silent.
- **Doc/PDF/URL onboarding**: text extraction in front of the existing ingest →
  value-rich facts → placed in the tree → conflict-checked per node.
- **A `reference`/spec ingest path** that keeps a token list verbatim as one
  pullable reference rather than atomizing it at all.
