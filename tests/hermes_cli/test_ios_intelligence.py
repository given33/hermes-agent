from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import base64
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from hermes_cli.ios_intelligence import (
    AMapClient,
    IOSIntelligenceStore,
    KNOWN_FEATURE_CAPABILITIES,
    QWeatherClient,
    WEATHER_MONTHLY_LIMIT,
    WEATHER_SOFT_LIMIT,
    ios_native_action_metadata,
    load_ios_feature_weights,
)


@pytest.fixture
def store(tmp_path):
    return IOSIntelligenceStore(tmp_path)


def test_intelligence_database_uses_fallback_when_home_is_full(monkeypatch, tmp_path: Path):
    home = tmp_path / "full-home"
    home.mkdir()
    fallback = tmp_path / "fallback"
    primary = home / "ios-intelligence.db"
    with sqlite3.connect(primary) as conn:
        conn.execute("CREATE TABLE preserved (value TEXT NOT NULL)")
        conn.execute("INSERT INTO preserved VALUES ('kept')")
    primary.with_name(primary.name + ".key").write_bytes(b"test-master-secret")
    cold_file = home / "ios-cold" / "owner" / "segment.bin"
    cold_file.parent.mkdir(parents=True)
    cold_file.write_bytes(b"cold-data")

    monkeypatch.setattr("hermes_cli.ios_intelligence.get_hermes_home", lambda: home)
    monkeypatch.setenv("HERMES_IOS_INTELLIGENCE_FALLBACK_DIR", str(fallback))
    monkeypatch.setattr(IOSIntelligenceStore, "_cleanup_orphan_cold_files", lambda self: {})
    import hermes_cli.sqlite_util as sqlite_util

    real_disk_usage = sqlite_util.shutil.disk_usage

    def disk_usage(path):
        if Path(path) == home:
            return SimpleNamespace(free=0)
        return real_disk_usage(path)

    monkeypatch.setattr(sqlite_util.shutil, "disk_usage", disk_usage)
    store = IOSIntelligenceStore()

    assert store.path == fallback / primary.name
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT value FROM preserved").fetchone() == ("kept",)
    assert store.path.with_name(store.path.name + ".key").read_bytes() == b"test-master-secret"
    assert (store.path.parent / "ios-cold" / "owner" / "segment.bin").read_bytes() == b"cold-data"


def test_store_rejects_an_empty_sidecar_master_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_IOS_DATA_KEY", raising=False)
    monkeypatch.delenv("HERMES_DATA_ENCRYPTION_KEY", raising=False)
    path = tmp_path / "empty-master-secret.db"
    path.with_name(path.name + ".key").write_bytes(b"")

    with pytest.raises(RuntimeError, match="Master secret file is empty"):
        IOSIntelligenceStore(path)


def test_sidecar_master_secret_syncs_directory_and_cleanup_is_best_effort(
    tmp_path, monkeypatch
):
    path = tmp_path / "durable-master-secret.db"
    synced = []
    real_unlink = Path.unlink

    monkeypatch.setattr(
        IOSIntelligenceStore,
        "_fsync_directory",
        staticmethod(lambda directory: synced.append(Path(directory))),
    )

    def flaky_unlink(candidate, *args, **kwargs):
        if candidate.name.startswith(f".{path.name}.key."):
            raise PermissionError("busy test candidate")
        return real_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    instance = IOSIntelligenceStore(path)

    assert instance._master_secret == path.with_name(path.name + ".key").read_bytes()
    assert synced == [path.parent]


def _location_event(event_id: str, *, latitude: float = 24.9, owner_time: int | None = None):
    return {
        "event_id": event_id,
        "kind": "location",
        "observed_at": owner_time,
        "payload": {
            "latitude": latitude,
            "longitude": 118.6,
            "horizontal_accuracy": 4.0,
            "speed": 1.2,
            "motion": "walking",
        },
    }


def test_ingest_is_idempotent_and_account_scoped(store):
    event = _location_event("point-1")
    first = store.ingest_events("alice", "iphone", [event], "cursor-1")
    second = store.ingest_events("alice", "iphone", [event], "cursor-1")
    store.ingest_events("alice", "iphone", [_location_event("point-2")], "cursor-2")
    replay = store.ingest_events("alice", "iphone", [event], "cursor-1")
    store.ingest_events("bob", "iphone", [_location_event("point-1", latitude=25.1)], "b1")

    assert first == {"accepted": 1, "duplicates": 0, "next_cursor": "cursor-1"}
    assert second["accepted"] == 0
    assert second["duplicates"] == 1
    assert replay["next_cursor"] == "cursor-2"
    assert store.latest_snapshot("alice", "location")["data"]["latitude"] == 24.9
    assert store.latest_snapshot("bob", "location")["data"]["latitude"] == 25.1
    assert store.get_upload_cursor("alice", "iphone") == "cursor-2"


def test_ingest_rolls_back_event_projection_cursor_and_job_together(store, monkeypatch):
    def fail_enqueue(*_args, **_kwargs):
        raise RuntimeError("derived queue unavailable")

    monkeypatch.setattr(store, "_enqueue_event_derived_jobs", fail_enqueue)

    with pytest.raises(RuntimeError, match="derived queue unavailable"):
        store.ingest_events(
            "alice",
            "iphone",
            [_location_event("atomic-point", owner_time=1_700_000_000)],
            "cursor-atomic",
        )

    assert store.get_upload_cursor("alice", "iphone") is None
    assert store.latest_snapshot("alice", "location") is None
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM ios_events WHERE event_id='atomic-point'"
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ios_derived_jobs").fetchone()[0] == 0


def test_derived_jobs_recover_after_cursor_commit_without_duplicate_training(
    store,
    monkeypatch,
):
    original_process = store._process_derived_job

    def fail_processing(_job):
        raise RuntimeError("fixture training failure")

    monkeypatch.setattr(store, "_process_derived_job", fail_processing)
    result = store.ingest_events(
        "alice",
        "iphone",
        [{
            "event_id": "place-recovery",
            "kind": "place",
            "observed_at": 1_700_000_000,
            "payload": {
                "place_id": "home",
                "name": "Home",
                "latitude": 24.9,
                "longitude": 118.6,
                "arrived_at": 1_700_000_000,
            },
        }],
        "cursor-recovery",
    )

    assert result["next_cursor"] == "cursor-recovery"
    assert store.get_place("alice", "home") is None
    pending = store.list_derived_jobs("alice", state="pending")
    assert {job["job_kind"] for job in pending} == {
        "behavior-feedback",
        "learn-place",
    }


def _clear_account_export(store, exported, owner_id="alice", passphrase=None):
    encrypted = base64.b64decode(exported["blob_base64"])
    clear = store.decrypt_account_export(
        encrypted,
        owner_id,
        passphrase or exported["recovery_key"],
    )
    return json.loads(clear)

    monkeypatch.setattr(store, "_process_derived_job", original_process)
    retry_at = max(int(job["not_before"]) for job in pending) + 1
    recovered = store.run_derived_jobs(owner_id="alice", now=retry_at)

    assert recovered["completed"] == 2
    assert store.get_place("alice", "home")["visits"] == 1
    assert all(
        job["state"] == "completed"
        for job in store.list_derived_jobs("alice")
    )


def test_derived_job_lease_replay_is_idempotent_after_process_exit(store, monkeypatch):
    original_process = store._process_derived_job
    monkeypatch.setattr(
        store,
        "_process_derived_job",
        lambda _job: (_ for _ in ()).throw(RuntimeError("defer fixture")),
    )
    store.ingest_events(
        "alice",
        "iphone",
        [{
            "event_id": "place-crash-window",
            "kind": "place",
            "observed_at": 1_700_000_100,
            "payload": {
                "place_id": "office",
                "name": "Office",
                "latitude": 24.91,
                "longitude": 118.61,
                "arrived_at": 1_700_000_100,
            },
        }],
        "cursor-crash-window",
    )
    monkeypatch.setattr(store, "_process_derived_job", original_process)
    pending = store.list_derived_jobs("alice", state="pending")
    claim_at = max(int(job["not_before"]) for job in pending) + 1
    claimed = store._claim_derived_jobs(
        limit=10,
        owner_id="alice",
        now=claim_at,
        lease_seconds=5,
    )
    place_job = next(job for job in claimed if job["job_kind"] == "learn-place")

    # Simulate process exit after the side effect but before lease completion.
    store._process_derived_job(place_job)
    assert store.get_place("alice", "office")["visits"] == 1

    replayed = store.run_derived_jobs(owner_id="alice", now=claim_at + 6)

    assert replayed["completed"] == 2
    assert store.get_place("alice", "office")["visits"] == 1


def test_derived_backfill_uses_low_priority_watermark_without_cursor_rollback(store):
    store.ingest_events(
        "alice",
        "iphone",
        [{
            "event_id": "legacy-place",
            "kind": "place",
            "observed_at": 1_600_000_000,
            "payload": {
                "place_id": "legacy-home",
                "latitude": 24.9,
                "longitude": 118.6,
                "arrived_at": 1_600_000_000,
            },
        }],
        "cursor-current",
    )
    with sqlite3.connect(store.path) as conn:
        for table in (
            "ios_derived_jobs",
            "ios_derived_backfill_watermarks",
            "ios_place_samples",
            "ios_place_graph",
            "ios_behavior_feedback",
        ):
            conn.execute(f"DELETE FROM {table}")

    assert store.backfill_derived_jobs(limit=10) == 2
    queued = store.list_derived_jobs("alice", state="pending")
    assert len(queued) == 2
    assert {job["priority"] for job in queued} == {10}
    assert store.get_upload_cursor("alice", "iphone") == "cursor-current"

    assert store.run_derived_jobs(owner_id="alice")["completed"] == 2
    assert store.get_place("alice", "legacy-home")["visits"] == 1
    assert store.backfill_derived_jobs(limit=10) == 0
    assert store.get_upload_cursor("alice", "iphone") == "cursor-current"


