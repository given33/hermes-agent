"""Actor/conversation access scoping for the holographic memory store.

Multi-user gateway deployments run several conversations against ONE
memory_store.db. These tests pin the per-actor isolation contract introduced
to keep those users from sharing a single fact store:

- Legacy (pre-actor) databases migrate in place inside _init_db and stay
  readable, with ids preserved and dedup moving to (content, actor).
- The same content stored by two actors yields distinct rows, each invisible
  to the other; shared rows (actor='') are visible to every caller.
- Guarded mutations (update/remove/feedback) act as NOT FOUND on foreign
  rows instead of touching them.
- Entity resolution is scoped per actor.
- plugins.hermes-memory-store.scoping = "shared" opts back into the global,
  unscoped store even when an identity is present.
"""

import json
import sqlite3

import pytest

from plugins.memory.holographic import holographic as hrr
from plugins.memory.holographic.retrieval import FactRetriever
from plugins.memory.holographic.store import MemoryStore

numpy_required = pytest.mark.skipif(
    not hrr._HAS_NUMPY, reason="HRR retrieval paths require numpy"
)


@pytest.fixture(autouse=True)
def _clean_shared_registry():
    """Each test starts and ends with an empty shared-connection registry."""
    for entry in list(MemoryStore._shared.values()):
        try:
            entry["conn"].close()
        except sqlite3.Error:
            pass
    MemoryStore._shared.clear()
    yield
    leaked = list(MemoryStore._shared)
    for entry in list(MemoryStore._shared.values()):
        try:
            entry["conn"].close()
        except sqlite3.Error:
            pass
    MemoryStore._shared.clear()
    assert not leaked, f"test leaked shared connections: {leaked}"


# ---------------------------------------------------------------------------
# Legacy migration readability
# ---------------------------------------------------------------------------

LEGACY_SCHEMA = """
CREATE TABLE facts (
    fact_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content         TEXT NOT NULL UNIQUE,
    category        TEXT DEFAULT 'general',
    tags            TEXT DEFAULT '',
    trust_score     REAL DEFAULT 0.5,
    retrieval_count INTEGER DEFAULT 0,
    helpful_count   INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    hrr_vector      BLOB
);
CREATE TABLE entities (
    entity_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    entity_type TEXT DEFAULT 'unknown',
    aliases     TEXT DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE fact_entities (
    fact_id   INTEGER REFERENCES facts(fact_id),
    entity_id INTEGER REFERENCES entities(entity_id),
    PRIMARY KEY (fact_id, entity_id)
);
CREATE INDEX idx_legacy_trust ON facts(trust_score DESC);
CREATE VIRTUAL TABLE facts_fts
    USING fts5(content, tags, content=facts, content_rowid=fact_id);
"""


