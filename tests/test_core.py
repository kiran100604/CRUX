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


def test_edit_refiles_tier_and_domain(store):
    # the user can correct an auto-classification (move where a fact is filed)
    it = store.capture("We will deploy on AWS.", type_hint="decision")
    before = store.db.embedding_of(it.id)
    assert store.edit(it.id, tier="core", domain="technical")
    refiled = store.db.get(it.id)
    assert refiled.tier == "core" and refiled.domain == "technical"
    # filing-only edits must NOT re-embed (text is unchanged)
    assert store.db.embedding_of(it.id) == before


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


def test_working_memory_view_and_nominate(store):
    a = store.capture("Exploring auth; token short-lived.", owner="me", proposed=False)
    store.capture("Use JWT 15-min expiry.", type_hint="decision", owner="me", proposed=True)
    # working memory shows only the private item; Review shows only the nomination
    assert [i["id"] for i in store.working_memory(owner="me")] == [a.id]
    assert all("Exploring auth" not in i["title"] for i in store.triage())
    # nominate moves the private item into Review
    assert store.nominate(a.id)
    assert store.working_memory(owner="me") == []
    assert any("Exploring auth" in i["title"] for i in store.triage())


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
    # the popup's save path now lands captures in WORKING memory as raw steps
    # (narrative, not atomized into facts up front)
    from crux.config import Config
    from crux.quickcapture import _save
    cfg = Config.load()
    msg = _save(cfg, "Tried Midjourney, too literal.")
    assert "working memory" in msg or "current thread" in msg
    # no fact was created; it's a step (episode) instead
    assert not store.db.list(scope="individual", limit=10)
    assert store.db.unsorted_steps(), "the capture should be an unsorted working step"


def test_dump_feeds_living_context(store):
    # with an active thread, a dumped card attaches to it, gets a kind tag, and
    # the thread's single living context refines to include it
    t = store.create_thread("Website hero image", "make a brand-cream hero image")
    assert store.current_thread_id() == t["id"]
    res = store.add_step("Tried DALL-E, colors off", source="hotkey", route=True)
    assert res["thread_id"] == t["id"]
    view = store.thread_view(t["id"])
    assert view["card_count"] == 1
    assert view["cards"][0]["kind"]  # classified
    assert view["context"], "the living context should be generated on view"
    assert "DALL-E" in store.thread_brief(t["id"]) or "hero image" in store.thread_brief(t["id"])


def test_context_ownership(store):
    # hand-editing the context makes it the user's; new dumps don't overwrite it,
    # until they explicitly refine
    t = store.create_thread("Refactor auth", "")
    store.add_step("looked at session handling", source="note", route=True)
    store.set_thread_context(t["id"], "MY VISION")
    store.add_step("checked token refresh", source="note", route=True)
    assert store.thread_view(t["id"])["context"] == "MY VISION"
    store.refine_context_now(t["id"])
    assert store.thread_view(t["id"])["context"] != "MY VISION"


def test_dump_classifies_kind(store):
    # dumps are tagged by kind (the only per-card metadata)
    t = store.create_thread("Poster", "poster representing fragmentation")
    store.add_step("https://x.com/design-principles — brand guideline", source="note", route=True)
    store.add_step("Prompt: generate tangled wires resolving into a lens", source="note", route=True)
    kinds = [c["kind"] for c in store.thread_view(t["id"])["cards"]]
    assert "reference" in kinds and "prompt" in kinds


def test_exclude_card_steers_context(store):
    # excluding a card removes it from the context; deleting drops it entirely
    t = store.create_thread("X", "do x")
    keep = store.add_step("keep this idea", source="note", route=True)["card_id"]
    drop = store.add_step("ignore this tangent", source="note", route=True)["card_id"]
    assert store.set_card_included(drop, False)
    cards = {c["id"]: c["included"] for c in store.thread_view(t["id"])["cards"]}
    assert cards[keep] is True and cards[drop] is False
    # excluded text is not fed into a freshly refined context
    ctx = store.refine_context_now(t["id"])["summary"]
    assert "tangent" not in ctx
    assert store.delete_card(drop)
    assert all(c["id"] != drop for c in store.thread_view(t["id"])["cards"])