def test_generation_tombstone_cancels_inflight_derived_job(store, monkeypatch):
    original_process = store._process_derived_job
    monkeypatch.setattr(
        store,
        "_process_derived_job",
        lambda _job: (_ for _ in ()).throw(RuntimeError("defer fixture")),
    )
    store.ingest_events(
        "alice",
        "iphone",
        [{
            "event_id": "old-generation-motion",
            "kind": "motion",
            "observed_at": 1_700_000_000,
            "payload": {"state": "walking"},
        }],
        "old-generation-cursor",
    )
    monkeypatch.setattr(store, "_process_derived_job", original_process)
    pending = store.list_derived_jobs("alice", state="pending")
    claim_at = int(pending[0]["not_before"]) + 1
    job = store._claim_derived_jobs(
        limit=1,
        owner_id="alice",
        now=claim_at,
    )[0]

    store.begin_account_deletion("alice", account_generation="legacy")
    with pytest.raises(sqlite3.IntegrityError, match="deletion tombstone") as error:
        store._process_derived_job(job)
    assert store._finish_derived_job(job, error=error.value, now=claim_at + 1)
    assert store.list_derived_jobs("alice")[0]["state"] == "cancelled"
    with pytest.raises(PermissionError, match="deletion tombstone"):
        store.ingest_events(
            "alice",
            "iphone",
            [_location_event("new-after-delete")],
            "forbidden-cursor",
        )


def test_route_and_model_derived_jobs_are_applied(store):
    result = store.ingest_events(
        "alice",
        "iphone",
        [
            {
                "event_id": "route-derived",
                "kind": "route",
                "observed_at": 1_700_000_000,
                "payload": {
                    "origin_place_id": "home",
                    "destination_place_id": "office",
                    "mode": "walking",
                    "duration_seconds": 900,
                    "distance_meters": 1200,
                },
            },
            {
                "event_id": "model-derived",
                "kind": "behavior-model",
                "observed_at": 1_700_000_001,
                "payload": {
                    "model_name": "departure",
                    "payload": {"bias": 0.25},
                },
            },
        ],
        "cursor-route-model",
    )

    assert result["accepted"] == 2
    assert store.get_route("alice", "home", "office", "walking")["trips"] == 1
    assert store.load_model("alice", "departure")["payload"] == {"bias": 0.25}


def test_same_client_event_id_is_isolated_by_account_and_device(store):
    event = {
        "event_id": "shared-client-id",
        "kind": "motion",
        "observed_at": 1_700_000_000,
        "payload": {"state": "walking"},
    }

    assert store.ingest_events("alice", "iphone", [event], "alice-1")["accepted"] == 1
    assert store.ingest_events("bob", "iphone", [event], "bob-1")["accepted"] == 1
    with sqlite3.connect(store.path) as conn:
        rows = conn.execute(
            "SELECT owner_id,COUNT(*) FROM ios_behavior_feedback "
            "WHERE label='actual-motion' GROUP BY owner_id ORDER BY owner_id"
        ).fetchall()
    assert rows == [("alice", 1), ("bob", 1)]


def test_apns_tokens_are_consumed_without_entering_history_or_exports(store):
    event = {
        "event_id": "legacy-apns-token",
        "kind": "apns_token",
        "observed_at": 1_700_000_000,
        "payload": {"token": "a1" * 32, "environment": "production"},
    }

    result = store.ingest_events("alice", "iphone", [event], "cursor-sensitive")

    assert result == {"accepted": 0, "duplicates": 1, "next_cursor": "cursor-sensitive"}
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM ios_events WHERE owner_id='alice' AND kind='apns-token'"
        ).fetchone()[0] == 0
        conn.execute(
            "INSERT INTO ios_events VALUES(?,?,?,?,?,?,?)",
            (
                "alice", "iphone", "direct-legacy-token", "apns-token",
                1_700_000_001, '{"token":"legacy-secret"}', 1_700_000_001,
            ),
        )
        conn.execute(
            "INSERT INTO ios_snapshots VALUES(?,?,?,?,?,?)",
            (
                "alice", "apns-token", "iphone", 1_700_000_001,
                '{"token":"legacy-secret"}', 1_700_000_001,
            ),
        )

    exported = _clear_account_export(store, store.export_account("alice"))
    assert exported["tables"]["ios_events"] == []
    assert exported["tables"]["ios_snapshots"] == []

    with sqlite3.connect(store.path) as conn:
        conn.execute("PRAGMA user_version=3")
    IOSIntelligenceStore(store.path)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM ios_events WHERE kind='apns-token'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM ios_snapshots WHERE kind='apns-token'"
        ).fetchone()[0] == 0


def test_hot_behavior_rows_use_account_envelopes_and_wrong_key_cannot_open(store, monkeypatch):
    marker = "PRIVATE_BEHAVIOR_MARKER"
    store.ingest_events(
        "alice",
        "iphone",
        [{
            "event_id": "encrypted-location",
            "kind": "location",
            "observed_at": 1_700_000_000,
            "payload": {
                "latitude": 24.900001,
                "longitude": 118.600001,
                "secret": marker,
            },
        }],
        "1",
    )

    raw_database = store.path.read_bytes()
    assert marker.encode() not in raw_database
    assert b"24.900001" not in raw_database
    assert store.latest_snapshot("alice", "location")["data"]["secret"] == marker

    monkeypatch.setenv("HERMES_IOS_DATA_KEY", "wrong-account-key")
    wrong_key_store = IOSIntelligenceStore(store.path)
    with pytest.raises(RuntimeError, match="authentication failed"):
        wrong_key_store.latest_snapshot("alice", "location")

    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE ios_snapshots SET payload_json='{}' WHERE owner_id='alice'")
    with pytest.raises(RuntimeError, match="envelope is missing"):
        store.latest_snapshot("alice", "location")


def test_account_export_rejects_nonempty_plaintext_in_default_columns(store):
    store.learn_place(
        "alice",
        "frequent",
        name="Encrypted place",
        latitude=24.9,
        longitude=118.6,
    )
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE ios_place_graph SET name='PLAINTEXT_EXPORT_BYPASS' "
            "WHERE owner_id='alice'"
        )

    with pytest.raises(RuntimeError, match="envelope is missing"):
        store.export_account("alice")


def test_account_export_passphrase_envelope_is_recoverable_without_plaintext(store):
    store.ingest_events(
        "alice",
        "iphone",
        [_location_event("export-secret", latitude=24.912345)],
        "export-cursor",
    )

    exported = store.export_account(
        "alice",
        encrypt=True,
        export_passphrase="correct horse battery staple",
    )
    encrypted = base64.b64decode(exported["blob_base64"])

    assert exported["encrypted"] is True
    assert exported["kdf"] == "scrypt-N16384-r8-p1"
    assert b"24.912345" not in encrypted
    clear = store.decrypt_account_export(
        encrypted,
        "alice",
        "correct horse battery staple",
    )
    assert b"export-secret" in clear
    with pytest.raises(Exception):
        store.decrypt_account_export(encrypted, "alice", "wrong password")


def test_account_export_cannot_be_downgraded_and_recovery_key_stays_outside_blob(store):
    store.ingest_events(
        "alice",
        "iphone",
        [_location_event("forced-encryption", latitude=24.976543)],
        "forced-encryption",
    )

    exported = store.export_account("alice", encrypt=False)
    encrypted = base64.b64decode(exported["blob_base64"])

    assert exported["encrypted"] is True
    assert exported["recovery_key"]
    assert exported["recovery_key"].encode() not in encrypted
    assert b"24.976543" not in encrypted
    clear = _clear_account_export(store, exported)
    assert clear["tables"]["ios_events"][0]["event_id"] == "forced-encryption"


def test_account_export_redacts_nested_credentials_and_records_scanner_audit(store):
    credential = "api-key-plaintext-export-marker"
    store.ingest_events(
        "alice",
        "iphone",
        [{
            "event_id": "credential-redaction",
            "kind": "location",
            "observed_at": 1_700_000_000,
            "payload": {
                "latitude": 24.9,
                "longitude": 118.6,
                "nested": {"api_key": credential},
            },
        }],
        "credential-redaction",
    )

    exported = store.export_account(
        "alice",
        export_passphrase="correct horse battery staple",
    )
    clear = _clear_account_export(
        store,
        exported,
        passphrase="correct horse battery staple",
    )

    assert credential not in json.dumps(clear)
    assert "api_key" not in clear["tables"]["ios_events"][0]["payload_json"]
    with sqlite3.connect(store.path) as conn:
        audit = conn.execute(
            "SELECT encrypted,kdf,scanner_redactions,scanner_findings,status "
            "FROM ios_export_audit ORDER BY created_at DESC,id DESC LIMIT 1"
        ).fetchone()
    assert audit[:2] == (1, "scrypt-N16384-r8-p1")
    assert audit[2] >= 1
    assert audit[3:] == (0, "completed")


def test_connect_closes_and_reraises_setup_errors(monkeypatch, tmp_path):
    import hermes_cli.ios_intelligence as intelligence_module

    class BrokenConnection:
        row_factory = None
        closed = False

        def execute(self, _statement):
            raise sqlite3.OperationalError("fixture setup failure")

        def close(self):
            self.closed = True

    connection = BrokenConnection()
    monkeypatch.setattr(
        intelligence_module.sqlite3,
        "connect",
        lambda *_args, **_kwargs: connection,
    )
    instance = object.__new__(IOSIntelligenceStore)
    instance.path = tmp_path / "broken.db"

    with pytest.raises(sqlite3.OperationalError, match="fixture setup failure"):
        instance._connect()

    assert connection.closed is True


