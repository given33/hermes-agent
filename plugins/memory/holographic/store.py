"""SQLite-backed fact store with entity resolution and trust scoring.

Hermes memory store plugin. Rows carry an 'actor' column so a single
database can serve a multi-user gateway deployment without leaking facts
across users: an identified caller sees shared rows (actor='') plus its own
rows; guarded mutations treat foreign rows as not-found. An empty caller
actor keeps the historical unscoped single-user behaviour (every row
visible).
"""

import os
import re
import sqlite3
import threading
from pathlib import Path

try:
    from . import holographic as hrr
except ImportError:
    import holographic as hrr  # type: ignore[no-redef]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    fact_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content         TEXT NOT NULL,
    actor           TEXT NOT NULL DEFAULT '',
    category        TEXT DEFAULT 'general',
    tags            TEXT DEFAULT '',
    trust_score     REAL DEFAULT 0.5,
    retrieval_count INTEGER DEFAULT 0,
    helpful_count   INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    hrr_vector      BLOB
);

CREATE TABLE IF NOT EXISTS entities (
    entity_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    actor       TEXT NOT NULL DEFAULT '',
    entity_type TEXT DEFAULT 'unknown',
    aliases     TEXT DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fact_entities (
    fact_id   INTEGER REFERENCES facts(fact_id),
    entity_id INTEGER REFERENCES entities(entity_id),
    PRIMARY KEY (fact_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_facts_trust    ON facts(trust_score DESC);
CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category);
CREATE INDEX IF NOT EXISTS idx_facts_actor    ON facts(actor);
CREATE INDEX IF NOT EXISTS idx_entities_name  ON entities(name);
CREATE INDEX IF NOT EXISTS idx_entities_actor ON entities(actor);

-- Per-actor dedup: identical content may exist once per actor value, so the
-- legacy inline UNIQUE(content) moved to this (content, actor) index.
CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_content_actor ON facts(content, actor);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
    USING fts5(content, tags, content=facts, content_rowid=fact_id);

CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, content, tags)
        VALUES (new.fact_id, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags)
        VALUES ('delete', old.fact_id, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags)
        VALUES ('delete', old.fact_id, old.content, old.tags);
    INSERT INTO facts_fts(rowid, content, tags)
        VALUES (new.fact_id, new.content, new.tags);
END;

-- Derived cache keyed (bank_name, actor): each actor bundles its visible
-- fact vectors into its own category bank.
CREATE TABLE IF NOT EXISTS memory_banks (
    bank_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    bank_name  TEXT NOT NULL,
    actor      TEXT NOT NULL DEFAULT '',
    vector     BLOB NOT NULL,
    dim        INTEGER NOT NULL,
    fact_count INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(bank_name, actor)
);
"""

# Staging-table DDL for the one-time legacy rebuild migrations below.
_FACTS_STAGING_DDL = """
CREATE TABLE facts_actor_migrated (
    fact_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content         TEXT NOT NULL,
    actor           TEXT NOT NULL DEFAULT '',
    category        TEXT DEFAULT 'general',
    tags            TEXT DEFAULT '',
    trust_score     REAL DEFAULT 0.5,
    retrieval_count INTEGER DEFAULT 0,
    helpful_count   INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    hrr_vector      BLOB
)
"""

_ENTITIES_STAGING_DDL = """
CREATE TABLE entities_actor_migrated (
    entity_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    actor       TEXT NOT NULL DEFAULT '',
    entity_type TEXT DEFAULT 'unknown',
    aliases     TEXT DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

# Trust adjustment constants
_HELPFUL_DELTA   =  0.05
_UNHELPFUL_DELTA = -0.10
_TRUST_MIN       =  0.0
_TRUST_MAX       =  1.0

# Entity extraction patterns
_RE_CAPITALIZED  = re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b')
_RE_DOUBLE_QUOTE = re.compile(r'"([^"]+)"')
_RE_SINGLE_QUOTE = re.compile(r"'([^']+)'")
_RE_AKA          = re.compile(
    r'(\w+(?:\s+\w+)*)\s+(?:aka|also known as)\s+(\w+(?:\s+\w+)*)',
    re.IGNORECASE,
)


def _clamp_trust(value: float) -> float:
    return max(_TRUST_MIN, min(_TRUST_MAX, value))

class MemoryStore:
    """SQLite-backed fact store with entity resolution and trust scoring."""

    # --- Process-wide shared connection registry -------------------------
    # SQLite permits only one writer at a time. Each MemoryStore instance used
    # to open its own connection guarded by its own RLock, so the several
    # providers that coexist in one process (the main agent plus every
    # delegate_task subagent) raced as independent WAL writers. Combined with
    # writes that were not rolled back on error, one connection could leave an
    # open write transaction that pinned the write lock and made every other
    # connection's write fail with "database is locked" for the full busy
    # timeout. All instances for the same database now share ONE connection and
    # ONE re-entrant lock, so access is fully serialized and cross-connection
    # contention is impossible. The shared connection is refcounted, so closing
    # one instance never tears the connection out from under a live sibling.
    _shared: dict = {}
    _shared_guard = threading.Lock()

    def __init__(
        self,
        db_path: "str | Path | None" = None,
        default_trust: float = 0.5,
        hrr_dim: int = 1024,
    ) -> None:
        if db_path is None:
            from hermes_constants import get_hermes_home
            db_path = str(get_hermes_home() / "memory_store.db")
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.default_trust = _clamp_trust(default_trust)
        self.hrr_dim = hrr_dim
        self._hrr_available = hrr._HAS_NUMPY

        # Acquire (or open) the process-wide shared connection for this DB.
        # resolve() (not just expanduser) so symlinked/relative paths to the
        # same file share ONE connection instead of silently reintroducing
        # the multi-writer contention this registry exists to prevent.
        try:
            self._key = str(self.db_path.resolve())
        except OSError:
            self._key = str(self.db_path)
        with MemoryStore._shared_guard:
            entry = MemoryStore._shared.get(self._key)
            if entry is None:
                conn = sqlite3.connect(
                    self._key,
                    check_same_thread=False,
                    timeout=10.0,
                    # Autocommit: every statement is its own transaction, so a
                    # write that raises mid-method can never leave a dangling
                    # transaction (and its write lock) open. The explicit
                    # commit() calls below become harmless no-ops.
                    isolation_level=None,
                )
                conn.row_factory = sqlite3.Row
                entry = {"conn": conn, "lock": threading.RLock(), "refs": 0, "ready": False}
                MemoryStore._shared[self._key] = entry
            entry["refs"] += 1
            self._entry = entry
            self._conn = entry["conn"]
            self._lock = entry["lock"]

        # Initialise the schema once per shared connection.
        with self._lock:
            if not self._entry["ready"]:
                self._init_db()
                self._entry["ready"] = True

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def count_facts(self, actor: str = "") -> int:
        """Return the fact count visible to actor under the shared lock."""

        with self._lock:
            vis_sql, vis_params = self._visibility_sql("actor", actor)
            where = f"WHERE {vis_sql}" if vis_sql else ""
            row = self._conn.execute(
                f"SELECT COUNT(*) FROM facts {where}", vis_params
            ).fetchone()
            return int(row[0] if row else 0)

    @staticmethod
    def _normalize_actor(actor: str) -> str:
        """Normalize a caller-supplied actor identity."""
        return str(actor or "").strip()

    @staticmethod
    def _visibility_sql(column: str, actor: str) -> "tuple[str, list]":
        """SQL fragment restricting column to rows actor may see.

        A row is visible iff it is shared (actor='') or owned by the caller.
        An empty caller identity means unscoped access: no filter is emitted,
        which keeps the historical single-user behaviour for direct store
        users and scoping="shared" deployments.
        """
        actor = MemoryStore._normalize_actor(actor)
        if not actor:
            return "", []
        return f"({column} = '' OR {column} = ?)", [actor]

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> list:
        """Return the column names of table, or [] when it does not exist."""
        try:
            return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        except sqlite3.Error:
            return []

    def _rebuild_facts_table_with_actor(self, src_cols: set) -> None:
        """One-transaction rebuild of a pre-actor facts table.

        The inline UNIQUE(content) constraint cannot be dropped via ALTER, so
        the table is rebuilt into a staging copy carrying the new shape (the
        per-(content, actor) uniqueness lives in the index that _SCHEMA
        creates afterwards). Fact ids are preserved so FTS5 external-content
        rowids stay aligned.
        """
        dst_cols = ["fact_id", "content", "actor"]
        src_exprs = ["fact_id", "content", "''"]
        for col in (
            "category", "tags", "trust_score", "retrieval_count",
            "helpful_count", "created_at", "updated_at",
        ):
            if col in src_cols:
                dst_cols.append(col)
                src_exprs.append(col)
        if "hrr_vector" in src_cols:
            dst_cols.append("hrr_vector")
            src_exprs.append("hrr_vector")

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute("DROP TABLE IF EXISTS facts_actor_migrated")
            self._conn.execute(_FACTS_STAGING_DDL)
            self._conn.execute(
                f"INSERT INTO facts_actor_migrated ({', '.join(dst_cols)}) "
                f"SELECT {', '.join(src_exprs)} FROM facts"
            )
            self._conn.execute("DROP TABLE facts")
            self._conn.execute("ALTER TABLE facts_actor_migrated RENAME TO facts")
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def _rebuild_entities_table_with_actor(self, src_cols: set) -> None:
        """One-transaction rebuild of a pre-actor entities table."""
        dst_cols = ["entity_id", "name", "actor"]
        src_exprs = ["entity_id", "name", "''"]
        for col in ("entity_type", "aliases", "created_at"):
            if col in src_cols:
                dst_cols.append(col)
                src_exprs.append(col)

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute("DROP TABLE IF EXISTS entities_actor_migrated")
            self._conn.execute(_ENTITIES_STAGING_DDL)
            self._conn.execute(
                f"INSERT INTO entities_actor_migrated ({', '.join(dst_cols)}) "
                f"SELECT {', '.join(src_exprs)} FROM entities"
            )
            self._conn.execute("DROP TABLE entities")
            self._conn.execute("ALTER TABLE entities_actor_migrated RENAME TO entities")
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def _init_db(self) -> None:
        """Create tables, indexes, and triggers if they do not exist. Enable WAL mode."""
        # Use the shared WAL-fallback helper so memory_store.db degrades
        # gracefully on NFS/SMB/FUSE-mounted HERMES_HOME (same issue as
        # state.db / kanban.db -- see hermes_state._WAL_INCOMPAT_MARKERS).
        from hermes_state import apply_wal_with_fallback
        apply_wal_with_fallback(self._conn, db_label="memory_store.db (holographic)")

        # Legacy (pre-actor) tables must be rebuilt BEFORE the shipped schema
        # executes: its actor-aware indexes cannot compile against tables
        # missing the column. Runs inside __init__'s shared-connection lock;
        # each rebuild is one explicit transaction so a failure leaves the
        # legacy database untouched.
        facts_migrated = False
        facts_cols = self._table_columns(self._conn, "facts")
        if facts_cols and "actor" not in facts_cols:
            self._rebuild_facts_table_with_actor(set(facts_cols))
            facts_migrated = True

        ent_cols = self._table_columns(self._conn, "entities")
        if ent_cols and "actor" not in ent_cols:
            self._rebuild_entities_table_with_actor(set(ent_cols))

        # memory_banks is a pure derived cache: drop the legacy layout and let
        # _SCHEMA recreate it keyed (bank_name, actor), then refill below.
        refill_banks = False
        bank_cols = self._table_columns(self._conn, "memory_banks")
        if bank_cols and "actor" not in bank_cols:
            self._conn.execute("DROP TABLE memory_banks")
            refill_banks = True

        self._conn.executescript(_SCHEMA)

        # Migrate: add hrr_vector column if missing (safe for existing databases)
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(facts)").fetchall()}
        if "hrr_vector" not in columns:
            self._conn.execute("ALTER TABLE facts ADD COLUMN hrr_vector BLOB")

        if facts_migrated:
            # The rebuilt facts table kept its rowids; resync the external-
            # content FTS index defensively so stale entries cannot linger.
            self._conn.execute("INSERT INTO facts_fts(facts_fts) VALUES('rebuild')")
        if refill_banks:
            self.rebuild_all_vectors()
        self._conn.commit();


    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_fact(
        self,
        content: str,
        category: str = "general",
        tags: str = "",
        actor: str = "",
    ) -> int:
        """Insert a fact owned by actor and return its fact_id.

        Deduplication is scoped to the caller's actor: identical content
        already stored by the SAME actor returns the existing fact_id without
        modifying the row; another actor gets its own row (shared rows carry
        actor=''). Extracts entities from the content and links them to the
        fact.
        """
        with self._lock:
            content = content.strip()
            actor = self._normalize_actor(actor)
            if not content:
                raise ValueError("content must not be empty")

            try:
                cur = self._conn.execute(
                    """
                    INSERT INTO facts (content, actor, category, tags, trust_score)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (content, actor, category, tags, self.default_trust),
                )
                self._conn.commit()
                fact_id: int = cur.lastrowid  # type: ignore[assignment]
            except sqlite3.IntegrityError:
                # Duplicate within THIS caller's scope -- return the existing id.
                row = self._conn.execute(
                    "SELECT fact_id FROM facts WHERE content = ? AND actor = ?",
                    (content, actor),
                ).fetchone()
                if row is None:
                    raise
                return int(row["fact_id"])

            # Entity extraction and linking
            for name in self._extract_entities(content):
                entity_id = self._resolve_entity(name, actor=actor)
                self._link_fact_entity(fact_id, entity_id)

            # Compute HRR vector after entity linking
            self._compute_hrr_vector(fact_id, content)
            self._rebuild_bank(category, actor)

            return fact_id

    def search_facts(
        self,
        query: str,
        category: "str | None" = None,
        min_trust: float = 0.3,
        limit: int = 10,
        actor: str = "",
    ) -> list:
        """Full-text search over the facts visible to actor using FTS5.

        Returns a list of fact dicts ordered by FTS5 rank, then trust_score
        descending. Also increments retrieval_count for matched facts.
        """
        with self._lock:
            query = query.strip()
            if not query:
                return []

            # FTS5 AND-joins tokens by default, which zeroes out recall on
            # natural-language queries. Reuse the retriever's sanitizer
            # (stopword drop + OR-join content tokens). Imported lazily to
            # avoid a store->retrieval import cycle.
            from plugins.memory.holographic.retrieval import FactRetriever

            match_query = FactRetriever._sanitize_fts_query(query)
            params: list = [match_query, min_trust]
            category_clause = ""
            if category is not None:
                category_clause = "AND f.category = ?"
                params.append(category)
            vis_sql, vis_params = self._visibility_sql("f.actor", actor)
            vis_clause = f"AND {vis_sql}" if vis_sql else ""
            params.extend(vis_params)
            params.append(limit)

            sql = f"""
                SELECT f.fact_id, f.content, f.category, f.tags,
                       f.trust_score, f.retrieval_count, f.helpful_count,
                       f.created_at, f.updated_at
                FROM facts f
                JOIN facts_fts fts ON fts.rowid = f.fact_id
                WHERE facts_fts MATCH ?
                  AND f.trust_score >= ?
                  {category_clause}
                  {vis_clause}
                ORDER BY fts.rank, f.trust_score DESC
                LIMIT ?
            """

            rows = self._conn.execute(sql, params).fetchall()
            results = [self._row_to_dict(r) for r in rows]

            if results:
                ids = [r["fact_id"] for r in results]
                placeholders = ",".join("?" * len(ids))
                self._conn.execute(
                    f"UPDATE facts SET retrieval_count = retrieval_count + 1 WHERE fact_id IN ({placeholders})",
                    ids,
                )
                self._conn.commit()

            return results

    def update_fact(
        self,
        fact_id: int,
        content: "str | None" = None,
        trust_delta: "float | None" = None,
        tags: "str | None" = None,
        category: "str | None" = None,
        actor: str = "",
    ) -> bool:
        """Partially update a fact visible to actor. Trust clamps to [0, 1].

        Returns True if the row existed and was updated, False otherwise.
        Foreign rows (owned by another identified actor) act as not-found.
        """
        with self._lock:
            vis_sql, vis_params = self._visibility_sql("actor", actor)
            vis_and = f"AND {vis_sql}" if vis_sql else ""
            row = self._conn.execute(
                f"""SELECT fact_id, trust_score FROM facts
                    WHERE fact_id = ? {vis_and}""",
                [fact_id, *vis_params],
            ).fetchone()
            if row is None:
                return False

            assignments: list = ["updated_at = CURRENT_TIMESTAMP"]
            params: list = []

            if content is not None:
                assignments.append("content = ?")
                params.append(content.strip())
            if tags is not None:
                assignments.append("tags = ?")
                params.append(tags)
            if category is not None:
                assignments.append("category = ?")
                params.append(category)
            if trust_delta is not None:
                new_trust = _clamp_trust(row["trust_score"] + trust_delta)
                assignments.append("trust_score = ?")
                params.append(new_trust)

            params.append(fact_id)
            self._conn.execute(
                f"UPDATE facts SET {', '.join(assignments)} WHERE fact_id = ?",
                params,
            )
            self._conn.commit()

            caller_actor = self._normalize_actor(actor)

            # If content changed, re-extract entities
            if content is not None:
                self._conn.execute(
                    "DELETE FROM fact_entities WHERE fact_id = ?", (fact_id,)
                )
                for name in self._extract_entities(content):
                    entity_id = self._resolve_entity(name, actor=caller_actor)
                    self._link_fact_entity(fact_id, entity_id)
                self._conn.commit()

            # Recompute HRR vector if content changed
            if content is not None:
                self._compute_hrr_vector(fact_id, content)
            # Rebuild bank for relevant category
            cat_row = self._conn.execute(
                f"SELECT category FROM facts WHERE fact_id = ? {vis_and}",
                [fact_id, *vis_params],
            ).fetchone()
            cat = category or cat_row["category"]
            self._rebuild_bank(cat, caller_actor)

            return True

    def remove_fact(self, fact_id: int, actor: str = "") -> bool:
        """Delete a fact visible to actor and its entity links.

        Returns True if the row existed, False otherwise (foreign rows act as
        not-found).
        """
        with self._lock:
            vis_sql, vis_params = self._visibility_sql("actor", actor)
            vis_and = f"AND {vis_sql}" if vis_sql else ""
            row = self._conn.execute(
                f"SELECT fact_id, category FROM facts WHERE fact_id = ? {vis_and}",
                [fact_id, *vis_params],
            ).fetchone()
            if row is None:
                return False

            self._conn.execute(
                "DELETE FROM fact_entities WHERE fact_id = ?", (fact_id,)
            )
            self._conn.execute("DELETE FROM facts WHERE fact_id = ?", (fact_id,))
            self._conn.commit()
            self._rebuild_bank(row["category"], actor)
            return True

    def list_facts(
        self,
        category: "str | None" = None,
        min_trust: float = 0.0,
        limit: int = 50,
        actor: str = "",
    ) -> list:
        """Browse the facts visible to actor, ordered by trust descending.

        Optionally filter by category and minimum trust score.
        """
        with self._lock:
            params: list = [min_trust]
            category_clause = ""
            if category is not None:
                category_clause = "AND category = ?"
                params.append(category)
            vis_sql, vis_params = self._visibility_sql("actor", actor)
            vis_clause = f"AND {vis_sql}" if vis_sql else ""
            params.extend(vis_params)
            params.append(limit)

            sql = f"""
                SELECT fact_id, content, category, tags, trust_score,
                       retrieval_count, helpful_count, created_at, updated_at
                FROM facts
                WHERE trust_score >= ?
                  {category_clause}
                  {vis_clause}
                ORDER BY trust_score DESC
                LIMIT ?
            """
            rows = self._conn.execute(sql, params).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def record_feedback(self, fact_id: int, helpful: bool, actor: str = "") -> dict:
        """Record user feedback and adjust trust asymmetrically.

        helpful=True  -> trust += 0.05, helpful_count += 1
        helpful=False -> trust -= 0.10

        Returns a dict with fact_id, old_trust, new_trust, helpful_count.
        Raises KeyError if the fact does not exist or belongs to another
        actor (foreign rows act as not-found).
        """
        with self._lock:
            vis_sql, vis_params = self._visibility_sql("actor", actor)
            vis_and = f"AND {vis_sql}" if vis_sql else ""
            row = self._conn.execute(
                f"""SELECT fact_id, trust_score, helpful_count FROM facts
                    WHERE fact_id = ? {vis_and}""",
                [fact_id, *vis_params],
            ).fetchone()
            if row is None:
                raise KeyError(f"fact_id {fact_id} not found")

            old_trust: float = row["trust_score"]
            delta = _HELPFUL_DELTA if helpful else _UNHELPFUL_DELTA
            new_trust = _clamp_trust(old_trust + delta)

            helpful_increment = 1 if helpful else 0
            self._conn.execute(
                """
                UPDATE facts
                SET trust_score    = ?,
                    helpful_count  = helpful_count + ?,
                    updated_at     = CURRENT_TIMESTAMP
                WHERE fact_id = ?
                """,
                (new_trust, helpful_increment, fact_id),
            )
            self._conn.commit()

            return {
                "fact_id":      fact_id,
                "old_trust":    old_trust,
                "new_trust":    new_trust,
                "helpful_count": row["helpful_count"] + helpful_increment,
            };


    # ------------------------------------------------------------------
    # Entity helpers
    # ------------------------------------------------------------------

    def _extract_entities(self, text: str) -> list:
        """Extract entity candidates from text using simple regex rules.

        Rules applied (in order):
        1. Capitalized multi-word phrases  e.g. "John Doe"
        2. Double-quoted terms             e.g. "Python"
        3. Single-quoted terms             e.g. 'pytest'
        4. AKA patterns                    e.g. "Guido aka BDFL" -> two entities

        Returns a deduplicated list preserving first-seen order.
        """
        seen: set = set()
        candidates: list = []

        def _add(name: str) -> None:
            stripped = name.strip()
            if stripped and stripped.lower() not in seen:
                seen.add(stripped.lower())
                candidates.append(stripped)

        for m in _RE_CAPITALIZED.finditer(text):
            _add(m.group(1))

        for m in _RE_DOUBLE_QUOTE.finditer(text):
            _add(m.group(1))

        for m in _RE_SINGLE_QUOTE.finditer(text):
            _add(m.group(1))

        for m in _RE_AKA.finditer(text):
            _add(m.group(1))
            _add(m.group(2))

        return candidates

    def _resolve_entity(self, name: str, actor: str = "") -> int:
        """Find an entity visible to actor by name or alias, or create one.

        Entity resolution is scoped per actor: an identified caller resolves
        against shared rows plus its own, and newly created entities carry the
        caller's actor. Returns the entity_id.
        """
        vis_sql, vis_params = self._visibility_sql("actor", actor)
        vis_and = f"AND {vis_sql}" if vis_sql else ""

        # Exact name match
        row = self._conn.execute(
            f"SELECT entity_id FROM entities WHERE name LIKE ? {vis_and}",
            [name, *vis_params],
        ).fetchone()
        if row is not None:
            return int(row["entity_id"])

        # Search aliases -- aliases stored as comma-separated; use LIKE with % boundaries
        alias_row = self._conn.execute(
            f"""
            SELECT entity_id FROM entities
            WHERE ',' || aliases || ',' LIKE '%,' || ? || ',%' {vis_and}
            """,
            [name, *vis_params],
        ).fetchone()
        if alias_row is not None:
            return int(alias_row["entity_id"])

        # Create new entity scoped to this actor
        cur = self._conn.execute(
            "INSERT INTO entities (name, actor) VALUES (?, ?)",
            (name, self._normalize_actor(actor)),
        )
        self._conn.commit()
        return int(cur.lastrowid)  # type: ignore[return-value]

    def _link_fact_entity(self, fact_id: int, entity_id: int) -> None:
        """Insert into fact_entities, silently ignore if the link already exists."""
        self._conn.execute(
            """
            INSERT OR IGNORE INTO fact_entities (fact_id, entity_id)
            VALUES (?, ?)
            """,
            (fact_id, entity_id),
        )
        self._conn.commit()

    def _compute_hrr_vector(self, fact_id: int, content: str) -> None:
        """Compute and store HRR vector for a fact. No-op if numpy unavailable."""
        with self._lock:
            if not self._hrr_available:
                return

            # Get entities linked to this fact
            rows = self._conn.execute(
                """
                SELECT e.name FROM entities e
                JOIN fact_entities fe ON fe.entity_id = e.entity_id
                WHERE fe.fact_id = ?
                """,
                (fact_id,),
            ).fetchall()
            entities = [row["name"] for row in rows]

            vector = hrr.encode_fact(content, entities, self.hrr_dim)
            self._conn.execute(
                "UPDATE facts SET hrr_vector = ? WHERE fact_id = ?",
                (hrr.phases_to_bytes(vector), fact_id),
            )
            self._conn.commit()

    def _rebuild_bank(self, category: str, actor: str = "") -> None:
        """Rebuild the (category, actor) memory bank from its visible vectors."""
        with self._lock:
            if not self._hrr_available:
                return

            actor = self._normalize_actor(actor)
            bank_name = f"cat:{category}"
            vis_sql, vis_params = self._visibility_sql("actor", actor)
            vis_and = f"AND {vis_sql}" if vis_sql else ""
            rows = self._conn.execute(
                f"""SELECT hrr_vector FROM facts
                    WHERE category = ? AND hrr_vector IS NOT NULL {vis_and}""",
                [category, *vis_params],
            ).fetchall()

            if not rows:
                # Drop only THIS (bank_name, actor) key -- other actors' banks
                # are independent caches.
                self._conn.execute(
                    "DELETE FROM memory_banks WHERE bank_name = ? AND actor = ?",
                    (bank_name, actor),
                )
                self._conn.commit()
                return

            vectors = [hrr.bytes_to_phases(row["hrr_vector"], dim=self.hrr_dim) for row in rows]
            bank_vector = hrr.bundle(*vectors)
            fact_count = len(vectors)

            # Check SNR
            hrr.snr_estimate(self.hrr_dim, fact_count)

            self._conn.execute(
                """
                INSERT INTO memory_banks (bank_name, actor, vector, dim, fact_count, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(bank_name, actor) DO UPDATE SET
                    vector = excluded.vector,
                    dim = excluded.dim,
                    fact_count = excluded.fact_count,
                    updated_at = excluded.updated_at
                """,
                (bank_name, actor, hrr.phases_to_bytes(bank_vector), self.hrr_dim, fact_count),
            )
            self._conn.commit()

    def rebuild_all_vectors(self, dim: "int | None" = None) -> int:
        """Recompute all HRR vectors + banks from text. For recovery/migration.

        Banks are rebuilt per distinct (category, actor) pair. Returns the
        number of facts processed.
        """
        with self._lock:
            if not self._hrr_available:
                return 0

            if dim is not None:
                self.hrr_dim = dim

            rows = self._conn.execute(
                "SELECT fact_id, content, category, actor FROM facts"
            ).fetchall()

            bank_keys: set = set()
            for row in rows:
                self._compute_hrr_vector(row["fact_id"], row["content"])
                bank_keys.add((row["category"], self._normalize_actor(row["actor"])))

            for category, actor in sorted(bank_keys):
                self._rebuild_bank(category, actor)

            return len(rows)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        """Convert a sqlite3.Row to a plain dict."""
        return dict(row)


    @classmethod
    def release_all_under(cls, directory: "str | Path") -> int:
        """Force-close every shared connection whose database lives under directory.

        close() is refcount-driven, so a live holder (e.g. an agent's
        memory provider) keeps a profile's SQLite handle open indefinitely.
        That is exactly what a profile delete must break on Windows: the
        desktop's main serve process opens memory_store.db for every
        known profile, and rmtree of the profile directory fails with
        WinError 32 while any of those handles is open (#88347). This
        closes the matching connections unconditionally -- the directory is
        going away, so later use by a stale holder is expected to fail -- and
        returns how many were closed. In a process that holds none (e.g. the
        CLI deleting from outside serve) this is a harmless no-op returning 0.
        """
        root = os.path.normcase(str(Path(directory).expanduser().resolve())) + os.sep
        with cls._shared_guard:
            # Snapshot the keys first so the registry stays stable while
            # connections are closed inside their per-database locks (closing
            # can run no user code, but this keeps the invariant obvious).
            doomed = [
                key
                for key in cls._shared
                if os.path.normcase(key).startswith(root)
            ]
            for key in doomed:
                entry = cls._shared.pop(key)
                try:
                    with entry["lock"]:
                        entry["conn"].close()
                except Exception:
                    # A connection that is already closed or broken must not
                    # abort releasing its siblings.
                    pass
        return len(doomed)

    def close(self) -> None:
        """Release this instance's reference to the shared connection.

        The underlying connection is closed only when the last MemoryStore
        referencing the same database is closed, so closing one instance can
        never break sibling instances that still hold it. Idempotent.
        """
        if getattr(self, "_entry", None) is None:
            return
        with MemoryStore._shared_guard:
            entry = self._entry
            if entry is None:
                return
            entry["refs"] -= 1
            if entry["refs"] <= 0:
                try:
                    entry["conn"].close()
                finally:
                    # Pop only OUR entry. After release_all_under() force-
                    # closed this entry (profile delete, #88347) a same-path
                    # store may have re-registered a FRESH entry under the
                    # same key; a stale holder's late close() must not evict
                    # it -- that would silently reintroduce the multi-writer
                    # contention this registry exists to prevent.
                    if MemoryStore._shared.get(self._key) is entry:
                        MemoryStore._shared.pop(self._key, None)
            self._entry = None

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