def test_ask_returns_grounded_sources(store):
    # chat over the KB: retrieve top verified facts + a grounded answer with sources
    for t, ty in [("Payments are processed through Stripe.", "decision"),
                  ("All payment errors must be logged to Sentry.", "constraint")]:
        store.promote(store.capture(t, type_hint=ty).id)
    r = store.ask("what do we know about payments?")
    assert r["sources"], "should surface ranked source facts"
    assert all("id" in s and "n" in s for s in r["sources"])
    assert r["answer"]  # offline = extractive; with a key = synthesized prose
    # empty question is handled
    assert store.ask("")["sources"] == []


def test_route_parser_is_defensive():
    # the real-model path's JSON parser — verified without a key
    from crux.processing import _parse_route
    d = _parse_route('{"kind":"insight","target":"new","new_title":"Lens idea",'
                     '"confidence":0.8,"reason":"different direction"}', set())
    assert d["kind"] == "insight" and d["confidence"] == 0.8
    # garbage degrades to a safe, low-confidence note
    g = _parse_route("not json at all", set())
    assert g["kind"] == "note" and g["confidence"] < 0.45


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


def test_ingest_working_splits_into_typed_entries(store):
    # A pasted chunk becomes several discrete, pre-classified working-memory cards,
    # all carrying the same provenance so the synthesis can attribute them.
    t = store.create_thread("Build the API", "Build a fast JSON API")
    chunk = ("We decided to use Postgres instead of SQLite.\n"
             "The API must respond under 200ms.\n"
             "Should we add rate limiting now?")
    res = store.ingest_working(chunk, thread_id=t["id"], source="paste",
                               source_ref="debug-agent", split=True)
    assert res["count"] == 3
    cards = store.db.thread_steps(t["id"])
    kinds = {c.kind for c in cards}
    assert {"decision", "requirement", "question"} <= kinds
    assert all(c.routed and c.source_ref == "debug-agent" for c in cards)


def test_brief_separates_intent_from_working_memory(store):
    t = store.create_thread("Site", "Make a marketing site", seed=False)
    store.add_step("We decided to go with a one-page layout.", thread_id=t["id"], route=True)
    brief = store.thread_brief(t["id"])
    assert "[INTENT — the goal]" in brief
    assert "Make a marketing site" in brief
    assert "[WORKING MEMORY" in brief
    # intent must lead; working memory is a separate, later section
    assert brief.index("[INTENT") < brief.index("[WORKING MEMORY")


def test_tagged_capture_is_born_classified(store):
    # A user tag at capture is authoritative: the card skips the router (it's
    # created already-routed with that kind), so external/competitor info can be
    # filed as a reference instead of being mistaken for our own decision.
    t = store.create_thread("Sync", "Build a sync engine", seed=False)
    res = store.add_step("Competitor X uses a CRDT-based merge engine.",
                         thread_id=t["id"], kind="reference")
    assert res["tagged"] is True
    card = store.db.get_episode(res["card_id"])
    assert card.routed is True and card.kind == "reference"
    assert card.route_reason == "tagged at capture"


def test_untagged_capture_stays_unrouted_for_the_router(store):
    t = store.create_thread("Sync", "Build a sync engine", seed=False)
    res = store.add_step("Competitor X uses a CRDT-based merge engine.",
                         thread_id=t["id"])
    assert res["tagged"] is False
    card = store.db.get_episode(res["card_id"])
    assert card.routed is False     # left for the (batched) router


def test_invalid_tag_falls_back_to_router(store):
    t = store.create_thread("Sync", "Build a sync engine", seed=False)
    res = store.add_step("Some note.", thread_id=t["id"], kind="bogus")
    assert res["tagged"] is False
    assert store.db.get_episode(res["card_id"]).routed is False


def test_resolve_thread_by_title_and_current(store):
    t = store.create_thread("Launch plan", "Plan the launch")
    assert store.resolve_thread("Launch plan") == t["id"]   # exact active title
    assert store.resolve_thread(t["id"]) == t["id"]          # by id
    assert store.resolve_thread("") == store.current_thread_id()