def test_secure_compact_retries_then_reraises_the_lock_error(monkeypatch):
    import hermes_cli.ios_intelligence as intelligence_module

    class LockedConnection:
        attempts = 0

        def execute(self, _statement):
            self.attempts += 1
            raise sqlite3.OperationalError("database is locked")

    times = iter((0.0, 0.0, 31.0))
    monkeypatch.setattr(intelligence_module.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(intelligence_module.time, "sleep", lambda _seconds: None)
    connection = LockedConnection()
    instance = object.__new__(IOSIntelligenceStore)

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        instance._secure_compact(connection)

    assert connection.attempts == 2


def test_schema_migration_seals_legacy_hot_rows(store):
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "INSERT INTO ios_events VALUES(?,?,?,?,?,?,?)",
            (
                "legacy-owner", "iphone", "legacy-location", "location", 1_700_000_001,
                '{"latitude":24.900002,"longitude":118.600002,"secret":"legacy-marker"}',
                1_700_000_001,
            ),
        )
        conn.execute(
            "INSERT INTO ios_trajectory VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy-owner", "iphone", "legacy-location", 1_700_000_001,
                24.900002, 118.600002, 4.0, None, 1.0, 90.0, "walking",
                '{"latitude":24.900002,"longitude":118.600002}',
            ),
        )
        conn.execute("PRAGMA user_version=4")

    IOSIntelligenceStore(store.path)
    raw_database = store.path.read_bytes()
    assert b"legacy-marker" not in raw_database
    assert b"24.900002" not in raw_database
    wal_path = store.path.with_name(store.path.name + "-wal")
    if wal_path.exists():
        assert b"legacy-marker" not in wal_path.read_bytes()
        assert b"24.900002" not in wal_path.read_bytes()

    with sqlite3.connect(store.path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == IOSIntelligenceStore.schema_version


def test_repeated_place_learning_without_a_name_preserves_the_existing_name(store):
    store.learn_place("alice", "frequent", name="Known place", latitude=24.9, longitude=118.6)
    updated = store.learn_place("alice", "frequent", latitude=24.9, longitude=118.6)

    assert updated["name"] == "Known place"
    assert updated["visits"] == 2


def test_existing_command_table_migrates_delivery_attempts(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE ios_device_commands ("
            "id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,device_id TEXT NOT NULL DEFAULT '',"
            "capability TEXT NOT NULL,action TEXT NOT NULL,payload_json TEXT NOT NULL,"
            "idempotency_key TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'pending',"
            "not_before INTEGER NOT NULL,expires_at INTEGER,created_at INTEGER NOT NULL,"
            "delivered_at INTEGER,acknowledged_at INTEGER,result_json TEXT NOT NULL DEFAULT '{}')"
        )

    IOSIntelligenceStore(path)

    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(ios_device_commands)")}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert "attempts" in columns
    assert version == IOSIntelligenceStore.schema_version


def test_concurrent_store_initialization_serializes_schema_migrations(tmp_path):
    path = tmp_path / "concurrent.db"
    with ThreadPoolExecutor(max_workers=8) as pool:
        stores = list(pool.map(lambda _index: IOSIntelligenceStore(path), range(16)))

    assert all(item.path == path for item in stores)
    secrets = {item._master_secret for item in stores}
    assert len(secrets) == 1
    assert path.with_name(path.name + ".key").read_bytes() in secrets
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == IOSIntelligenceStore.schema_version


def test_numeric_upload_cursor_never_regresses_on_delayed_new_batch(store):
    store.ingest_events("alice", "iphone", [_location_event("point-20")], "20")
    # The event is new, but the delayed device batch carries an older cursor.
    store.ingest_events("alice", "iphone", [_location_event("point-10")], "10")

    assert store.get_upload_cursor("alice", "iphone") == "20"


def test_trajectory_has_no_ttl_or_pruning(store):
    old = 946_684_800  # 2000-01-01
    store.ingest_events("alice", "iphone", [_location_event("old", owner_time=old)], "1")
    with sqlite3.connect(store.path) as conn:
        row = conn.execute("SELECT observed_at FROM ios_trajectory WHERE event_id='old'").fetchone()
    assert row == (old,)
    assert not hasattr(store, "prune_trajectory")


def test_today_snapshot_returns_only_local_calendar_day(store):
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime.now(tz)
    timestamp = int(now.timestamp())
    store.ingest_events(
        "alice",
        "iphone",
        [
            _location_event("now", owner_time=timestamp),
            {
                "event_id": "visit",
                "kind": "place-visit",
                "observed_at": timestamp,
                "payload": {
                    "place_id": "study-room",
                    "name": "泉州九一百脑汇",
                    "latitude": 24.91,
                    "longitude": 118.59,
                    "arrived_at": timestamp - 900,
                    "departed_at": timestamp,
                    "indoor": True,
                },
            },
        ],
        "2",
    )
    today = store.today_snapshot("alice", "Asia/Shanghai")
    assert today["date"] == now.date().isoformat()
    assert len(today["trajectory"]) == 1
    assert today["places"][0]["name"] == "泉州九一百脑汇"
    assert today["places"][0]["indoor"] is True


def test_today_snapshot_includes_visit_overlapping_local_midnight(store, monkeypatch):
    tz = ZoneInfo("Asia/Shanghai")
    fixed_now = datetime(2026, 7, 18, 0, 6, tzinfo=tz)
    arrived_at = int(datetime(2026, 7, 17, 23, 51, tzinfo=tz).timestamp())
    departed_at = int(fixed_now.timestamp())

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, current_tz=None):
            return fixed_now if current_tz is None else fixed_now.astimezone(current_tz)

    store.ingest_events(
        "alice",
        "iphone",
        [{
            "event_id": "overnight-visit",
            "kind": "place-visit",
            "observed_at": arrived_at,
            "payload": {
                "place_id": "home",
                "name": "Home",
                "latitude": 24.9,
                "longitude": 118.6,
                "arrived_at": arrived_at,
                "departed_at": departed_at,
                "indoor": True,
            },
        }],
        "1",
    )
    monkeypatch.setattr("hermes_cli.ios_intelligence.datetime", FixedDateTime)

    today = store.today_snapshot("alice", "Asia/Shanghai")

    assert today["date"] == "2026-07-18"
    assert [(place["place_id"], place["arrived_at"], place["departed_at"]) for place in today["places"]] == [
        ("home", arrived_at, departed_at),
    ]


def test_today_snapshot_excludes_visit_that_ended_at_local_midnight(store, monkeypatch):
    tz = ZoneInfo("Asia/Shanghai")
    fixed_now = datetime(2026, 7, 18, 0, 6, tzinfo=tz)
    arrived_at = int(datetime(2026, 7, 17, 23, 51, tzinfo=tz).timestamp())
    midnight = int(datetime(2026, 7, 18, 0, 0, tzinfo=tz).timestamp())

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, current_tz=None):
            return fixed_now if current_tz is None else fixed_now.astimezone(current_tz)

    store.ingest_events(
        "alice",
        "iphone",
        [{
            "event_id": "previous-day-visit",
            "kind": "place-visit",
            "observed_at": arrived_at,
            "payload": {
                "place_id": "previous-place",
                "name": "Previous Place",
                "latitude": 24.9,
                "longitude": 118.6,
                "arrived_at": arrived_at,
                "departed_at": midnight,
                "indoor": True,
            },
        }],
        "1",
    )
    monkeypatch.setattr("hermes_cli.ios_intelligence.datetime", FixedDateTime)

    assert store.today_snapshot("alice", "Asia/Shanghai")["places"] == []


def test_coordinate_only_visits_cluster_into_a_stable_learned_place(store):
    now = int(datetime.now().timestamp())
    visits = [
        {
            "event_id": f"visit-{index}",
            "kind": "place-visit",
            "observed_at": now + index,
            "payload": {
                "latitude": 24.90000 + index * 0.00002,
                "longitude": 118.60000 + index * 0.00002,
                "accuracy": 50,
                "arrived_at": now + index,
                "departed_at": now + index + 60,
                "indoor": True,
            },
        }
        for index in range(2)
    ]
    store.ingest_events("alice", "iphone", visits, "2")

    places = store.list_places("alice")
    assert len(places) == 1
    assert places[0]["visits"] == 2
    assert places[0]["weight"] > 1


def test_coordinate_only_visits_beyond_merge_radius_keep_distinct_place_ids(store):
    now = int(datetime.now().timestamp())
    visits = [
        {
            "event_id": f"visit-{index}",
            "kind": "place-visit",
            "observed_at": now + index,
            "payload": {
                "latitude": latitude,
                "longitude": 118.6,
                "accuracy": 1,
                "arrived_at": now + index,
                "departed_at": now + index + 60,
            },
        }
        for index, latitude in enumerate((24.90001, 24.90049))
    ]

    store.ingest_events("alice", "iphone", visits, "distinct")

    places = store.list_places("alice")
    assert len(places) == 2
    assert len({place["place_id"] for place in places}) == 2


def test_departure_callback_closes_the_existing_visit_without_double_learning(store):
    arrived_at = 1_784_313_600
    departed_at = arrived_at + 3_600
    arrival = {
        "event_id": "visit-arrival",
        "kind": "place-visit",
        "observed_at": arrived_at,
        "payload": {
            "place_id": "home",
            "name": "Home",
            "latitude": 24.9,
            "longitude": 118.6,
            "arrived_at": arrived_at,
            "departed_at": None,
            "indoor": True,
        },
    }
    departure = {
        "event_id": "visit-departure",
        "kind": "place-visit",
        "observed_at": departed_at,
        "payload": {
            **arrival["payload"],
            "departed_at": departed_at,
        },
    }

    store.ingest_events("alice", "iphone", [arrival], "arrival")
    store.ingest_events("alice", "iphone", [departure], "departure")

    visits = store.list_visit_history("alice")
    assert len(visits) == 1
    assert visits[0]["data"]["departed_at"] == departed_at
    assert store.get_place("alice", "home")["visits"] == 1
    assert store.evaluate_behavior("alice", departed_at + 60)["current_place"] is None
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM ios_events WHERE owner_id='alice' AND kind='place-visit'"
        ).fetchone()[0] == 2


def test_account_delete_removes_registered_custom_cold_segment(store):
    observed_at = 946_684_800
    store.ingest_events(
        "alice",
        "iphone",
        [_location_event("cold-point", owner_time=observed_at)],
        "cold",
    )
    target = store._cold_owner_directory("alice", create=True) / "alice-segment.enc"
    archived = store.archive_cold_storage(
        "alice",
        before=observed_at + 1,
        destination=target,
        encrypt=True,
    )
    assert archived["archived"] is True
    assert archived["encrypted"] is True
    assert archived["hot_points_removed"] == 1
    assert target.is_file()
    assert target.read_bytes().startswith(b"HERMES-AESGCM-1")
    restored = store.read_cold_trajectory("alice", archived["segment_id"])
    assert restored[0]["event_id"] == "cold-point"
    exported = _clear_account_export(store, store.export_account("alice"))
    assert exported["cold_trajectory"][0]["event_id"] == "cold-point"
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM ios_trajectory WHERE owner_id='alice'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM ios_events WHERE owner_id='alice' AND event_id='cold-point'"
        ).fetchone()[0] == 0

    deleted = store.delete_account("alice")

    assert deleted["cold_files_removed"] == 1
    assert not target.exists()
    assert store.list_cold_segments("alice") == []