def _build_legacy_db(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.executescript(LEGACY_SCHEMA)
    first = conn.execute(
        "INSERT INTO facts (content, category) VALUES (?, ?)",
        ("Legacy deployment note", "project"),
    ).lastrowid
    conn.execute(
        "INSERT INTO facts (content, category) VALUES (?, ?)",
        ("Legacy preference note", "user_pref"),
    )
    entity_id = conn.execute(
        "INSERT INTO entities (name) VALUES (?)", ("Legacy Owner",)
    ).lastrowid
    conn.execute(
        "INSERT INTO fact_entities (fact_id, entity_id) VALUES (?, ?)",
        (first, entity_id),
    )
    # Populate the FTS index so the rebuild-vs-rebuild comparison is real.
    conn.execute("INSERT INTO facts_fts(facts_fts) VALUES('rebuild')")
    conn.commit()
    conn.close()
    return first


class TestLegacyMigration:
    def test_legacy_db_migrates_in_place_and_stays_readable(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        legacy_first_id = _build_legacy_db(db_path)

        store = MemoryStore(db_path)
        try:
            # Rows survived the table rebuild with ids preserved and every
            # pre-existing row marked shared (actor='').
            rows = store._conn.execute(
                "SELECT fact_id, content, actor FROM facts ORDER BY fact_id"
            ).fetchall()
            assert [r["fact_id"] for r in rows] == [
                legacy_first_id,
                legacy_first_id + 1,
            ]
            assert all(r["actor"] == "" for r in rows)
            assert {r["content"] for r in rows} == {
                "Legacy deployment note",
                "Legacy preference note",
            }

            ent_rows = store._conn.execute(
                "SELECT entity_id, name, actor FROM entities"
            ).fetchall()
            assert ent_rows[0]["actor"] == ""
            assert ent_rows[0]["name"] == "Legacy Owner"

            # Public API reads work against the migrated tables.
            assert store.count_facts() == 2
            contents = {f["content"] for f in store.list_facts(limit=10)}
            assert contents == {"Legacy deployment note", "Legacy preference note"}

            # New schema pieces are in place: actor column + the per-(content,
            # actor) unique index replacing the inline UNIQUE(content).
            facts_cols = {
                r[1] for r in store._conn.execute("PRAGMA table_info(facts)").fetchall()
            }
            assert "actor" in facts_cols
            indexes = {
                r[1] for r in store._conn.execute("PRAGMA index_list(facts)").fetchall()
            }
            assert "idx_facts_content_actor" in indexes

            # Dedup is now per actor: a DIFFERENT actor inserts its own row...
            scoped_dup = store.add_fact("Legacy deployment note", actor="alice")
            assert scoped_dup != legacy_first_id
            # ...while the same (content, actor='') pair still dedups to the
            # migrated row.
            assert store.add_fact("Legacy deployment note") == legacy_first_id

            # Writes keep working through the rebuilt triggers/FTS index.
            new_id = store.add_fact("Post-migration fact about deploy pipelines")
            hits = store.search_facts("deploy")
            assert any(h["fact_id"] == new_id for h in hits)

            # FTS index stayed consistent across the migration: legacy content
            # remains searchable too.
            legacy_hits = store.search_facts("legacy deployment")
            assert any(h["fact_id"] == legacy_first_id for h in legacy_hits)
        finally:
            store.close()

        # Reopening does not re-migrate: counts stable, no duplicated copies,
        # and dedup still resolves migrated rows to their preserved ids.
        reopened = MemoryStore(db_path)
        try:
            # 2 legacy + alice's scoped duplicate + the post-migration fact.
            assert reopened.count_facts() == 4
            assert reopened.add_fact("Legacy deployment note") == legacy_first_id
            assert (
                reopened.add_fact("Post-migration fact about deploy pipelines")
                == new_id
            )
        finally:
            reopened.close()

    def test_migration_preserves_fts_searchability_of_legacy_content(self, tmp_path):
        db_path = tmp_path / "legacy_fts.db"
        first_id = _build_legacy_db(db_path)

        store = MemoryStore(db_path)
        try:
            # A token unique to the second row retrieves exactly that row
            # through the resynced external-content FTS index.
            hits = store.search_facts("preference", actor="")
            assert [h["fact_id"] for h in hits] == [first_id + 1]
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Per-actor isolation
# ---------------------------------------------------------------------------


class TestPerActorIsolation:
    def test_same_content_two_actors_distinct_ids_isolated_visibility(self, tmp_path):
        store = MemoryStore(tmp_path / "scoped.db")
        try:
            alice_id = store.add_fact("Deploy key rotation happens Friday", actor="alice")
            bob_id = store.add_fact("Deploy key rotation happens Friday", actor="bob")
            assert alice_id != bob_id

            # Dedup stays scoped: re-adding under alice returns alice's row.
            assert store.add_fact("Deploy key rotation happens Friday", actor="alice") == alice_id

            alice_view = {f["fact_id"] for f in store.list_facts(actor="alice")}
            bob_view = {f["fact_id"] for f in store.list_facts(actor="bob")}
            assert alice_id in alice_view
            assert bob_id not in alice_view
            assert bob_id in bob_view
            assert alice_id not in bob_view

            # Search honours the same boundary.
            alice_hits = [h["fact_id"] for h in store.search_facts("deploy rotation", actor="alice")]
            bob_hits = [h["fact_id"] for h in store.search_facts("deploy rotation", actor="bob")]
            assert alice_hits == [alice_id]
            assert bob_hits == [bob_id]

            # Counts are scoped; the unscoped count sees everything.
            assert store.count_facts(actor="alice") == 1
            assert store.count_facts(actor="bob") == 1
            assert store.count_facts() == 2

            # Shared rows (actor='') are visible to every identified actor.
            shared_id = store.add_fact("Office wifi password rotates monthly")
            assert shared_id in {f["fact_id"] for f in store.list_facts(actor="alice")}
            assert shared_id in {f["fact_id"] for f in store.list_facts(actor="bob")}
            assert store.count_facts(actor="alice") == 2
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Guarded mutations
# ---------------------------------------------------------------------------


class TestGuardedMutations:
    def test_foreign_rows_act_as_not_found_for_mutations(self, tmp_path):
        store = MemoryStore(tmp_path / "guarded.db")
        try:
            alice_id = store.add_fact("Alice private note", actor="alice")

            assert store.update_fact(alice_id, content="hacked", actor="bob") is False
            assert store.remove_fact(alice_id, actor="bob") is False
            with pytest.raises(KeyError):
                store.record_feedback(alice_id, True, actor="bob")

            # Nothing changed by the foreign attempts.
            facts = store.list_facts(actor="alice")
            assert [f["content"] for f in facts] == ["Alice private note"]

            # The owner mutates normally.
            assert store.update_fact(alice_id, trust_delta=0.1, actor="alice") is True
            feedback = store.record_feedback(alice_id, True, actor="alice")
            assert feedback["helpful_count"] == 1
            assert feedback["new_trust"] > feedback["old_trust"]
            assert store.remove_fact(alice_id, actor="alice") is True
            assert store.list_facts(actor="alice") == []
        finally:
            store.close()

    def test_unscoped_caller_keeps_full_access(self, tmp_path):
        """actor='' keeps the historical single-user behaviour: sees and may
        mutate everything (direct store users, scoping='shared')."""
        store = MemoryStore(tmp_path / "unscoped.db")
        try:
            alice_id = store.add_fact("Alice fact", actor="alice")
            bob_id = store.add_fact("Bob fact", actor="bob")

            assert store.update_fact(alice_id, tags="touched") is True
            feedback = store.record_feedback(bob_id, False)
            assert feedback["new_trust"] < feedback["old_trust"]

            contents = {f["fact_id"] for f in store.list_facts()}
            assert contents == {alice_id, bob_id}
            assert store.remove_fact(alice_id) is True
        finally:
            store.close()

    def test_shared_row_mutable_by_identified_actor(self, tmp_path):
        """Shared rows are visible to identified callers and therefore mutable
        by them -- they are not foreign."""
        store = MemoryStore(tmp_path / "shared.db")
        try:
            shared_id = store.add_fact("Team-wide convention")
            assert store.update_fact(shared_id, tags="convention", actor="alice") is True
            assert store.record_feedback(shared_id, True, actor="bob")["helpful_count"] == 1
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Per-actor entity resolution
# ---------------------------------------------------------------------------


class TestEntityScoping:
    def test_entity_resolution_is_per_actor(self, tmp_path):
        store = MemoryStore(tmp_path / "entities.db")
        try:
            store.add_fact('Meet "Alice Smith" tomorrow', actor="alice")
            store.add_fact('Meet "Alice Smith" yesterday', actor="bob")

            rows = store._conn.execute(
                "SELECT name, actor FROM entities WHERE name = ? ORDER BY actor",
                ("Alice Smith",),
            ).fetchall()
            assert [(r["name"], r["actor"]) for r in rows] == [
                ("Alice Smith", "alice"),
                ("Alice Smith", "bob"),
            ]

            # Resolution within one actor is stable (no duplicate entities).
            eid = store._resolve_entity("Alice Smith", actor="alice")
            assert store._resolve_entity("Alice Smith", actor="alice") == eid

            # A third actor resolves her as a NEW entity, not alice's/bob's.
            carol_eid = store._resolve_entity("Alice Smith", actor="carol")
            assert carol_eid not in {
                r["entity_id"]
                for r in store._conn.execute(
                    "SELECT entity_id FROM entities WHERE actor IN ('alice','bob')"
                ).fetchall()
            }
            assert store._resolve_entity("Alice Smith", actor="carol") == carol_eid
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Retriever-level scoping
# ---------------------------------------------------------------------------


@numpy_required
class TestRetrieverScoping:
    @pytest.fixture
    def scoped(self, tmp_path):
        store = MemoryStore(str(tmp_path / "retrieval.db"))
        store.add_fact("Quarterly report lives in drive", actor="alice", category="work")
        store.add_fact("Birthday party planning notes", actor="bob", category="personal")
        retriever = FactRetriever(store=store)
        yield store, retriever
        store.close()

    def test_search_scopes_candidates(self, scoped):
        store, retriever = scoped
        assert [r["fact_id"] for r in retriever.search("quarterly report", actor="alice")]
        assert retriever.search("quarterly report", actor="bob") == []
        assert [r["fact_id"] for r in retriever.search("birthday party", actor="bob")]

    def test_probe_related_reason_exclude_foreign_facts(self, scoped):
        store, retriever = scoped
        alice_contents = "Quarterly report lives in drive"
        bob_contents = "Birthday party planning notes"

        for call in (
            lambda actor: retriever.probe("report", actor=actor, limit=10),
            lambda actor: retriever.related("report", actor=actor, limit=10),
            lambda actor: retriever.reason(["report"], actor=actor, limit=10),
        ):
            alice_texts = " ".join(r["content"] for r in call("alice"))
            bob_texts = " ".join(r["content"] for r in call("bob"))
            assert alice_contents in alice_texts
            assert bob_contents not in alice_texts
            assert bob_contents in bob_texts
            assert alice_contents not in bob_texts

    def test_bank_cache_keyed_by_category_and_actor(self, scoped):
        store, retriever = scoped
        banks = {
            (r["bank_name"], r["actor"])
            for r in store._conn.execute(
                "SELECT bank_name, actor FROM memory_banks"
            ).fetchall()
        }
        assert ("cat:work", "alice") in banks
        assert ("cat:personal", "bob") in banks
        # No cross-actor bank rows were created.
        assert ("cat:work", "bob") not in banks
        assert ("cat:personal", "") not in banks

        # Bank-vector scoring uses ONLY the caller's own (category, actor) bank.
        results = retriever.probe("quarterly", category="work", actor="alice", limit=10)
        assert all(r["content"] != "Birthday party planning notes" for r in results)
        # Bob asking about the work category has no bank and no visible facts:
        # graceful non-vector fallback returns nothing rather than leaking.
        assert retriever.probe("quarterly", category="work", actor="bob", limit=10) == []

    def test_contradict_accepts_actor_and_stays_in_scope(self, scoped):
        store, retriever = scoped
        # Fewer than two visible facts per actor -> empty, but the actor
        # parameter must be accepted end-to-end without leaking candidates.
        assert retriever.contradict(actor="alice") == []
        assert retriever.contradict(actor="bob") == []


# ---------------------------------------------------------------------------
# Provider-level scoping config
# ---------------------------------------------------------------------------


class TestProviderScoping:
    DB_NAME = "provider.db"

    def _provider(self, tmp_path, **config_overrides):
        from plugins.memory.holographic import HolographicMemoryProvider

        config = {"db_path": str(tmp_path / self.DB_NAME), "hrr_dim": 64}
        config.update(config_overrides)
        return HolographicMemoryProvider(config=config)

    def test_auto_scoping_derives_actor_from_kwargs_precedence(self, tmp_path):
        cases = [
            (
                {"user_id": "user-1", "user_id_alt": "alt-9",
                 "gateway_session_key": "gsk", "chat_id": "chat-7"},
                "user-1",
            ),
            ({"user_id_alt": "alt-9", "gateway_session_key": "gsk"}, "alt-9"),
            ({"gateway_session_key": "gsk", "chat_id": "chat-7"}, "gsk"),
            ({"chat_id": "chat-7"}, "chat-7"),
            ({}, ""),
        ]
        for kwargs, expected in cases:
            provider = self._provider(tmp_path)
            try:
                provider.initialize("sess", **kwargs)
                assert provider._actor == expected, kwargs
            finally:
                provider.shutdown()

    def test_actor_normalization_strips_and_caps_length(self, tmp_path):
        provider = self._provider(tmp_path)
        try:
            provider.initialize("sess", user_id="  " + "u" * 300 + " \n ")
            assert provider._actor == "u" * 191
        finally:
            provider.shutdown()

    def test_shared_scoping_ignores_identity_and_writes_global_rows(self, tmp_path):
        provider = self._provider(tmp_path, scoping="shared")
        try:
            provider.initialize("sess", user_id="user-1", chat_id="chat-7")
            assert provider._actor == ""

            result = json.loads(provider.handle_tool_call(
                "fact_store", {"action": "add", "content": "Shared fact"}
            ))
            row = provider._store._conn.execute(
                "SELECT actor FROM facts WHERE fact_id = ?", (result["fact_id"],)
            ).fetchone()
            assert row["actor"] == ""
        finally:
            provider.shutdown()

    def test_tool_calls_are_scoped_to_provider_actor(self, tmp_path):
        alice = self._provider(tmp_path)
        bob = self._provider(tmp_path)
        try:
            alice.initialize("sess-a", user_id="alice")
            bob.initialize("sess-b", user_id="bob")

            added = json.loads(alice.handle_tool_call(
                "fact_store", {"action": "add", "content": "Alice private"}
            ))
            alice_id = added["fact_id"]
            row = alice._store._conn.execute(
                "SELECT actor FROM facts WHERE fact_id = ?", (alice_id,)
            ).fetchone()
            assert row["actor"] == "alice"

            # Bob's listing cannot see Alice's fact.
            listed = json.loads(bob.handle_tool_call(
                "fact_store", {"action": "list", "limit": 50}
            ))
            assert listed["count"] == 0

            # Wrong-actor mutations come back as clean not-found results.
            updated = json.loads(bob.handle_tool_call(
                "fact_store",
                {"action": "update", "fact_id": alice_id, "trust_delta": 0.4},
            ))
            assert updated == {"updated": False}
            removed = json.loads(bob.handle_tool_call(
                "fact_store", {"action": "remove", "fact_id": alice_id}
            ))
            assert removed == {"removed": False}
            feedback = json.loads(bob.handle_tool_call(
                "fact_feedback", {"action": "helpful", "fact_id": alice_id}
            ))
            assert "error" in feedback
            assert "not found" in feedback["error"]

            # Scoped prompt-block counts.
            assert "Empty fact store" in bob.system_prompt_block()
            assert "1 facts" in alice.system_prompt_block()

            # Scoped prefetch.
            assert bob.prefetch("Alice private") == ""
            assert "Alice private" in alice.prefetch("Alice private")

            # Alice's fact survived bob's attempts unchanged.
            facts = alice._store.list_facts(actor="alice")
            assert [f["content"] for f in facts] == ["Alice private"]
        finally:
            alice.shutdown()
            bob.shutdown()

    def test_on_memory_write_mirror_lands_in_actor_scope(self, tmp_path):
        provider = self._provider(tmp_path)
        try:
            provider.initialize("sess", user_id="alice")
            provider.on_memory_write("add", "user", "Mirrored preference")
            rows = provider._store._conn.execute(
                "SELECT actor FROM facts WHERE content = ?", ("Mirrored preference",)
            ).fetchall()
            assert rows and rows[0]["actor"] == "alice"
        finally:
            provider.shutdown()

    def test_scoped_auto_extraction_uses_actor(self, tmp_path):
        provider = self._provider(tmp_path, auto_extract=True)
        try:
            provider.initialize("sess", user_id="alice")
            provider.on_session_end(
                [{"role": "user", "content": "I prefer dark mode everywhere"}]
            )
            rows = provider._store._conn.execute(
                "SELECT actor FROM facts"
            ).fetchall()
            assert len(rows) == 1
            assert rows[0]["actor"] == "alice"
        finally:
            provider.shutdown()
