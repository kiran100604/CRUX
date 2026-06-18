"""Core loop tests — run fully offline with the fake providers."""

import json
import os
import tempfile

import pytest


@pytest.fixture()
def store():
    tmp = tempfile.mkdtemp()
    os.environ["CRUX_HOME"] = tmp
    os.environ["CRUX_DB_PATH"] = os.path.join(tmp, "test.db")
    from crux.config import Config
    from crux.store import Store
    s = Store(Config.load())
    yield s
    s.close()


def test_capture_and_search(store):
    store.capture("We decided to use Stripe over Razorpay for payments.", type_hint="decision")
    store.capture("All payment errors must be logged to Sentry.", type_hint="constraint")
    results = store.search("payment gateway choice", limit=5)
    assert results
    titles = " ".join(r.item.title.lower() for r in results)
    assert "stripe" in titles


def test_dedup_by_hash(store):
    a = store.capture("identical content here", type_hint="context")
    b = store.capture("identical   content here", type_hint="context")  # whitespace-normalized
    assert a.id == b.id


def test_supersede_demotes(store):
    old = store.capture("We ship on Friday.", type_hint="decision")
    new = store.capture("Actually we ship on Monday now.", type_hint="decision")
    store.supersede(old.id, new.id)
    refreshed = store.db.get(old.id)
    assert refreshed.superseded_by == new.id
    # superseded item must not outrank its replacement
    results = store.search("when do we ship", limit=5)
    ranks = {r.item.id: i for i, r in enumerate(results)}
    if old.id in ranks and new.id in ranks:
        assert ranks[new.id] < ranks[old.id]


def test_archive_hides_from_search(store):
    item = store.capture("some reference note", type_hint="reference")
    assert store.archive(item.id) and store.db.get(item.id).archived
    assert all(r.item.id != item.id for r in store.search("reference note"))


def test_promote_to_main(store):
    item = store.capture("We will use Postgres for storage.", type_hint="decision")
    assert item.scope == "individual"
    assert store.promote(item.id, summary="Storage engine is Postgres.")
    promoted = store.db.get(item.id)
    assert promoted.scope == "main"
    assert promoted.confidence >= 0.9
    assert promoted.promoted_at is not None
    assert promoted.summary == "Storage engine is Postgres."


def test_scope_filter_and_main_ranks_higher(store):
    work = store.capture("Exploring whether to cache auth tokens in redis.", type_hint="exploration")
    truth = store.capture("Auth tokens are cached in Redis with a 5 minute TTL.", type_hint="decision")
    store.promote(truth.id)
    # main-only scope excludes the working item
    main_only = store.search("how are auth tokens cached", scope="main")
    assert all(r.item.scope == "main" for r in main_only)
    # across both tiers, verified truth outranks the working note
    both = store.search("how are auth tokens cached", scope=None)
    ranks = {r.item.id: i for i, r in enumerate(both)}
    if truth.id in ranks and work.id in ranks:
        assert ranks[truth.id] < ranks[work.id]


def test_document_ingest_makes_linked_facts(store):
    doc = ("# Payments\n\n## Gateway\nWe will use Stripe as the gateway.\n\n"
           "## Errors\nAll payment errors must be logged to Sentry.\n\n"
           "# Auth\nAuth uses short-lived JWTs.")
    res = store.ingest(doc, source_type="file", source_ref="design.md", title="design")
    facts = res["facts"]
    # one fact per section, all linked back to the single episode
    assert len(facts) >= 3
    assert all(f.source_episode_id == res["episode"].id for f in facts)
    assert any("Gateway" in (f.locator or "") for f in facts)
    # the raw episode is preserved whole as the source of truth
    ep = store.db.get_episode(res["episode"].id)
    assert ep and "Stripe" in ep.raw_content and ep.source_ref == "design.md"
    # facts are immediately searchable
    assert store.search("how are errors logged", limit=5)


def test_capture_creates_episode(store):
    it = store.capture("We chose Postgres.", type_hint="decision")
    assert it.source_episode_id
    ep = store.db.get_episode(it.source_episode_id)
    assert ep.source_type == "note" and "Postgres" in ep.raw_content