def test_cold_archive_restart_removes_only_reserved_partial_files(
    store,
    tmp_path,
    monkeypatch,
):
    observed_at = 946_684_800
    store.ingest_events(
        "alice",
        "iphone",
        [_location_event("cold-crash", owner_time=observed_at)],
        "cold-crash",
    )
    real_replace = os.replace

    def replace_then_exit(source, destination):
        real_replace(source, destination)
        raise SystemExit("fixture process exit")

    monkeypatch.setattr(os, "replace", replace_then_exit)
    with pytest.raises(SystemExit, match="fixture process exit"):
        store.archive_cold_storage("alice", before=observed_at + 1)
    monkeypatch.setattr(os, "replace", real_replace)

    owner_dir = (
        tmp_path
        / "ios-cold"
        / hashlib.sha256(b"alice").hexdigest()[:24]
    )
    # The failed install's durable intent owns its target, so recovery removes it.
    assert list(owner_dir.glob("*")) == []
    assert store.list_cold_segments("alice") == []

    orphan = owner_dir / "unindexed.jsonl.gz.enc"
    orphan.write_bytes(b"orphaned-ciphertext")
    partial = owner_dir / ".ios-cold-partial"
    partial.write_bytes(b"partial")

    reopened = IOSIntelligenceStore(store.path)

    # An unindexed ciphertext is not provably an orphan: a restored/empty DB
    # would otherwise destroy the only recoverable copy.
    assert orphan.exists()
    assert not partial.exists()
    assert [path.name for path in owner_dir.glob("*")] == ["unindexed.jsonl.gz.enc"]
    deleted = reopened.delete_account("alice")
    assert deleted["state"] == "complete"


def test_account_delete_retains_failed_cold_segment_and_retries_after_restart(
    store,
    monkeypatch,
):
    observed_at = 946_684_800
    store.ingest_events(
        "alice",
        "iphone",
        [_location_event("cold-retry", owner_time=observed_at)],
        "cold-retry",
    )
    target = store._cold_owner_directory("alice", create=True) / "retry-segment.enc"
    archived = store.archive_cold_storage(
        "alice",
        before=observed_at + 1,
        destination=target,
        encrypt=True,
    )
    original_unlink = Path.unlink

    def fail_target(path, *args, **kwargs):
        if path == target:
            raise PermissionError("fixture-denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_target)
    deleted = store.delete_account("alice")

    assert deleted["state"] == "pending"
    assert deleted["cold_segments_pending"] == 1
    assert target.is_file()
    assert store.list_cold_segments("alice")[0]["segment_id"] == archived["segment_id"]
    status = store.account_deletion_status("alice")
    assert status is not None
    assert status["status"] == "pending"
    assert status["attempts"] == 1
    assert status["last_error"] == "PermissionError"

    monkeypatch.setattr(Path, "unlink", original_unlink)
    reopened = IOSIntelligenceStore(store.path)
    retried = reopened.retry_account_deletions(owner_id="alice")

    assert retried == [{
        "cold_files_removed": 1,
        "cold_segments_pending": 0,
        "cold_segments_failed": 0,
        "cold_install_intents_pending": 0,
        "state": "complete",
    }]
    assert not target.exists()
    assert reopened.list_cold_segments("alice") == []
    status = reopened.account_deletion_status("alice")
    assert status is not None
    assert status["status"] == "complete"
    assert status["attempts"] == 2
    assert status["completed_at"] is not None


def test_account_deletion_tombstone_blocks_late_uploads_and_derived_writes(store):
    store.ingest_events(
        "alice",
        "iphone",
        [_location_event("before-delete", owner_time=946_684_800)],
        "before-delete",
    )
    deleted = store.delete_account("alice")
    assert deleted["state"] == "complete"

    with pytest.raises(PermissionError, match="deletion tombstone"):
        store.ingest_events(
            "alice",
            "iphone",
            [_location_event("late-upload", owner_time=946_684_801)],
            "late-upload",
        )
    with pytest.raises(sqlite3.IntegrityError, match="deletion tombstone"):
        store.learn_place(
            "alice",
            "late-place",
            name="Late place",
            latitude=24.9,
            longitude=118.6,
        )

    archived = store.archive_cold_storage(
        "alice",
        before=946_684_900,
        encrypt=True,
    )
    assert archived == {
        "owner_id": "alice",
        "archived": False,
        "point_count": 0,
        "account_deleted": True,
    }


def test_old_cleanup_and_late_writes_cannot_cross_reused_owner_generation(
    tmp_path,
    monkeypatch,
):
    from hermes_cli.dashboard_auth import mobile_device_store

    monkeypatch.setattr(
        mobile_device_store,
        "mobile_auth_db_path",
        lambda: tmp_path / "mobile-auth.db",
    )
    mobile = mobile_device_store.MobileDeviceStore()
    old_generation = mobile.account_generation("alice", create=True)
    intelligence = IOSIntelligenceStore(tmp_path / "intelligence")
    first = intelligence.ingest_events(
        "alice",
        "iphone-old",
        [_location_event("same-event", owner_time=946_684_800)],
        "1",
    )
    assert first["accepted"] == 1
    with intelligence._connect() as conn:
        old_storage_owner = str(
            conn.execute(
                "SELECT owner_id FROM ios_events WHERE event_id='same-event'"
            ).fetchone()[0]
        )

    intelligence.begin_account_deletion(
        "alice",
        "owner-scope",
        old_generation,
    )
    mobile.begin_account_deletion("alice", "owner-scope", old_generation)
    new_generation = mobile.activate_account_generation(
        "alice",
        replace_deleting=True,
    )
    assert new_generation != old_generation

    replacement = intelligence.ingest_events(
        "alice",
        "iphone-new",
        [_location_event("same-event", owner_time=946_684_900)],
        "1",
    )
    assert replacement["accepted"] == 1
    with pytest.raises(PermissionError, match="deletion tombstone"):
        intelligence.ingest_events(
            old_storage_owner,
            "iphone-old",
            [_location_event("late-old", owner_time=946_684_901)],
            "2",
        )

    deleted = intelligence.delete_account(
        "alice",
        account_generation=old_generation,
    )

    assert deleted["state"] == "complete"
    assert intelligence.latest_snapshot("alice", "location")["observed_at"] == 946_684_900
    assert intelligence.account_deletion_status("alice") is None
    assert intelligence.account_deletion_status(
        "alice",
        old_generation,
    )["status"] == "complete"
    exported = intelligence.export_account("alice")
    assert exported["account_generation"] == new_generation


def test_device_command_queue_survives_delivery_and_ack(store, monkeypatch):
    wakes = []
    monkeypatch.setattr(
        "hermes_cli.dashboard_auth.mobile_notifications.deliver_account_background_wake",
        lambda **kwargs: wakes.append(kwargs) or {"state": "delivered"},
    )
    first = store.queue_device_command(
        "alice", "ios-reminders", "create", {"title": "带伞"},
        device_id="iphone", idempotency_key="reminder-1",
    )
    duplicate = store.queue_device_command(
        "alice", "ios-reminders", "create", {"title": "重复"},
        device_id="iphone", idempotency_key="reminder-1",
    )
    commands = store.pull_device_commands("alice", "iphone")["commands"]

    assert duplicate["id"] == first["id"]
    assert first["wake"]["state"] == "delivered"
    assert wakes == [{
        "owner_id": "alice",
        "command_id": first["id"],
        "expires_at": None,
    }]
    assert duplicate["duplicate"] is True
    assert commands[0]["payload"]["title"] == "带伞"
    assert store.pull_device_commands("bob", "iphone")["commands"] == []
    assert store.ack_device_command("alice", first["id"], result={"native_id": "r1"}) is True
    assert store.ack_device_command("alice", first["id"], result={}) is False


def test_failed_device_command_retries_then_becomes_terminal(store):
    command = store.queue_device_command("alice", "ios-device", "open-url", {"url": "https://example.test"})
    for expected_attempt in (1, 2):
        pulled = store.pull_device_commands("alice", "iphone")["commands"]
        assert [item["id"] for item in pulled] == [command["id"]]
        assert store.ack_device_command(
            "alice", command["id"], status="failed", error=f"transient-{expected_attempt}",
        ) is True
        with sqlite3.connect(store.path) as conn:
            status, acknowledged_at = conn.execute(
                "SELECT status,acknowledged_at FROM ios_device_commands WHERE id=?",
                (command["id"],),
            ).fetchone()
        assert status == "pending"
        assert acknowledged_at is None

    pulled = store.pull_device_commands("alice", "iphone")["commands"]
    assert [item["id"] for item in pulled] == [command["id"]]
    assert store.ack_device_command(
        "alice", command["id"], status="failed", error="terminal",
    ) is True
    with sqlite3.connect(store.path) as conn:
        status, acknowledged_at = conn.execute(
            "SELECT status,acknowledged_at FROM ios_device_commands WHERE id=?",
            (command["id"],),
        ).fetchone()
    assert status == "failed"
    assert acknowledged_at is not None
    assert store.pull_device_commands("alice", "iphone")["commands"] == []


def test_device_command_idempotency_key_cannot_cross_action_boundary(store):
    store.queue_device_command(
        "alice",
        "ios-reminders",
        "create",
        {"title": "draft"},
        idempotency_key="shared-key",
    )
    with pytest.raises(ValueError, match="already belongs to ios-reminders:create"):
        store.queue_device_command(
            "alice",
            "ios-calendar",
            "create",
            {"title": "wrong target"},
            idempotency_key="shared-key",
        )


def test_native_action_metadata_fails_closed_for_unknown_actions():
    assert ios_native_action_metadata("ios-calendar", "create")["confirmation"] == "required"
    assert ios_native_action_metadata("ios-clipboard", "read")["risk"] == "read"
    unknown = ios_native_action_metadata("ios-device", "future-write")
    assert unknown["risk"] == "destructive"
    assert unknown["confirmation"] == "required"


@pytest.mark.parametrize(
    ("capability", "action", "risk", "confirmation", "permission"),
    [
        ("ios-media", "get", "read", "none", "media"),
        ("ios-media", "control", "write", "required", "media"),
        ("ios-media", "pause", "write", "required", "media"),
        ("ios-nfc", "scan", "read", "required", "nfc"),
        ("ios-health-write", "authorize", "write", "required", "health"),
        ("ios-health-write", "write", "write", "required", "health"),
        ("ios-photos", "ocr", "read", "none", "photos"),
        ("ios-device", "open-url", "write", "required", "device"),
    ],
)
def test_native_action_metadata_matches_device_contract(
    capability, action, risk, confirmation, permission,
):
    metadata = ios_native_action_metadata(capability, action)
    assert metadata["risk"] == risk
    assert metadata["confirmation"] == confirmation
    assert metadata["permission"] == permission


@pytest.mark.parametrize("action", ["search", "state"])
def test_unregistered_search_and_state_actions_fail_closed(action):
    metadata = ios_native_action_metadata("ios-device", action)
    assert metadata["risk"] == "destructive"
    assert metadata["confirmation"] == "required"


def test_pulled_commands_include_canonical_native_action_metadata(store):
    command = store.queue_device_command("alice", "ios-reminders", "create", {"title": "x"})
    pulled = store.pull_device_commands("alice", "iphone")["commands"][0]
    assert pulled["id"] == command["id"]
    assert pulled["action_metadata"]["permission"] == "reminders"
    assert pulled["action_metadata"]["confirmation"] == "required"


def test_device_command_delivery_lease_recovers_unacknowledged_pull(store):
    command = store.queue_device_command("alice", "ios-location", "refresh")
    assert store.pull_device_commands("alice", "iphone")["commands"][0]["id"] == command["id"]
    assert store.pull_device_commands("alice", "iphone")["commands"] == []

    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE ios_device_commands SET delivered_at=delivered_at-121 WHERE id=?",
            (command["id"],),
        )
    recovered = store.pull_device_commands("alice", "iphone", lease_seconds=120)
    assert recovered["commands"][0]["id"] == command["id"]