def test_sessions_autostart_and_rollover_with_checkpoint(store):
    t = store.create_thread("Site", "Build a marketing site")
    store.add_step("We decided on a one-page layout.", thread_id=t["id"], route=True)
    s1 = store.db.active_session(t["id"])
    assert s1 and s1["status"] == "active"
    # force the open session past the idle gap, then capture again
    store.db.update_session(s1["id"], {"last_at": "2020-01-01T00:00:00+00:00"})
    store.add_step("Now adding a pricing section.", thread_id=t["id"], route=True)
    sessions = store.db.list_sessions(t["id"])
    assert len(sessions) == 2
    closed = [x for x in sessions if x["status"] == "closed"]
    active = [x for x in sessions if x["status"] == "active"]
    assert len(closed) == 1 and len(active) == 1
    assert closed[0]["summary"]  # a checkpoint was written at rollover
    # each card belongs to exactly one (different) session
    cards = store.db.thread_steps(t["id"])
    assert len({c.session_id for c in cards}) == 2


def test_finish_thread_checkpoints_open_session(store):
    t = store.create_thread("Launch", "Plan the launch")
    store.add_step("We chose June 1 as the launch date.", thread_id=t["id"], route=True)
    store.finish_thread(t["id"])
    sessions = store.db.list_sessions(t["id"])
    assert sessions and all(s["status"] == "closed" for s in sessions)
    assert sessions[-1]["summary"]


def test_resume_surfaces_after_idle(store):
    t = store.create_thread("Site", "Build a site")
    store.add_step("Decided to use a dark theme.", thread_id=t["id"], route=True)
    s1 = store.db.active_session(t["id"])
    store.db.update_session(s1["id"], {"last_at": "2020-01-01T00:00:00+00:00"})
    store.add_step("How should pricing tiers work?", thread_id=t["id"], route=True)
    # idle the new session too → next view should offer a resume from the checkpoint
    s2 = store.db.active_session(t["id"])
    store.db.update_session(s2["id"], {"last_at": "2020-01-02T00:00:00+00:00"})
    tv = store.thread_view(t["id"])
    assert tv["resume"]
    assert any("pricing" in q.lower() for q in tv["open_questions"])


def test_assemble_context_is_state_aware(store):
    # verified KB facts the assembler should surface for the current state
    store.capture("The sync engine persists to PostgreSQL.", type_hint="decision",
                  scope="main", proposed=False)
    store.capture("Never deploy on Fridays.", type_hint="constraint",
                  scope="main", proposed=False)
    t = store.create_thread("Sync", "Build a data sync feature", seed=False)
    store.add_step("We decided to sync incrementally.", thread_id=t["id"], route=True)
    store.add_step("How should we resolve conflicts?", thread_id=t["id"], route=True)
    pkg = store.assemble_context(t["id"], query="design the database writes")
    assert "[CURRENT TASK]" in pkg["brief"]
    assert "design the database writes" in pkg["brief"]
    assert "[INTENT — the goal]" in pkg["brief"]
    assert "[WORKING MEMORY" in pkg["brief"]
    # KB knowledge retrieved fresh against the current state
    titles = {k["title"] for k in pkg["kb"]}
    assert any("PostgreSQL" in x for x in titles)
    assert any("resolve conflicts" in q.lower() for q in pkg["open_questions"])


def test_assemble_records_usage_payoff(store):
    # the fact shares a word ("checkout") with the work, so it passes the relevance
    # gate offline (real embeddings also catch semantic relations)
    i = store.capture("The checkout flow uses Stripe.", type_hint="decision",
                      scope="main", proposed=False)
    t = store.create_thread("Checkout", "Build the checkout", seed=False)
    store.add_step("Working on the checkout step.", thread_id=t["id"], route=True)
    store.assemble_context(t["id"], query="wire up the checkout payment provider")
    assert store.db.usage_counts().get(i.id, 0) >= 1


def test_relevance_gate_excludes_unrelated_kb(store):
    # an UNRELATED verified fact must NOT leak into a project's context (the
    # 'CRUX background on a trading thread' bug)
    store.capture("Our marketing tagline is bold and minimal.", type_hint="reference",
                  scope="main", proposed=False)
    t = store.create_thread("Sync", "Build the data sync engine", seed=False)
    store.add_step("Designing incremental sync.", thread_id=t["id"], route=True)
    pkg = store.assemble_context(t["id"], query="how should the sync engine batch writes")
    assert pkg["kb"] == []          # nothing relevant → nothing surfaced
    assert "marketing" not in pkg["brief"].lower()


