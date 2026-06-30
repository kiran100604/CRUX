"""Extraction-quality eval — measures whether CRUX KEEPS the signal it ingests.

Two failure modes this catches, the ones that make the KB useless in practice:

  1. VALUE-STRIPPING / VAGUENESS (KB fact extraction). A design-system paste must
     not collapse into "has 36 color tokens with specific hex values". The hexes,
     the class name, the px values are the whole point — they have to survive into
     the fact's displayed title/summary.

  2. NARRATIVE INVENTION / FILLER (working-memory synthesis). Three real captures
     must not bloom into a hallucinated journey ("started with a blank slate,
     began exploring various platforms, looked into competitors for inspiration").
     Working memory has to stay grounded in what was actually captured.

    python -m eval.extract_quality

Runs against whatever processor `Config.load()` resolves — the offline `fake`
provider with no key (a floor; the numbers will be modest), or a real provider
when a key is set. The harness is provider-agnostic on purpose: set your key and
re-run to measure a prompt change instead of vibe-checking it.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

# --- metrics (pure, deterministic — importable + unit-testable) ---------------

# A token counts as CONCRETE if it pins down an actual VALUE rather than gesturing
# at one: a hex colour, a number-with-unit or version, a quoted literal, a
# --css-var / hyphenated identifier. A BARE count ("36 tokens", "2 fonts") does
# NOT qualify — "36 color tokens with specific hex values" names a count but no
# value, and that's exactly the vague shape we want to catch. Multi-word proper
# nouns (Inter, JetBrains Mono) are handled by the must_retain check, not here.
_CONCRETE = re.compile(
    r"#[0-9a-fA-F]{3,8}\b"                     # hex colour
    r"|\d+(?:\.\d+)?\s?(?:px|rem|em|ms|s|%|x|pt|kb|mb|gb)\b"  # number + unit
    r"|\d+\.\d+"                               # a decimal / version
    r"|'[^']+'|\"[^\"]+\""                    # a quoted literal
    r"|--[a-z][a-z0-9-]+"                      # a css custom property
    r"|\b[a-z]+-[a-z][a-z0-9-]{2,}",          # a hyphenated identifier (crux-dark)
    re.I)

# Words that PROMISE a specific without giving it. They're only a problem when the
# summary names no concrete value alongside them — "specific hex values" with no
# hex is vague; "stroke is 1.7px" is fine even though it has no vague word at all.
_VAGUE_MARKER = re.compile(
    r"\b(specific|various|several|certain|some|a number of|guidelines?|"
    r"a set of|multiple|predefined|its own)\b", re.I)


def has_concrete(text: str) -> bool:
    return bool(_CONCRETE.search(text or ""))


def is_vague(summary: str) -> bool:
    """A fact summary is vague when it leans on a 'specific/various/…' promise but
    names no concrete value to back it."""
    s = summary or ""
    return bool(_VAGUE_MARKER.search(s)) and not has_concrete(s)


def value_retention(facts_text: str, expected: list[str]) -> tuple[float, list[str]]:
    """Fraction of the concrete values that survived anywhere into the facts'
    displayed text. Returns (fraction, the ones that were lost)."""
    if not expected:
        return 1.0, []
    hay = (facts_text or "").lower()
    lost = [v for v in expected if v.lower() not in hay]
    return (len(expected) - len(lost)) / len(expected), lost


def filler_hits(memory: str, phrases: list[str]) -> list[str]:
    """The forbidden generic-narrative phrases that showed up in working memory."""
    m = (memory or "").lower()
    return [p for p in phrases if p.lower() in m]


def mention_coverage(memory: str, terms: list[str]) -> tuple[float, list[str]]:
    if not terms:
        return 1.0, []
    m = (memory or "").lower()
    missing = [t for t in terms if t.lower() not in m]
    return (len(terms) - len(missing)) / len(terms), missing


# --- runner -------------------------------------------------------------------

def _fresh_store(tmp: str):
    from crux.config import Config
    from crux.store import Store
    os.environ["CRUX_HOME"] = tmp
    os.environ["CRUX_DB_PATH"] = str(Path(tmp) / "extract_eval.db")
    return Store(Config.load())


def _run_document_case(store, case: dict) -> dict:
    res = store.ingest(case["content"], source_type=case.get("source_type", "paste"),
                       source_ref=case["name"])
    facts = res["facts"]
    shown = "\n".join(f"{f.title} :: {f.summary}" for f in facts)
    retain, lost = value_retention(shown, case.get("must_retain", []))
    vague = [f for f in facts if is_vague(f.summary)]
    return {
        "name": case["name"], "kind": "document",
        "n_facts": len(facts),
        "value_retention": retain, "lost_values": lost,
        "vague_facts": len(vague), "vague_examples": [f.summary for f in vague[:3]],
        "shown": shown,
    }


def _run_working_memory_case(store, case: dict) -> dict:
    t = store.create_thread(case["name"], case.get("intent", ""), seed=False)
    tid = t["id"]
    for s in case.get("steps", []):
        store.add_step(s["content"], thread_id=tid, kind=s.get("kind"), route=True)
    view = store.thread_view(tid)            # synchronous refine (CLI/test path)
    memory = view.get("context", "") or ""
    hits = filler_hits(memory, case.get("forbidden_filler", []))
    cover, missing = mention_coverage(memory, case.get("should_mention", []))
    return {
        "name": case["name"], "kind": "working_memory",
        "filler_hits": hits,
        "mention_coverage": cover, "missing_mentions": missing,
        "memory": memory,
    }


def run(verbose: bool = True) -> list[dict]:
    seed = json.loads((Path(__file__).parent / "extract_cases.json").read_text())
    results: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        store = _fresh_store(tmp)
        prov = f"processing={store.cfg.processing_provider} embedding={store.cfg.embedding_provider}"
        for case in seed.get("document_cases", []):
            results.append(_run_document_case(store, case))
        for case in seed.get("working_memory_cases", []):
            results.append(_run_working_memory_case(store, case))

    if verbose:
        print(f"\nExtraction quality  ·  {prov}\n" + "=" * 64)
        for r in results:
            if r["kind"] == "document":
                print(f"\n[doc] {r['name']}  ·  {r['n_facts']} fact(s)")
                print(f"  value retention : {r['value_retention']*100:5.1f}%"
                      + (f"   LOST: {', '.join(r['lost_values'])}" if r["lost_values"] else "   (all kept)"))
                print(f"  vague facts     : {r['vague_facts']}"
                      + (f"   e.g. \"{r['vague_examples'][0]}\"" if r["vague_examples"] else ""))
                for line in r["shown"].splitlines():
                    print(f"      · {line}")
            else:
                print(f"\n[wm]  {r['name']}")
                ok = "clean" if not r["filler_hits"] else f"FILLER: {', '.join(r['filler_hits'])}"
                print(f"  grounding       : {ok}")
                print(f"  mention coverage: {r['mention_coverage']*100:5.1f}%"
                      + (f"   missing: {', '.join(r['missing_mentions'])}" if r["missing_mentions"] else ""))
                for line in r["memory"].splitlines():
                    print(f"      | {line}")
        print("\n" + "=" * 64)
        docs = [r for r in results if r["kind"] == "document"]
        wms = [r for r in results if r["kind"] == "working_memory"]
        if docs:
            avg_ret = sum(r["value_retention"] for r in docs) / len(docs)
            tot_vague = sum(r["vague_facts"] for r in docs)
            print(f"docs: avg value retention {avg_ret*100:.1f}%  ·  {tot_vague} vague fact(s) total")
        if wms:
            tot_filler = sum(len(r["filler_hits"]) for r in wms)
            print(f"wm  : {tot_filler} filler phrase(s) total across {len(wms)} thread(s)")
        print("Higher retention + zero vague + zero filler is the goal. Set a real "
              "provider key and re-run to measure a prompt change.\n")
    return results


if __name__ == "__main__":
    run()
    sys.exit(0)