def test_device_command_max_attempts_expires_a_poisoned_command(store):
    command = store.queue_device_command("alice", "ios-location", "refresh")
    first = store.pull_device_commands("alice", "iphone", max_attempts=1)
    assert first["commands"][0]["id"] == command["id"]
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE ios_device_commands SET delivered_at=delivered_at-121 WHERE id=?",
            (command["id"],),
        )

    assert store.pull_device_commands("alice", "iphone", max_attempts=1)["commands"] == []
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT status FROM ios_device_commands WHERE id=?", (command["id"],)
        ).fetchone()[0] == "expired"


def test_trajectory_window_is_account_scoped_and_time_ordered(store):
    base = 1_700_000_000
    for owner, latitude in (("alice", 24.9), ("bob", 25.1)):
        store.ingest_events(
            owner,
            "iphone",
            [{
                "event_id": f"{owner}-point",
                "kind": "location",
                "observed_at": base + 60,
                "payload": {"latitude": latitude, "longitude": 118.6, "motion": "cycling"},
            }],
            owner,
        )
    points = store.list_trajectory_between("alice", base, base + 120)
    assert [point["event_id"] for point in points] == ["alice-point"]


def test_watch_source_device_is_preserved_in_the_merged_timeline(store):
    store.ingest_events(
        "alice",
        "iphone-relay",
        [{
            "event_id": "watch-point",
            "kind": "location",
            "observed_at": 1_700_000_000,
            "payload": {
                "latitude": 24.9,
                "longitude": 118.6,
                "motion": "walking",
                "source_device_id": "watch-installation",
            },
        }],
        "watch-cursor",
    )

    points = store.list_trajectory_between("alice", 1_699_999_999, 1_700_000_001)
    assert points[0]["device_id"] == "watch-installation"
    assert store.latest_snapshot("alice", "location")["device_id"] == "watch-installation"
    assert store.get_upload_cursor("alice", "iphone-relay") == "watch-cursor"


def test_source_device_id_cannot_overwrite_another_self_uploading_device(store):
    store.ingest_events(
        "alice",
        "device-a",
        [{
            "event_id": "a-location",
            "kind": "location",
            "observed_at": 1_700_000_000,
            "payload": {"latitude": 24.9, "longitude": 118.6},
        }],
        "a-cursor",
    )
    store.ingest_events(
        "alice",
        "device-b",
        [{
            "event_id": "b-spoof",
            "kind": "location",
            "observed_at": 1_700_000_100,
            "payload": {
                "latitude": 1.0,
                "longitude": 2.0,
                "source_device_id": "device-a",
            },
        }],
        "b-cursor",
    )

    # Spoof is attributed to device-b, not device-a.
    points = store.list_trajectory_between("alice", 1_699_999_999, 1_700_000_200)
    spoof = next(point for point in points if point["event_id"] == "b-spoof")
    assert spoof["device_id"] == "device-b"
    assert store.latest_snapshot("alice", "location")["device_id"] == "device-b"
    with sqlite3.connect(store.path) as conn:
        a_observed = conn.execute(
            "SELECT observed_at FROM ios_snapshots "
            "WHERE owner_id=? AND kind=? AND device_id=?",
            ("alice", "location", "device-a"),
        ).fetchone()[0]
        b_observed = conn.execute(
            "SELECT observed_at FROM ios_snapshots "
            "WHERE owner_id=? AND kind=? AND device_id=?",
            ("alice", "location", "device-b"),
        ).fetchone()[0]
    assert a_observed == 1_700_000_000
    assert b_observed == 1_700_000_100


def test_active_accounts_tracks_ingest_and_command_activity(store):
    store.record_snapshot("alice", "power", {"level": 0.5})
    store.queue_device_command("bob", "ios-location", "refresh")
    assert set(store.active_accounts()) == {"alice", "bob"}


def test_fixed_study_place_is_not_inferred_as_home(store):
    tz = ZoneInfo("Asia/Shanghai")
    events = []
    for index, day in enumerate((1, 2), start=1):
        arrived = int(datetime(2026, 7, day, 23, 30, tzinfo=tz).timestamp())
        events.append({
            "event_id": f"study-night-{index}",
            "kind": "place-visit",
            "observed_at": arrived,
            "payload": {
                "place_id": "study-quanzhou-91-bainaohui",
                "name": "泉州九一百脑汇",
                "arrived_at": arrived,
                "departed_at": arrived + 7 * 3600,
                "indoor": True,
                "fixed": True,
            },
        })
    store.ingest_events("alice", "iphone", events, "study-nights")

    assert store.learn_home("alice", "Asia/Shanghai") is None


def test_fixed_study_place_is_excluded_from_home_without_client_fixed_marker(store):
    tz = ZoneInfo("Asia/Shanghai")
    events = []
    for day in (1, 2):
        arrived = int(datetime(2026, 7, day, 23, 30, tzinfo=tz).timestamp())
        events.append({
            "event_id": f"study-unmarked-{day}",
            "kind": "place-visit",
            "observed_at": arrived,
            "payload": {
                "place_id": "study-quanzhou-91-bainaohui",
                "name": "泉州九一百脑汇",
                "arrived_at": arrived,
                "departed_at": arrived + 7 * 3600,
                "indoor": True,
            },
        })
    store.ingest_events("alice", "iphone", events, "study-unmarked")

    assert store.learn_home("alice", "Asia/Shanghai") is None


def test_home_learning_counts_stays_that_begin_after_midnight(store):
    tz = ZoneInfo("Asia/Shanghai")
    events = []
    for day in (2, 3):
        arrived = int(datetime(2026, 7, day, 0, 30, tzinfo=tz).timestamp())
        events.append({
            "event_id": f"home-early-{day}",
            "kind": "place-visit",
            "observed_at": arrived,
            "payload": {
                "place_id": "home-candidate",
                "name": "家",
                "arrived_at": arrived,
                "departed_at": arrived + 5 * 3600,
                "indoor": True,
            },
        })
    store.ingest_events("alice", "iphone", events, "home-early")

    assert store.learn_home("alice", "Asia/Shanghai")["place_id"] == "home-candidate"


def test_home_learning_requires_distinct_overnight_visits(store, monkeypatch):
    tz = ZoneInfo("Asia/Shanghai")
    arrived = int(datetime(2026, 7, 1, 20, 0, tzinfo=tz).timestamp())
    departed = int(datetime(2026, 7, 4, 8, 0, tzinfo=tz).timestamp())
    store.ingest_events(
        "alice",
        "iphone",
        [{
            "event_id": "one-long-stay",
            "kind": "place-visit",
            "observed_at": arrived,
            "payload": {
                "place_id": "single-candidate",
                "arrived_at": arrived,
                "departed_at": departed,
                "indoor": True,
            },
        }],
        "one-long-stay",
    )

    assert store.learn_home("alice", "Asia/Shanghai") is None

    open_arrival = int(datetime(2026, 7, 5, 20, 0, tzinfo=tz).timestamp())
    store.ingest_events(
        "alice",
        "iphone",
        [{
            "event_id": "open-evening-stay",
            "kind": "place-visit",
            "observed_at": open_arrival,
            "payload": {
                "place_id": "single-candidate",
                "arrived_at": open_arrival,
                "departed_at": None,
                "indoor": True,
            },
        }],
        "open-evening-stay",
    )
    monkeypatch.setattr("hermes_cli.ios_intelligence.time.time", lambda: open_arrival + 3600)

    assert store.learn_home("alice", "Asia/Shanghai") is None


def test_behavior_prediction_honors_configured_timezone(store):
    # 2024-01-07 23:00 America/New_York is Sunday; Shanghai is already Monday 12:00.
    instant = int(datetime(2024, 1, 8, 4, 0, tzinfo=ZoneInfo("UTC")).timestamp())
    shanghai = store.evaluate_behavior("alice", instant, timezone="Asia/Shanghai")
    new_york = store.evaluate_behavior("alice", instant, timezone="America/New_York")
    assert shanghai["timezone"] == "Asia/Shanghai"
    assert new_york["timezone"] == "America/New_York"
    assert shanghai["calendar_context"]["weekday"] == 0  # Monday
    assert new_york["calendar_context"]["weekday"] == 6  # Sunday
    assert shanghai["calendar_context"]["is_weekend"] is False
    assert new_york["calendar_context"]["is_weekend"] is True
    assert store.weather_month(instant, timezone="America/New_York") == "2024-01"
    assert store.weather_month(instant, timezone="Asia/Shanghai") == "2024-01"


def test_behavior_prediction_uses_visits_and_motion(store):
    base = 1_700_000_000
    events = []
    for index in range(4):
        arrived = base - (index + 1) * 7 * 86400
        events.append({
            "event_id": f"completed-{index}", "kind": "place-visit", "observed_at": arrived,
            "payload": {
                "place_id": "home", "name": "家", "arrived_at": arrived,
                "departed_at": arrived + 3600, "indoor": True,
            },
        })
    events.append({
        "event_id": "current", "kind": "place-visit", "observed_at": base,
        "payload": {"place_id": "home", "name": "家", "arrived_at": base, "indoor": True},
    })
    store.ingest_events("alice", "iphone", events, "visits")
    store.record_snapshot("alice", "motion", {"state": "walking"}, observed_at=base + 300)

    result = store.evaluate_behavior("alice", base + 300)
    assert result["current_place"]["place_id"] == "home"
    assert result["likely_to_leave"] is True
    assert result["leave_probability"] >= 0.9
    assert result["frequent_destinations"][0]["place_id"] == "home"