def test_contradiction_flagged_at_write_time(store):
    # two near-identical pricing facts should trip the offline similarity flag
    store.capture("SaaS price is 3000 rupees per seat per month.", type_hint="decision")
    store.capture("SaaS price is 5000 rupees per seat per month.", type_hint="decision")
    conflicts = store.open_conflicts()
    assert conflicts, "expected a contradiction candidate"
    c = conflicts[0]
    # resolving by superseding one clears it from review
    store.supersede(c["a"]["id"], c["b"]["id"])
    assert not store.open_conflicts()


def test_same_document_facts_not_flagged(store):
    doc = ("# A\nWe will use Stripe for payments.\n\n"
           "# B\nWe will use Stripe for payments.")  # identical across sections
    store.ingest(doc, source_type="file", source_ref="d.md")
    # facts from the same episode must never be flagged as contradicting each other
    assert not store.open_conflicts()


def test_dismiss_conflict_sticks(store):
    store.capture("Plan price is 3000 rupees per seat per month.", type_hint="decision")
    store.capture("Plan price is 4000 rupees per seat per month.", type_hint="decision")
    conflicts = store.open_conflicts()
    assert conflicts
    store.dismiss_conflict(conflicts[0]["id"])
    assert not store.open_conflicts()  # dismissed ones don't resurface


def test_edit_reembeds(store):
    it = store.capture("We will deploy on AWS.", type_hint="decision")
    before = store.db.embedding_of(it.id)
    assert store.edit(it.id, summary="We will deploy on Google Cloud Platform instead.")
    after = store.db.embedding_of(it.id)
    assert before != after, "editing the text must re-embed"
    assert store.db.get(it.id).version == 2
    # the edited text is now findable by its new meaning
    titles = [r.item.id for r in store.search("google cloud platform", limit=5)]
    assert it.id in titles


def test_promote_with_refine_reembeds(store):
    it = store.capture("price note", type_hint="exploration")
    before = store.db.embedding_of(it.id)
    store.promote(it.id, title="Pricing is fixed at $29/seat", summary="Seat price is $29 per month.")
    after = store.db.embedding_of(it.id)
    assert before != after, "refining text during promote must re-embed"


def test_process_episode_links_back(store):
    ep = store.create_episode("# T\nUse Redis for caching.", source_type="file", source_ref="n.md")
    facts = store.process_episode(ep.id, ep.raw_content, source_type="file", source_ref="n.md")
    assert facts and all(f.source_episode_id == ep.id for f in facts)


def test_usage_payoff_loop(store):
    item = store.capture("Errors go to Sentry.", type_hint="constraint")
    store.record_usage([item.id, item.id], "error handling", session="s1")
    assert store.db.usage_counts()[item.id] == 2
    assert item.id in store.db.usage_last()
    feed = store.db.recent_usages(10)
    assert feed and feed[0]["item_id"] == item.id and feed[0]["title"]


def test_hook_inject_is_crash_safe(monkeypatch, capsys):
    from crux import hooks
    monkeypatch.setattr("sys.stdin", _FakeStdin("not valid json"))
    rc = hooks.hook_inject()
    assert rc == 0
    assert capsys.readouterr().out.strip() == "{}"


def test_retrieve_endpoint_brief_and_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("CRUX_HOME", str(tmp_path))
    monkeypatch.setenv("CRUX_DB_PATH", str(tmp_path / "c.db"))
    from fastapi.testclient import TestClient

    from crux.config import Config
    from crux.server import create_app
    from crux.store import Store
    s = Store(Config.load())
    i = s.capture("Never touch billing without sign-off.", type_hint="constraint")
    s.promote(i.id)
    s.close()
    c = TestClient(create_app(Config.load()))
    r = c.post("/retrieve", json={"prompt": "work on billing", "user": "kiran",
                                  "session": "s1", "limit": 5}).json()
    assert r["count"] >= 1
    assert "CONSTRAINTS TO HONOR" in r["context"]
    # usage was recorded server-side, tagged with the user
    s = Store(Config.load())
    users = [row["user"] for row in s.db.conn.execute("SELECT user FROM usages")]
    s.close()
    assert "kiran" in users