def test_assemble_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("CRUX_HOME", str(tmp_path))
    monkeypatch.setenv("CRUX_DB_PATH", str(tmp_path / "t.db"))
    from fastapi.testclient import TestClient
    from crux.config import Config
    from crux.server import create_app
    c = TestClient(create_app(Config.load()))
    tid = c.post("/threads", json={"intent": "Build an API"}).json()["id"]
    c.post("/hook", json={"content": "We decided to use FastAPI.", "project": tid,
                          "source": "paste", "split": True})
    r = c.post(f"/threads/{tid}/assemble", json={"query": "add an endpoint"}).json()
    assert "[CURRENT TASK]" in r["brief"]
    assert r["intent"] == "Build an API"
    assert c.post("/threads/does-not-exist/assemble", json={}).status_code == 404


def test_hook_session_start_injects_resume(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CRUX_HOME", str(tmp_path))
    monkeypatch.setenv("CRUX_DB_PATH", str(tmp_path / "t.db"))
    from crux import hooks
    from crux.config import Config
    from crux.store import Store
    s = Store(Config.load())
    t = s.create_thread("Site", "Build a marketing site")
    s.add_step("We decided on a one-page layout.", thread_id=t["id"], route=True)
    s.set_current_thread(t["id"])
    s.close()
    assert hooks.hook_session_start() == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "Build a marketing site" in ctx and "one-page layout" in ctx


def test_hook_capture_files_turn_into_working_memory(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CRUX_HOME", str(tmp_path))
    monkeypatch.setenv("CRUX_DB_PATH", str(tmp_path / "t.db"))
    from crux import hooks
    from crux.config import Config
    from crux.store import Store
    s = Store(Config.load())
    t = s.create_thread("API", "Build an API")
    s.set_current_thread(t["id"])
    s.close()
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "uuid": "u1",
                    "message": {"role": "user", "content": "build it"}}) + "\n" +
        json.dumps({"type": "assistant", "uuid": "a1", "message": {"role": "assistant",
                    "content": [{"type": "text",
                                 "text": "We decided to use FastAPI. The API must respond under 200ms."}]}}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr("sys.stdin", _FakeStdin(json.dumps(
        {"session_id": "sess1", "transcript_path": str(transcript)})))
    assert hooks.hook_capture() == 0
    # emits a clickable capture breadcrumb (deep link) back to the agent
    out = json.loads(capsys.readouterr().out.strip())
    crumb = out["hookSpecificOutput"]["additionalContext"]
    assert "captured" in crumb and "/#/project/" + t["id"] in crumb
    s2 = Store(Config.load())
    cards = s2.db.thread_steps(t["id"])
    kinds = {c.kind for c in cards}
    assert {"decision", "requirement"} <= kinds
    assert all(c.source_ref == "claude-code" for c in cards)
    # a second Stop on the unchanged transcript dedupes — no new cards
    monkeypatch.setattr("sys.stdin", _FakeStdin(json.dumps(
        {"session_id": "sess1", "transcript_path": str(transcript)})))
    hooks.hook_capture()
    assert len(s2.db.thread_steps(t["id"])) == len(cards)
    s2.close()


def test_hook_capture_noop_without_active_project(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CRUX_HOME", str(tmp_path))
    monkeypatch.setenv("CRUX_DB_PATH", str(tmp_path / "t.db"))
    from crux import hooks
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "assistant", "uuid": "a1",
        "message": {"role": "assistant", "content": [{"type": "text",
        "text": "We decided to ship."}]}}) + "\n", encoding="utf-8")
    monkeypatch.setattr("sys.stdin", _FakeStdin(json.dumps(
        {"session_id": "s", "transcript_path": str(transcript)})))
    assert hooks.hook_capture() == 0
    assert capsys.readouterr().out.strip() == "{}"


def test_install_writes_all_three_hooks(tmp_path):
    from crux.install import install_claude_hooks
    settings = tmp_path / "settings.json"
    assert install_claude_hooks(settings) == {
        "SessionStart": "installed", "UserPromptSubmit": "installed", "Stop": "installed"}
    data = json.loads(settings.read_text())
    assert "crux hook-session-start" in json.dumps(data["hooks"]["SessionStart"])
    assert "crux hook-capture" in json.dumps(data["hooks"]["Stop"])
    assert install_claude_hooks(settings) == {  # idempotent
        "SessionStart": "already-installed", "UserPromptSubmit": "already-installed",
        "Stop": "already-installed"}