def test_calendar_holiday_feature_only_uses_events_overlapping_today(store):
    tz = ZoneInfo("Asia/Shanghai")
    now = int(datetime(2026, 7, 20, 10, 0, tzinfo=tz).timestamp())  # Monday
    yesterday = now - 24 * 3600
    store.record_snapshot(
        "alice",
        "calendar",
        {"title": "Festival holiday", "start": yesterday, "end": yesterday + 3600},
        observed_at=yesterday,
    )

    weekday = store.evaluate_behavior("alice", now)
    assert weekday["calendar_context"]["is_holiday"] is False
    assert weekday["calendar_context"]["day_type"] == "weekday"

    store.record_snapshot(
        "alice",
        "calendar",
        {"title": "今天休息", "start": now, "end": now + 3600},
        observed_at=now,
    )
    holiday = store.evaluate_behavior("alice", now)
    assert holiday["calendar_context"]["is_holiday"] is True
    assert holiday["calendar_context"]["day_type"] == "holiday"


def test_disabled_non_motion_mcp_features_are_excluded_from_behavior_prediction(store):
    """V4 §11: unhealthy MCPs must not poison leave/calendar/context features."""

    tz = ZoneInfo("Asia/Shanghai")
    now = int(datetime(2026, 7, 20, 10, 0, tzinfo=tz).timestamp())  # Monday
    store.record_snapshot(
        "alice",
        "calendar",
        {"title": "今天休息", "start": now, "end": now + 3600},
        observed_at=now,
    )
    store.record_snapshot(
        "alice",
        "power",
        {"battery_level": 0.12, "low_power_mode": True},
        observed_at=now,
    )
    store.record_snapshot(
        "alice",
        "screen-time",
        {"total_minutes": 240},
        observed_at=now,
    )
    store.record_snapshot(
        "alice",
        "health-sleep",
        {"hours": 7.5},
        observed_at=now,
    )
    store.record_snapshot(
        "alice",
        "watch",
        {"connected": True},
        observed_at=now,
    )
    store.record_snapshot(
        "alice",
        "motion",
        {"state": "walking"},
        observed_at=now,
    )

    full = store.evaluate_behavior("alice", now)
    assert full["calendar_context"]["is_holiday"] is True
    assert "power" in full["context_features"]
    assert "screen_time" in full["context_features"]
    assert "sleep" in full["context_features"]
    assert "watch" in full["context_features"]
    assert full["motion_weight"] == 1.0
    assert full["leave_probability"] >= 0.9

    disabled = store.evaluate_behavior(
        "alice",
        now,
        feature_weights={
            "ios-calendar": 0.0,
            "ios-power": 0.0,
            "ios-screen-time": 0.0,
            "ios-health-sleep": 0.0,
            "ios-watch": 0.0,
            "ios-motion": 0.0,
        },
    )
    assert disabled["calendar_weight"] == 0.0
    assert disabled["calendar_context"]["is_holiday"] is False
    assert disabled["calendar_context"]["day_type"] == "weekday"
    assert disabled["calendar_context"]["calendar_weight"] == 0.0
    assert "power" not in disabled["context_features"]
    assert "screen_time" not in disabled["context_features"]
    assert "sleep" not in disabled["context_features"]
    assert "watch" not in disabled["context_features"]
    assert set(disabled["excluded_context_features"]) >= {
        "power",
        "screen_time",
        "sleep",
        "watch",
    }
    assert disabled["motion_state"] == "walking"
    assert disabled["motion_weight"] == 0.0
    assert disabled["effective_motion_state"] is None
    assert disabled["leave_probability"] == 0.15
    assert disabled["applied_feature_weights"]["ios-calendar"] == 0.0
    assert disabled["applied_feature_weights"]["ios-power"] == 0.0

    degraded = store.evaluate_behavior(
        "alice",
        now,
        feature_weights={
            "ios-power": 0.5,
            "ios-screen-time": 0.2,
        },
    )
    assert degraded["context_features"]["power"]["feature_weight"] == 0.5
    assert degraded["context_features"]["power"]["capability"] == "ios-power"
    assert degraded["context_features"]["screen_time"]["feature_weight"] == 0.2
    assert "sleep" in degraded["context_features"]


@pytest.mark.parametrize("kind", ["calendar", "reminder"])
def test_mutable_eventkit_items_use_only_the_indexed_current_revision(store, monkeypatch, kind):
    monkeypatch.setattr("hermes_cli.ios_intelligence.time.time", lambda: 1_700_000_000)
    base = 1_784_313_600
    original = {
        "event_id": f"{kind}:item-1:original",
        "kind": kind,
        "observed_at": base,
        "payload": {"id": "item-1", "title": "Festival holiday", "start": base, "end": base + 3600},
    }
    store.ingest_events("alice", "iphone", [original], "original")
    store.ingest_events(
        "alice",
        "iphone",
        [{
            "event_id": f"{kind}-index:1",
            "kind": f"{kind}-index",
            "observed_at": base + 1,
            "payload": {
                "ids": ["item-1"],
                "versions": {"item-1": original["event_id"]},
            },
        }],
        "index-1",
    )

    monkeypatch.setattr("hermes_cli.ios_intelligence.time.time", lambda: 1_700_000_100)
    edited = {
        "event_id": f"{kind}:item-1:edited",
        "kind": kind,
        "observed_at": base,
        "payload": {"id": "item-1", "title": "Work", "start": base, "end": base + 3600},
    }
    store.ingest_events("alice", "iphone", [edited], "edited")
    store.ingest_events(
        "alice",
        "iphone",
        [{
            "event_id": f"{kind}-index:2",
            "kind": f"{kind}-index",
            "observed_at": base + 2,
            "payload": {
                "ids": ["item-1"],
                "versions": {"item-1": edited["event_id"]},
            },
        }],
        "index-2",
    )

    assert [item["data"]["title"] for item in store.list_snapshots("alice", kind)] == ["Work"]
    assert store.latest_snapshot("alice", kind)["data"]["title"] == "Work"
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM ios_events WHERE owner_id='alice' AND kind=?",
            (kind,),
        ).fetchone()[0] == 2


def test_collection_index_supports_exact_revert_and_deletion_without_losing_raw_history(store, monkeypatch):
    base = 1_784_313_600
    versions = [
        {
            "event_id": "calendar:item-1:holiday",
            "kind": "calendar",
            "observed_at": base,
            "payload": {"id": "item-1", "title": "Holiday", "start": base, "end": base + 3600},
        },
        {
            "event_id": "calendar:item-1:work",
            "kind": "calendar",
            "observed_at": base,
            "payload": {"id": "item-1", "title": "Work", "start": base, "end": base + 3600},
        },
    ]
    for index, event in enumerate(versions, start=1):
        monkeypatch.setattr(
            "hermes_cli.ios_intelligence.time.time",
            lambda index=index: 1_700_000_000 + index,
        )
        store.ingest_events("alice", "iphone", [event], f"event-{index}")
        store.ingest_events(
            "alice",
            "iphone",
            [{
                "event_id": f"calendar-index:{index}",
                "kind": "calendar-index",
                "observed_at": base + index,
                "payload": {
                    "ids": ["item-1"],
                    "versions": {"item-1": event["event_id"]},
                },
            }],
            f"index-{index}",
        )

    monkeypatch.setattr("hermes_cli.ios_intelligence.time.time", lambda: 1_700_000_003)
    store.ingest_events(
        "alice",
        "iphone",
        [{
            "event_id": "calendar-index:3",
            "kind": "calendar-index",
            "observed_at": base + 3,
            "payload": {
                "ids": ["item-1"],
                "versions": {"item-1": versions[0]["event_id"]},
            },
        }],
        "index-3",
    )
    assert store.list_snapshots("alice", "calendar")[0]["data"]["title"] == "Holiday"

    monkeypatch.setattr("hermes_cli.ios_intelligence.time.time", lambda: 1_700_000_004)
    store.ingest_events(
        "alice",
        "iphone",
        [{
            "event_id": "calendar-index:4",
            "kind": "calendar-index",
            "observed_at": base + 4,
            "payload": {"ids": [], "versions": {}},
        }],
        "index-4",
    )
    assert store.list_snapshots("alice", "calendar") == []
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM ios_events WHERE owner_id='alice' AND kind='calendar'"
        ).fetchone()[0] == 2


def test_current_collection_merges_devices_and_prefers_the_freshest_device_index(store, monkeypatch):
    base = 1_784_313_600
    for index, (device, title) in enumerate((("ipad", "Stale"), ("iphone", "Current")), start=1):
        monkeypatch.setattr(
            "hermes_cli.ios_intelligence.time.time",
            lambda index=index: 1_700_000_000 + index,
        )
        event_id = f"calendar:item-1:{device}"
        store.ingest_events(
            "alice",
            device,
            [{
                "event_id": event_id,
                "kind": "calendar",
                "observed_at": base,
                "payload": {"id": "item-1", "title": title, "start": base, "end": base + 3600},
            }, {
                "event_id": f"calendar-index:{device}",
                "kind": "calendar-index",
                "observed_at": base + index,
                "payload": {"ids": ["item-1"], "versions": {"item-1": event_id}},
            }],
            device,
        )

    items = store.list_snapshots("alice", "calendar")
    assert len(items) == 1
    assert items[0]["device_id"] == "iphone"
    assert items[0]["data"]["title"] == "Current"


def test_weather_quota_has_soft_and_hard_limits(store):
    soft = store.reserve_weather_requests(WEATHER_SOFT_LIMIT)
    assert soft["allowed"] is True
    assert soft["soft_limited"] is True
    final = store.reserve_weather_requests(WEATHER_MONTHLY_LIMIT - WEATHER_SOFT_LIMIT)
    assert final["used"] == WEATHER_MONTHLY_LIMIT
    assert final["exhausted"] is True
    denied = store.reserve_weather_requests()
    assert denied["allowed"] is False
    assert denied["used"] == WEATHER_MONTHLY_LIMIT