def test_role_enforcement(tmp_path, monkeypatch):
    monkeypatch.setenv("CRUX_HOME", str(tmp_path))
    monkeypatch.setenv("CRUX_DB_PATH", str(tmp_path / "c.db"))
    monkeypatch.setenv("CRUX_ADMIN_TOKEN", "secret123")
    from fastapi.testclient import TestClient

    from crux.config import Config
    from crux.server import create_app
    from crux.store import Store
    it = Store(Config.load()).capture("We use PostgreSQL.", type_hint="decision")
    c = TestClient(create_app(Config.load()))   # TestClient host is remote, not localhost
    hdr = {"x-crux-token": "secret123"}
    assert c.get("/api/whoami").json()["leader"] is False
    assert c.get("/api/whoami", headers=hdr).json()["leader"] is True
    # member is blocked from validating; leader (token) is allowed
    assert c.post(f"/items/{it.id}/promote", json={}).status_code == 403
    assert c.post(f"/items/{it.id}/promote", json={}, headers=hdr).status_code == 200
    # members can still propose (capture) and use (retrieve)
    assert c.post("/capture", json={"content": "a member idea"}).status_code == 200
    assert c.post("/retrieve", json={"prompt": "database"}).status_code == 200


def test_directive_brief_groups_by_intent(store):
    from crux.hooks import _format
    store.capture("Never touch billing without sign-off.", type_hint="constraint")
    store.capture("We use PostgreSQL for persistence.", type_hint="decision")
    store.capture("Target users are enterprise ops teams.", type_hint="reference")
    results, links = store.retrieve("build the sync feature", limit=5)
    brief = _format(results, links)
    assert "CONSTRAINTS TO HONOR (hard rules):" in brief
    assert "DECISIONS ALREADY MADE:" in brief
    assert "PRODUCT & CONTEXT:" in brief
    # constraints must appear before product-context (directive ordering)
    assert brief.index("CONSTRAINTS TO HONOR") < brief.index("PRODUCT & CONTEXT")


def test_build_snippets_respects_chord():
    from crux import hotkey
    snaps = hotkey.build_snippets(["ctrl", "shift"], "k")
    assert "^+k::" in snaps["crux-capture.ahk"]
    snaps2 = hotkey.build_snippets(["cmd", "shift"], "space")
    assert '"cmd", "shift"' in snaps2["hammerspoon.lua"]
    assert hotkey.chord_label(["ctrl", "shift"], "space") == "Ctrl + Shift + Space"


def test_retrieve_pulls_in_connected_facts(store):
    a = store.capture("Payments are processed through Stripe.", type_hint="decision")
    store.promote(a.id)
    # b extends a, but is worded so a 'stripe' query won't match it directly
    b = store.capture("The finance team owns monthly reconciliation.", type_hint="reference")
    store.promote(b.id)
    store.extend(b.id, a.id, promote=False)
    results, links = store.retrieve("stripe payment processing", limit=1)
    assert [r.item.id for r in results] == [a.id]      # query found only A
    assert [it.id for _, it in links] == [b.id]        # graph pulled in B
    # without expansion, the connected fact is not added
    _, no_links = store.retrieve("stripe payment processing", limit=1, expand=False)
    assert no_links == []


def test_extend_creates_linked_edge_and_promotes(store):
    a = store.capture("Alex is a PM at Stripe.")
    store.promote(a.id)
    b = store.capture("Alex leads payments infra and a team of 5.")
    # extend b -> a: edge recorded, b promoted, both kept (a not superseded)
    assert store.extend(b.id, a.id, reason="adds detail", promote=True)
    assert store.db.get(b.id).scope == "main"
    assert store.db.get(a.id).superseded_by is None  # both still valid
    be = store.relations_of(b.id)
    ae = store.relations_of(a.id)
    assert [x["id"] for x in be["extends"]] == [a.id]
    assert [x["id"] for x in ae["extended_by"]] == [b.id]
    # archiving an endpoint clears its edges
    store.archive(b.id)
    assert store.relations_of(a.id)["extended_by"] == []


