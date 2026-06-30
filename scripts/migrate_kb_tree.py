"""One-shot data migration: stamp `subject_path` onto an exported KB so the
committed knowledge base carries the new tree format.

Every fact in the demo KB (`crux_kb.json`) is about CRUX, so it's filed under a
stable `crux/` root, split by the area it's about. A fact that already names a
`subject` keeps it as the leaf segment; otherwise its `domain` is the area. Facts
that already have a `subject_path` are left untouched (idempotent).

    python -m scripts.migrate_kb_tree [path-to-kb.json] [--root crux]

This only rewrites the JSON. On import, the store rebuilds the node tree from
these paths; with a real provider, `crux build-tree` can refine placement further.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from crux.processing import slug

# domain → a readable area segment under the root (kept close to the domain so the
# tree reads like documentation: crux/technical, crux/users, crux/market, …)
_AREA = {
    "product": "product", "technical": "technical", "user": "users",
    "market": "market", "competitor": "competitors", "legal": "legal",
    "process": "process", "other": "general",
}


def path_for(item: dict, root: str) -> str:
    subject = slug(item.get("subject") or "")
    area = _AREA.get(item.get("domain") or "other", "general")
    segs = [slug(root), area]
    if subject and subject != area:
        segs.append(subject)
    return "/".join(s for s in segs if s)


def main(path: str = "crux_kb.json", root: str = "crux") -> None:
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    items = data.get("items", data if isinstance(data, list) else [])
    changed = 0
    for it in items:
        if it.get("subject_path"):
            continue
        it["subject_path"] = path_for(it, root)
        changed += 1
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    paths = sorted({it.get("subject_path", "") for it in items})
    print(f"✓ stamped subject_path on {changed}/{len(items)} item(s) → {p}")
    print("  tree paths:", ", ".join(paths))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--root=")), "crux")
    main(args[0] if args else "crux_kb.json", root)