def test_active_forecast_expires_from_native_today_view(store):
    now = int(datetime.now().timestamp())
    active = store.record_active_forecast(
        "alice", {"id": "rain-1", "valid_from": now, "valid_until": now + 1800, "summary": "有雨"}
    )
    store.record_active_forecast(
        "alice", {"id": "old", "valid_from": now - 3600, "valid_until": now - 1, "summary": "已结束"}
    )
    store.record_active_forecast(
        "alice", {"id": "future", "valid_from": now + 60, "valid_until": now + 1800, "summary": "未开始"}
    )
    store.record_active_forecast(
        "bob", {"id": "rain-1", "valid_from": now, "valid_until": now + 1800, "summary": "晴"}
    )
    assert active["id"] == "rain-1"
    assert [item["id"] for item in store.active_forecast("alice", now)] == ["rain-1"]
    assert store.active_forecast("alice", now)[0]["is_active"] is True
    assert store.active_forecast("alice", now)[0]["starts_at"] == now
    assert store.active_forecast("alice", now)[0]["expires_at"] == now + 1800
    assert store.today_snapshot("alice")["active_forecasts"][0]["data"]["summary"] == "有雨"
    assert store.today_snapshot("alice")["schema_version"] == "hermes.ios-intelligence.snapshot.v1"
    assert store.active_forecast("bob", now)[0]["data"]["summary"] == "晴"


def test_notification_outbox_is_idempotent_and_expires(store):
    now = int(datetime.now().timestamp())
    first = store.enqueue_notification(
        "alice", {"title": "带伞"}, idempotency_key="rain-1", expires_at=now + 300
    )
    duplicate = store.enqueue_notification(
        "alice", {"title": "重复"}, idempotency_key="rain-1", expires_at=now + 300
    )
    assert duplicate["id"] == first["id"]
    assert store.pending_notifications()[0]["payload"]["title"] == "带伞"
    device_deliveries = {
        "device-a": {
            "state": "delivered",
            "attempts": 1,
            "last_error": "",
            "updated_at": now * 1000,
        },
        "device-b": {
            "state": "retry",
            "attempts": 1,
            "last_error": "Shutdown",
            "updated_at": now * 1000,
        },
    }
    assert store.update_notification_delivery(
        first["id"], "retry", 1, "Shutdown", device_deliveries
    ) is True
    assert store.pending_notifications()[0]["device_deliveries"] == device_deliveries
    assert store.update_notification_delivery(
        first["id"], "delivered", 2, device_deliveries=device_deliveries
    ) is True
    assert store.pending_notifications() == []


def test_notification_outbox_migrates_legacy_schema(tmp_path):
    path = tmp_path / "legacy.sqlite"
    now = int(datetime.now().timestamp())
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE ios_notification_outbox ("
            "id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, "
            "payload_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', "
            "deliveries INTEGER NOT NULL DEFAULT 0, not_before INTEGER NOT NULL, "
            "expires_at INTEGER NOT NULL, last_error TEXT NOT NULL DEFAULT '', "
            "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
            "UNIQUE(owner_id, idempotency_key))"
        )
        conn.execute(
            "INSERT INTO ios_notification_outbox VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy-notification",
                "alice",
                "legacy-key",
                '{"title":"legacy"}',
                "pending",
                0,
                now,
                now + 300,
                "",
                now,
                now,
            ),
        )

    store = IOSIntelligenceStore(path)

    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(ios_notification_outbox)")}
    assert "device_deliveries_json" in columns
    pending = store.pending_notifications(now=now)
    assert pending[0]["id"] == "legacy-notification"
    assert pending[0]["device_deliveries"] == {}


def test_notification_claim_is_atomic_and_recovers_after_lease_expiry(store):
    now = int(datetime.now().timestamp())
    queued = store.enqueue_notification(
        "alice",
        {"title": "Rain"},
        idempotency_key="lease-rain-1",
        expires_at=now + 300,
        now=now,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(
            pool.map(
                lambda _: store.claim_pending_notifications(
                    now=now,
                    lease_seconds=15,
                ),
                range(2),
            )
        )

    claimed = [item for batch in claims for item in batch]
    assert [item["id"] for item in claimed] == [queued["id"]]
    first_token = claimed[0]["lease_token"]
    assert store.pending_notifications(now=now) == []
    assert store.claim_pending_notifications(now=now + 14) == []

    reclaimed = store.claim_pending_notifications(now=now + 15)
    assert len(reclaimed) == 1
    assert reclaimed[0]["lease_token"] != first_token
    assert store.update_notification_delivery(
        queued["id"],
        "delivered",
        1,
        lease_token=first_token,
    ) is False
    assert store.update_notification_delivery(
        queued["id"],
        "delivered",
        1,
        lease_token=reclaimed[0]["lease_token"],
    ) is True


def test_weather_outbox_reconciliation_expires_delivery_and_removes_map_card(store):
    now = int(datetime.now().timestamp())
    queued = store.enqueue_notification(
        "alice",
        {"category": "smart-weather", "event_key": "rain", "title": "Rain"},
        idempotency_key="weather:rain:1",
        expires_at=now + 3600,
        not_before=now + 1800,
        now=now,
    )
    store.record_active_forecast(
        "alice",
        {
            "id": queued["id"],
            "category": "smart-weather",
            "event_key": "rain",
            "valid_from": now,
            "valid_until": now + 3600,
        },
    )

    result = store.expire_pending_weather_notifications("alice", now=now + 60)

    assert result == {"expired": 1, "forecasts_removed": 1}
    assert store.pending_notifications(now=now + 1800) == []
    assert store.active_forecast("alice", now) == []


def test_weather_reconciliation_preserves_active_delivery_lease(store):
    now = 1_700_000_000
    queued = store.enqueue_notification(
        "alice",
        {
            "category": "smart-weather",
            "event_key": "rain-A",
            "title": "Rain",
        },
        idempotency_key="weather:claim-expire:1",
        expires_at=now + 300,
        now=now,
    )
    claimed = store.claim_pending_notifications(now=now, lease_seconds=30)
    assert [item["id"] for item in claimed] == [queued["id"]]
    old_token = claimed[0]["lease_token"]

    # Active delivering lease must not be stolen mid-APNs.
    result = store.expire_pending_weather_notifications(
        "alice",
        now=now + 1,
        keep_event_key="rain-B",
    )
    assert result == {"expired": 0, "forecasts_removed": 0}
    assert store.update_notification_delivery(
        queued["id"],
        "delivered",
        1,
        lease_token=old_token,
        now=now + 1,
    ) is True
    with sqlite3.connect(store.path) as conn:
        state = conn.execute(
            "SELECT state FROM ios_notification_outbox WHERE id=?",
            (queued["id"],),
        ).fetchone()[0]
    assert state == "delivered"


def test_weather_reconciliation_expires_stale_delivery_lease_after_timeout(store):
    now = 1_700_000_000
    queued = store.enqueue_notification(
        "alice",
        {
            "category": "smart-weather",
            "event_key": "rain-A",
            "title": "Rain",
        },
        idempotency_key="weather:claim-expire-stale:1",
        expires_at=now + 300,
        now=now,
    )
    claimed = store.claim_pending_notifications(now=now, lease_seconds=15)
    assert [item["id"] for item in claimed] == [queued["id"]]
    old_token = claimed[0]["lease_token"]

    result = store.expire_pending_weather_notifications(
        "alice",
        now=now + 15,
        keep_event_key="rain-B",
    )
    assert result == {"expired": 1, "forecasts_removed": 0}
    assert store.update_notification_delivery(
        queued["id"],
        "delivered",
        1,
        lease_token=old_token,
        now=now + 15,
    ) is False
    with sqlite3.connect(store.path) as conn:
        state, lease_token, leased_until = conn.execute(
            "SELECT state,lease_token,leased_until FROM ios_notification_outbox "
            "WHERE id=?",
            (queued["id"],),
        ).fetchone()
    assert (state, lease_token, leased_until) == ("expired", "", 0)


def test_weather_reconciliation_does_not_race_active_worker_completion(store):
    now = 1_700_000_000
    queued = store.enqueue_notification(
        "alice",
        {
            "category": "smart-weather",
            "event_key": "storm-A",
            "title": "Storm",
        },
        idempotency_key="weather:threaded-claim-expire:1",
        expires_at=now + 300,
        now=now,
    )
    claim_complete = threading.Event()
    expiration_complete = threading.Event()
    outcome: dict[str, object] = {}

    def worker() -> None:
        claimed = store.claim_pending_notifications(now=now, lease_seconds=30)
        assert len(claimed) == 1
        outcome["lease_token"] = claimed[0]["lease_token"]
        claim_complete.set()
        assert expiration_complete.wait(timeout=5)
        outcome["worker_update"] = store.update_notification_delivery(
            queued["id"],
            "delivered",
            1,
            lease_token=str(outcome["lease_token"]),
            now=now + 1,
        )

    def reconciler() -> None:
        assert claim_complete.wait(timeout=5)
        outcome["expiration"] = store.expire_pending_weather_notifications(
            "alice",
            now=now + 1,
            keep_event_key="storm-B",
        )
        expiration_complete.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        worker_future = pool.submit(worker)
        reconcile_future = pool.submit(reconciler)
        worker_future.result(timeout=10)
        reconcile_future.result(timeout=10)

    assert outcome["expiration"] == {"expired": 0, "forecasts_removed": 0}
    assert outcome["worker_update"] is True
    with sqlite3.connect(store.path) as conn:
        state, deliveries = conn.execute(
            "SELECT state,deliveries FROM ios_notification_outbox WHERE id=?",
            (queued["id"],),
        ).fetchone()
    assert (state, deliveries) == ("delivered", 1)


def test_delivered_weather_expiry_records_one_negative_value_label(store):
    now = int(datetime.now().timestamp())
    queued = store.enqueue_notification(
        "alice",
        {"category": "smart-weather", "title": "Rain"},
        idempotency_key="weather:negative:1",
        expires_at=now + 60,
        now=now,
    )
    assert store.update_notification_delivery(queued["id"], "delivered", 1)

    store.expire_pending_weather_notifications("alice", now=now + 61)
    store.expire_pending_weather_notifications("alice", now=now + 120)

    assert store.evaluate_behavior("alice", now=now + 120)["notification_value"] == {
        "samples": 1,
        "useful": 0,
        "score": 0.0,
    }


def test_expired_partial_delivery_reaches_terminal_state_when_event_is_kept(store):
    now = int(datetime.now().timestamp())
    queued = store.enqueue_notification(
        "alice",
        {"category": "smart-weather", "event_key": "rain", "title": "Rain"},
        idempotency_key="weather:partial-expired:1",
        expires_at=now + 60,
        now=now,
    )
    deliveries = {
        "device-a": {"state": "delivered", "attempts": 1},
        "device-b": {"state": "retry", "attempts": 1, "last_error": "timeout"},
    }
    assert store.update_notification_delivery(
        queued["id"], "retry", 1, "timeout", deliveries
    ) is True

    result = store.expire_pending_weather_notifications(
        "alice", now=now + 61, keep_event_key="rain"
    )

    assert result["expired"] == 1
    with sqlite3.connect(store.path) as conn:
        state, lease_token, leased_until = conn.execute(
            "SELECT state,lease_token,leased_until FROM ios_notification_outbox WHERE id=?",
            (queued["id"],),
        ).fetchone()
    assert (state, lease_token, leased_until) == ("expired", "", 0)


def test_partially_delivered_weather_expiry_records_negative_value_label(store):
    now = int(datetime.now().timestamp())
    queued = store.enqueue_notification(
        "alice",
        {"category": "smart-weather", "title": "Rain"},
        idempotency_key="weather:partial-negative:1",
        expires_at=now + 60,
        now=now,
    )
    assert store.update_notification_delivery(
        queued["id"],
        "retry",
        1,
        device_deliveries={
            "iphone": {"state": "delivered"},
            "old-device": {"state": "retry"},
        },
        now=now,
    )

    store.expire_pending_weather_notifications("alice", now=now + 61)

    assert store.evaluate_behavior("alice", now=now + 61)["notification_value"] == {
        "samples": 1,
        "useful": 0,
        "score": 0.0,
    }


def test_opened_weather_feedback_wins_over_automatic_expiry_label(store):
    now = int(datetime.now().timestamp())
    queued = store.enqueue_notification(
        "alice",
        {"category": "smart-weather", "title": "Storm"},
        idempotency_key="weather:positive:1",
        expires_at=now + 60,
        now=now,
    )
    assert store.update_notification_delivery(queued["id"], "delivered", 1)
    store.expire_pending_weather_notifications("alice", now=now + 61)
    store.record_behavior_feedback(
        "alice",
        "notification-value",
        {
            "action": "opened",
            "notification_id": queued["id"],
            "useful": True,
        },
        observed_at=now + 62,
        feedback_id=f"opened:{queued['id']}",
    )

    assert store.evaluate_behavior("alice", now=now + 62)["notification_value"] == {
        "samples": 1,
        "useful": 1,
        "score": 1.0,
    }


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _NoContentResponse:
    status_code = 204

    def raise_for_status(self):
        return None

    def json(self):
        raise AssertionError("HTTP 204 must not attempt to decode a body")


class _HTTPClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.payload)


