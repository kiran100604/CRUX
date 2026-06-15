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


class _FakeStdin:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data