def test_private_working_memory_and_nominations(store):
    # private working memory (proposed=False) — not reviewed, owner-scoped
    store.capture("Exploring auth; token expires fast.", owner="alice", proposed=False)
    store.capture("Trying Redis for cache.", owner="bob", proposed=False)
    # a nomination (proposed=True) — reaches Review
    store.capture("Use JWT with 15-minute expiry.", type_hint="decision",
                  owner="alice", proposed=True)
    review = [i["title"] for i in store.triage()]
    assert any("JWT" in t for t in review)             # nomination shown
    assert not any("Exploring auth" in t for t in review)  # private WM hidden
    # retrieval is per-user: alice sees her own WM, bob does not
    ra, _ = store.retrieve("auth token", limit=5, user="alice")
    rb, _ = store.retrieve("auth token", limit=5, user="bob")
    assert any("Exploring auth" in r.item.title for r in ra)
    assert not any("Exploring auth" in r.item.title for r in rb)
    # expiry archives private WM (not nominations)
    # (force-old by editing captured_at would need DB; just assert it runs)
    assert store.expire_working_memory(days=0) >= 2     # both private notes archived
    assert any("JWT" in i["title"] for i in store.triage())  # nomination survives


def test_lens_gathers_relevant_facts(store):
    for t in ["Pricing moves to usage-based billing.",
              "The payment screen must show saved cards.",
              "Deploy the API on AWS Fargate."]:
        store.promote(store.capture(t).id)
    lid = store.create_lens("Payments", "billing and the payment screen")
    assert any(l["name"] == "Payments" for l in store.list_lenses())
    ids = store.lens_item_ids(lid)
    titles = {store.db.get(i).title for i in ids}
    assert any("payment" in t.lower() or "pricing" in t.lower() for t in titles)
    store.delete_lens(lid)
    assert store.list_lenses() == []


def test_domain_classification_and_autolink(store):
    # domain is auto-classified (heuristic offline, LLM when keyed)
    assert store.capture("Users churn at the payment step.").domain == "user"
    assert store.capture("The API uses a Postgres database.").domain == "technical"
    assert store.capture("Our go-to-market is usage-based pricing.").domain == "market"
    # auto-link: promoting connects genuinely-related verified facts ('relates' edge)
    a = store.capture("The sync layer uses a local queue flushed on reconnect.")
    store.promote(a.id)
    b = store.capture("The sync layer uses a local queue, flushed when the device reconnects.")
    store.promote(b.id)
    rel = store.relations_of(a.id)["related"]
    assert [x["id"] for x in rel] == [b.id]
    # and retrieval follows the relates edge
    results, links = store.retrieve("local queue sync", limit=1)
    assert b.id in {it.id for _, it in links} or a.id in {it.id for _, it in links}


def test_tier_classification_and_override(store):
    # enrichment assigns an altitude tier (heuristic offline, LLM when keyed)
    core = store.capture("Our mission is to give AI agents perfect long-term memory.")
    mid = store.capture("Our roadmap this quarter is the Review inbox redesign.")
    leaf = store.capture("Use tabs not spaces in the parser module.")
    assert core.tier == "core"
    assert mid.tier == "mid"
    assert leaf.tier == "leaf"
    # promoting can override the tier, and it persists
    assert store.promote(leaf.id, tier="core")
    assert store.db.get(leaf.id).tier == "core"


def test_triage_flags_conflicts_and_bulk_promotes_clean(store):
    store.capture("We will use PostgreSQL as our primary database.", type_hint="decision")
    store.capture("Plan price is 3000 rupees per seat per month.", type_hint="decision")
    store.capture("Plan price is 7000 rupees per seat per month.", type_hint="decision")
    items = store.triage()
    by_status = {}
    for it in items:
        by_status.setdefault(it["status"], []).append(it)
    assert len(by_status.get("conflict", [])) == 2   # the two prices contradict
    assert len(by_status.get("clean", [])) == 1       # postgres is new/clean
    # each item carries a human-readable implication
    assert all(it["implication"] for it in items)
    # bulk promote only touches clean items; conflicts stay behind
    clean_ids = [it["id"] for it in items if it["status"] == "clean"]
    for cid in clean_ids:
        store.promote(cid)
    assert len(store.db.list(scope="main", limit=100)) == 1
    assert len([i for i in store.triage() if i["status"] == "conflict"]) == 2