class _NoContentHTTPClient(_HTTPClient):
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _NoContentResponse()


class _FailingResponse:
    def raise_for_status(self):
        raise RuntimeError("request failed: https://weather.test?key=server-only")

    def json(self):
        return {}


class _FailingHTTPClient(_HTTPClient):
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _FailingResponse()


def test_qweather_uses_server_secret_quota_and_cache(store):
    http = _HTTPClient({"code": "200", "summary": "未来两小时无降水"})
    client = QWeatherClient(store, api_key="server-only", client=http)
    first = client.minutely(24.9, 118.6)
    second = client.minutely(24.9, 118.6)

    assert first["_cache"] is False
    assert second["_cache"] is True
    assert len(http.calls) == 1
    assert http.calls[0][1]["params"]["key"] == "server-only"
    assert "server-only" not in str(first)
    assert store.weather_quota_status()["used"] == 1


def test_qweather_http_204_is_a_valid_empty_result(store):
    client = QWeatherClient(store, api_key="server-only", client=_NoContentHTTPClient({}))

    result = client.minutely(24.9, 118.6)

    assert result == {"code": "204", "_cache": False}


def test_qweather_rejects_and_does_not_cache_business_errors(store):
    http = _HTTPClient({"code": "401", "message": "invalid credential"})
    client = QWeatherClient(store, api_key="server-only", client=http)

    with pytest.raises(RuntimeError, match="code 401"):
        client.current(24.9, 118.6)
    with pytest.raises(RuntimeError, match="code 401"):
        client.current(24.9, 118.6)

    assert len(http.calls) == 2
    # The provider counts each outbound request even when its business code
    # rejects the operation, so the hard cap must reserve both attempts.
    assert store.weather_quota_status()["used"] == 2


def test_qweather_concurrent_cache_misses_cannot_cross_hard_quota(store):
    store.reserve_weather_requests(WEATHER_MONTHLY_LIMIT - 1)
    http = _HTTPClient({"code": "200", "now": {"text": "clear"}})
    client = QWeatherClient(store, api_key="server-only", client=http)
    barrier = threading.Barrier(2)

    def request(longitude):
        barrier.wait(timeout=5)
        try:
            return client.current(24.9, longitude)
        except RuntimeError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(request, [118.6, 118.7]))

    assert sum(isinstance(item, dict) for item in outcomes) == 1
    assert sum("monthly request limit" in item for item in outcomes if isinstance(item, str)) == 1
    assert len(http.calls) == 1
    assert store.weather_quota_status()["used"] == WEATHER_MONTHLY_LIMIT


def test_external_clients_require_server_credentials(store, monkeypatch):
    for name in ("HERMES_QWEATHER_API_KEY", "QWEATHER_API_KEY", "HERMES_AMAP_WEB_API_KEY", "AMAP_WEB_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="QWeather"):
        QWeatherClient(store, api_key=None).current(1, 2)
    with pytest.raises(RuntimeError, match="AMap"):
        AMapClient(api_key=None).reverse_geocode(1, 2)


def test_external_client_errors_redact_server_credentials(store):
    for client, call in [
        (QWeatherClient(store, api_key="server-only", client=_FailingHTTPClient({})), "minutely"),
        (AMapClient(api_key="server-only", client=_FailingHTTPClient({})), "reverse_geocode"),
    ]:
        with pytest.raises(RuntimeError) as error:
            getattr(client, call)(24.9, 118.6)
        assert "server-only" not in str(error.value)


def test_amap_validates_mode_and_status():
    http = _HTTPClient({"status": "1", "route": {"paths": []}})
    client = AMapClient(api_key="server-only", client=http)
    result = client.route("118,24", "119,25", "walking")
    assert result["status"] == "1"
    assert "/v3/direction/walking" in http.calls[0][0]
    with pytest.raises(ValueError, match="mode"):
        client.route("a", "b", "flying")

    failed_v4 = AMapClient(
        api_key="server-only",
        client=_HTTPClient({"errcode": 10001, "errmsg": "invalid key server-only"}),
    )
    with pytest.raises(RuntimeError, match="invalid key") as error:
        failed_v4.route("118,24", "119,25", "cycling")
    assert "server-only" not in str(error.value)
    assert "/v4/direction/bicycling" in failed_v4.client.calls[0][0]


def test_schema_status_reports_code_and_db_user_version(store):
    status = store.schema_status()
    assert status["code_schema_version"] == IOSIntelligenceStore.schema_version
    assert status["db_user_version"] == IOSIntelligenceStore.schema_version
    assert status["schema_version"] == IOSIntelligenceStore.schema_version
    assert status["migrated"] is True
    assert status["compatible"] is True


def test_weather_quota_month_bucket_follows_timezone(store):
    # 2026-07-31 23:30 UTC is already August 1 in Asia/Shanghai.
    instant = int(datetime(2026, 7, 31, 23, 30, tzinfo=ZoneInfo("UTC")).timestamp())
    shanghai = store.weather_month(instant, timezone="Asia/Shanghai")
    utc = store.weather_month(instant, timezone="UTC")
    assert shanghai == "2026-08"
    assert utc == "2026-07"
    reserved = store.reserve_weather_requests(1, now=instant, timezone="Asia/Shanghai")
    assert reserved["allowed"] is True
    assert reserved["month"] == "2026-08"
    assert store.weather_quota_status(now=instant, timezone="Asia/Shanghai")["used"] == 1
    assert store.weather_quota_status(now=instant, timezone="UTC")["used"] == 0


def test_qweather_client_reserves_quota_in_configured_timezone(store):
    class _HTTPClient:
        def __init__(self, payload):
            self.payload = payload
            self.calls = []

        def get(self, url, params=None):
            self.calls.append((url, dict(params or {})))
            return SimpleNamespace(
                status_code=200,
                json=lambda: self.payload,
                raise_for_status=lambda: None,
            )

    import time as time_module

    instant = int(datetime(2026, 7, 31, 23, 30, tzinfo=ZoneInfo("UTC")).timestamp())
    # Pin wall clock so request() uses the configured timezone for month buckets.
    original_time = time_module.time
    try:
        time_module.time = lambda: float(instant)
        client = QWeatherClient(
            store,
            api_key="server-only",
            client=_HTTPClient({"code": "200", "now": {"temp": "28"}}),
            timezone="Asia/Shanghai",
        )
        client.request("/v7/weather/now", {"location": "118.6,24.9"})
    finally:
        time_module.time = original_time
    assert store.weather_quota_status(now=instant, timezone="Asia/Shanghai")["used"] == 1
    assert store.weather_quota_status(now=instant, timezone="UTC")["used"] == 0


def test_load_ios_feature_weights_fail_closed_and_state_map():
    healthy = load_ios_feature_weights(
        SimpleNamespace(statuses=lambda: [{"name": "ios-motion", "state": "RUNNING"}])
    )
    assert healthy["ios-motion"] == 1.0

    degraded = load_ios_feature_weights(
        SimpleNamespace(
            statuses=lambda: [
                {"name": "ios-motion", "state": "DISABLED"},
                {"name": "ios-power", "state": "QUARANTINED"},
                {"name": "ios-calendar", "state": "DEGRADED"},
            ]
        )
    )
    assert degraded["ios-motion"] == 0.0
    assert degraded["ios-power"] == 0.2
    assert degraded["ios-calendar"] == 0.5

    unavailable = load_ios_feature_weights(
        SimpleNamespace(statuses=lambda: (_ for _ in ()).throw(RuntimeError("down")))
    )
    assert set(KNOWN_FEATURE_CAPABILITIES) <= set(unavailable)
    assert all(weight == 0.5 for weight in unavailable.values())