def test_activity_trail_logs_pulls_and_captures_with_links(store):
    t = store.create_thread("Sync", "Build data sync")
    store.ingest_working("We decided to use Postgres.\nThe API must respond under 200ms.",
                         thread_id=t["id"], source="paste", source_ref="claude-code", split=True)
    store.assemble_context(t["id"], query="implement conflict resolution")
    store.add_step("Quick hotkey note", source="hotkey", thread_id=t["id"], route=True)
    ev = store.activity(50)
    kinds = [e["kind"] for e in ev]
    assert "pull" in kinds and "capture" in kinds
    # every row deep-links to the exact project page
    assert all(("/#/project/" + t["id"]) in e["link"] for e in ev if e["thread_id"])
    cap = next(e for e in ev if e["kind"] == "capture" and "claude-code" in (e["detail"] or ""))
    assert cap["count"] == 2 and "decision" in cap["detail"]


def test_deep_link_uses_localhost(store):
    link = store.deep_link("abc", card_id="xyz")
    assert link.endswith("/#/project/abc/card/xyz")
    assert "localhost" in link


def test_subject_scoping_beats_cross_subject_noise(store):
    a = store.capture("The sync service uses Postgres with incremental replication.",
                      type_hint="architecture"); store.promote(a.id, subject="sync service")
    b = store.capture("The billing service uses Postgres for invoice storage.",
                      type_hint="architecture"); store.promote(b.id, subject="billing service")
    res = store.search("architecture of the sync service", limit=3)
    assert res[0].item.id == a.id          # the on-subject fact wins despite shared "Postgres"
    assert res[0].item.subject == "sync service"


def test_subject_enriched_and_editable(store):
    it = store.capture("Auth tokens expire after 15 minutes.", type_hint="reference")
    assert isinstance(it.subject, str)     # subject is set at enrichment (offline: keyword)
    assert store.edit(it.id, subject="Auth Service")
    assert store.db.get(it.id).subject == "auth service"   # normalized lowercase


def test_section_heading_becomes_subject(store):
    doc = ("# Retrieval\nWe fuse vector and keyword search with RRF.\n\n"
           "# Competition\nHyper is a cloud company brain; Glean is enterprise search.\n\n"
           "# Storage\nEverything is one SQLite file with embeddings as a BLOB.")
    res = store.ingest(doc, source_type="file", source_ref="d.md")
    subs = {f.subject for f in res["facts"]}
    assert {"retrieval", "competition", "storage"} <= subs   # headings → subjects


def test_subject_channel_retrieves_across_word_variants(store):
    for f in store.ingest(
        "# Competition\nHyper is a cloud company brain; Glean is enterprise search.\n\n"
        "# Storage\nEverything is one SQLite file with embeddings as a BLOB.",
        source_type="file", source_ref="d.md")["facts"]:
        store.promote(f.id)
    # 'competitors' must reach the 'competition' fact even with no shared raw words
    top = store.search("who are our competitors", scope="main", limit=1)
    assert top and top[0].item.subject == "competition"
    assert store.search("how is data stored", scope="main", limit=1)[0].item.subject == "storage"


def test_route_pending_batches_all_unrouted(store):
    t = store.create_thread("X", "build a sync engine", seed=False)
    for txt in ["We decided to use Postgres.", "The API must respond under 200ms.",
                "How do we handle conflicts?"]:
        store.add_step(txt, thread_id=t["id"], route=False)   # leave unrouted
    n = store.route_pending(t["id"])                          # one batch call
    assert n == 3
    kinds = {c.kind for c in store.db.thread_steps(t["id"])}
    assert {"decision", "requirement", "question"} <= kinds
    assert all(c.routed for c in store.db.thread_steps(t["id"]))


def test_query_embedding_is_cached(store):
    store.capture("The sync engine uses Postgres.", type_hint="decision",
                  scope="main", proposed=False)
    store.search("sync engine storage", scope="main")
    store.search("sync engine storage", scope="main")        # identical → cache hit
    assert len(store._qcache) == 1
