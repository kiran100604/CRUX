"""Retrieval eval. Builds a throwaway store from the seed corpus and reports
recall@k / MRR so retrieval tuning is measured, not guessed.

    python -m eval.run_eval [k]

Uses the offline fake providers, so it runs with no API keys. Recall will be
modest with fake embeddings (lexical-leaning) — the harness is what matters; plug
in a real embedding provider to see true numbers. Reference benchmarks for the
technique: LongMemEval, LoCoMo.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from crux.config import Config
from crux.store import Store


def main(k: int = 5) -> None:
    seed = json.loads((Path(__file__).parent / "queries.json").read_text())
    with tempfile.TemporaryDirectory() as tmp:
        import os
        os.environ["CRUX_HOME"] = tmp
        os.environ["CRUX_DB_PATH"] = str(Path(tmp) / "eval.db")
        store = Store(Config.load())
        for doc in seed["corpus"]:
            store.capture(doc["text"], type_hint=doc.get("type"))

        recalls, rr = [], []
        for case in seed["cases"]:
            results = store.search(case["query"], limit=k)
            titles = " | ".join(r.item.title.lower() for r in results)
            hits = [e for e in case["expect"] if e.lower() in titles]
            recall = len(hits) / len(case["expect"])
            recalls.append(recall)
            rank = next((i + 1 for i, r in enumerate(results)
                         if any(e.lower() in r.item.title.lower() for e in case["expect"])), 0)
            rr.append(1.0 / rank if rank else 0.0)
            print(f"  q={case['query']!r:50} recall@{k}={recall:.2f}  top={results[0].item.title if results else '-'}")

        print(f"\nmean recall@{k} = {sum(recalls)/len(recalls):.3f}   MRR = {sum(rr)/len(rr):.3f}")
        store.close()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 5)