def test_quickcapture_save(store):
    # the popup's save path: short text -> a note; long/multiline -> ingested doc
    from crux.config import Config
    from crux.quickcapture import _save
    cfg = Config.load()
    assert _save(cfg, "Use Stripe for payments.").startswith("Captured:")
    assert "fact" in _save(cfg, "Payments via Stripe.\nErrors go to Sentry.\nWebhooks signed.")


def test_valid_chord():
    from crux.hotkey import valid_chord
    assert valid_chord(["ctrl", "shift"], "z")[0]
    assert valid_chord(["ctrl", "alt"], "space")[0]
    assert not valid_chord([], "z")[0]                 # no modifier
    assert not valid_chord(["shift"], "a")[0]          # shift-only isn't enough
    assert not valid_chord(["ctrl", "shift"], "j")[0]  # reserved (DevTools)
    assert not valid_chord(["ctrl"], "w")[0]           # reserved (close tab)
    assert not valid_chord(["ctrl"], ".")[0]           # punctuation not portable


def test_pynput_hotkey_format():
    from crux.hotkey import pynput_hotkey
    assert pynput_hotkey(["ctrl", "shift"], "space") == "<ctrl>+<shift>+<space>"
    assert pynput_hotkey(["cmd", "shift"], "k") == "<cmd>+<shift>+k"


def test_setup_persists_chord_for_tray_app(tmp_path, monkeypatch):
    monkeypatch.setenv("CRUX_HOME", str(tmp_path))
    monkeypatch.setenv("CRUX_DB_PATH", str(tmp_path / "c.db"))
    monkeypatch.setenv("HOME", str(tmp_path))
    from fastapi.testclient import TestClient

    from crux.config import Config
    from crux.hotkey import pynput_hotkey
    from crux.server import create_app
    c = TestClient(create_app(Config.load()))
    c.post("/api/setup", json={"chord_mods": ["ctrl", "shift"], "chord_key": "z",
                               "install_hook": False})
    cfg = Config.load()  # reload from the saved config.env
    assert cfg.hotkey_mods == ("ctrl", "shift") and cfg.hotkey_key == "z"
    assert pynput_hotkey(cfg.hotkey_mods, cfg.hotkey_key) == "<ctrl>+<shift>+z"


def test_first_run_setup_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("CRUX_HOME", str(tmp_path))
    monkeypatch.setenv("CRUX_DB_PATH", str(tmp_path / "c.db"))
    # don't touch the real ~/.claude during tests
    monkeypatch.setenv("HOME", str(tmp_path))
    from fastapi.testclient import TestClient

    from crux.config import Config
    from crux.server import create_app
    c = TestClient(create_app(Config.load()))
    # first run redirects to the wizard
    assert c.get("/", follow_redirects=False).status_code == 307
    assert c.get("/setup").status_code == 200
    assert c.get("/api/setup").json()["configured"] is False
    r = c.post("/api/setup", json={"chord_mods": ["ctrl", "shift"], "chord_key": "z",
                                   "install_hook": False}).json()
    assert r["ok"] and r["chord"] == "Ctrl + Shift + Z"
    # now configured → dashboard serves directly
    assert c.get("/", follow_redirects=False).status_code == 200
    assert c.get("/api/setup").json()["configured"] is True
    assert (tmp_path / "hotkey" / "crux-capture.ahk").exists()


def test_hotkey_snippets_written_as_utf8(tmp_path):
    # Regression: on Windows (cp1252) writing the '✓'-containing snippets crashed.
    from crux import hotkey
    hotkey.run(install=True, out_dir=tmp_path)
    files = list(tmp_path.iterdir())
    assert files, "expected hotkey snippets to be written"
    for f in files:
        f.read_text(encoding="utf-8")  # must decode cleanly


class _FakeStdin:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data
