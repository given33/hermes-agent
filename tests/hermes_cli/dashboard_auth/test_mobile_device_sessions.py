"""Durable mobile device session, rotation, revocation, and APNs contracts."""
from __future__ import annotations

import contextlib
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_cli.dashboard_auth.mobile_device_store as mobile_device_store
from hermes_cli.dashboard_auth.mobile_device_store import (
    MobileDeviceInfo,
    MobileDeviceStore,
    OwnerMobileTokenProvider,
)
from hermes_cli.dashboard_auth.mobile_notifications import (
    process_account_deletion_outbox,
)


def _device(device_id: str, name: str) -> MobileDeviceInfo:
    return MobileDeviceInfo(
        id=device_id,
        name=name,
        model="iPhone17,1",
        os_version="18.6",
        app_version="2.0.0",
    )


def test_custom_database_path_does_not_change_parent_permissions(tmp_path, monkeypatch):
    db_path = tmp_path / "shared" / "mobile-auth.db"
    calls = []
    monkeypatch.setattr(
        MobileDeviceStore,
        "_restrict_permissions",
        staticmethod(lambda path, mode: calls.append((path, mode))),
    )

    MobileDeviceStore(db_path).connect().close()

    assert (db_path.parent, 0o700) not in calls
    assert (db_path, 0o600) in calls


def test_default_database_path_keeps_private_parent_permissions(tmp_path, monkeypatch):
    db_path = tmp_path / "dashboard" / "mobile-auth.db"
    calls = []
    monkeypatch.setattr(mobile_device_store, "mobile_auth_db_path", lambda: db_path)
    monkeypatch.setattr(
        MobileDeviceStore,
        "_restrict_permissions",
        staticmethod(lambda path, mode: calls.append((path, mode))),
    )

    MobileDeviceStore().connect().close()

    assert (db_path.parent, 0o700) in calls
    assert (db_path, 0o600) in calls


def test_default_database_uses_writable_fallback_when_home_is_full(tmp_path, monkeypatch):
    db_path = tmp_path / "full" / "dashboard" / "mobile-auth.db"
    fallback_root = tmp_path / "fallback"
    db_path.parent.mkdir(parents=True)
    monkeypatch.setattr(mobile_device_store, "mobile_auth_db_path", lambda: db_path)
    monkeypatch.setenv("HERMES_MOBILE_AUTH_FALLBACK_DIR", str(fallback_root))
    real_disk_usage = mobile_device_store.shutil.disk_usage

    def disk_usage(path):
        if Path(path) == db_path.parent:
            return SimpleNamespace(free=0)
        return real_disk_usage(path)

    monkeypatch.setattr(mobile_device_store.shutil, "disk_usage", disk_usage)

    store = MobileDeviceStore()
    assert store.db_path == fallback_root / "dashboard" / "mobile-auth.db"
    store.connect().close()
    assert store.db_path.exists()


def test_fallback_database_migrates_existing_auth_rows(tmp_path, monkeypatch):
    db_path = tmp_path / "full" / "dashboard" / "mobile-auth.db"
    fallback_root = tmp_path / "fallback"
    db_path.parent.mkdir(parents=True)
    source_store = MobileDeviceStore(db_path)
    tokens = source_store.create_session(
        user_id="owner",
        device=_device("device-primary", "Owner iPhone"),
    )
    monkeypatch.setattr(mobile_device_store, "mobile_auth_db_path", lambda: db_path)
    monkeypatch.setenv("HERMES_MOBILE_AUTH_FALLBACK_DIR", str(fallback_root))
    real_disk_usage = mobile_device_store.shutil.disk_usage

    def disk_usage(path):
        if Path(path) == db_path.parent:
            return SimpleNamespace(free=0)
        return real_disk_usage(path)

    monkeypatch.setattr(mobile_device_store.shutil, "disk_usage", disk_usage)

    fallback_store = MobileDeviceStore()
    assert fallback_store.db_path != db_path
    assert fallback_store.verify_access(tokens.access_token, touch=False) is not None
    assert fallback_store.db_path.exists()


def test_sqlite_fallback_snapshot_carries_pending_sidecars(tmp_path):
    source = tmp_path / "source.db"
    destination = tmp_path / "fallback" / "mobile-auth.db"
    destination.parent.mkdir()
    source.write_bytes(b"database")
    Path(f"{source}-wal").write_bytes(b"wal")
    Path(f"{source}-journal").write_bytes(b"journal")
    Path(f"{destination}-wal").write_bytes(b"stale-wal")
    Path(f"{destination}-journal").write_bytes(b"stale-journal")

    mobile_device_store._copy_sqlite_database(source, destination)

    assert destination.read_bytes() == b"database"
    assert Path(f"{destination}-wal").read_bytes() == b"wal"
    assert Path(f"{destination}-journal").read_bytes() == b"journal"


def test_tokens_are_hashed_and_survive_store_reopen(tmp_path):
    db_path = tmp_path / "mobile-auth.db"
    store = MobileDeviceStore(db_path)
    tokens = store.create_session(
        user_id="owner",
        device=_device("device-primary", "Owner iPhone"),
    )

    raw = db_path.read_bytes()
    assert tokens.access_token.encode() not in raw
    assert tokens.refresh_token.encode() not in raw

    reopened = MobileDeviceStore(db_path)
    session = reopened.verify_access(tokens.access_token, touch=False)
    assert session is not None
    assert session.device_id == "device-primary"
    assert session.account_generation == tokens.session.account_generation

    provider = OwnerMobileTokenProvider(lambda: MobileDeviceStore(db_path))
    principal = provider.verify_token(token=tokens.access_token)
    assert principal is not None
    assert principal.principal == "owner"
    assert principal.provider == "owner-mobile"
    assert principal.account_generation == reopened.account_generation(
        "owner", create=False
    )


def test_old_mobile_tokens_cannot_cross_a_same_name_account_generation(tmp_path):
    store = MobileDeviceStore(tmp_path / "mobile-auth.db")
    old = store.create_session(
        user_id="owner",
        device=_device("device-old", "Old iPhone"),
    )
    old_generation = old.session.account_generation

    store.begin_account_deletion("owner", "owner-scope")
    with store.connection() as conn:
        conn.execute(
            "UPDATE mobile_account_deletion_outbox SET state='delivered' "
            "WHERE user_id='owner'"
        )
        conn.commit()
    assert store.clear_completed_account_deletion("owner") is True
    replacement = store.create_session(
        user_id="owner",
        device=_device("device-new", "New iPhone"),
    )
    assert replacement.session.account_generation != old_generation

    # Simulate incomplete cleanup resurrecting the old credential rows. The
    # issuance generation still fences both access and refresh credentials.
    with store.connection() as conn:
        conn.execute(
            "UPDATE mobile_devices SET revoked_at=NULL WHERE id='device-old'"
        )
        conn.execute(
            "UPDATE mobile_sessions SET revoked_at=NULL WHERE id=?",
            (old.session.session_id,),
        )
        conn.commit()

    assert store.verify_access(old.access_token, touch=False) is None
    assert store.rotate_refresh(old.refresh_token) is None
    assert OwnerMobileTokenProvider(lambda: store).verify_token(
        token=old.access_token
    ) is None
    assert store.verify_access(replacement.access_token, touch=False) is not None


def test_late_old_generation_cleanup_preserves_replacement_devices_and_tokens(tmp_path):
    store = MobileDeviceStore(tmp_path / "mobile-auth.db")
    old = store.create_session(
        user_id="owner",
        device=_device("device-old", "Old iPhone"),
    )
    store.register_apns(
        device_id=old.session.device_id,
        token="aa" * 32,
        environment="production",
        bundle_id="com.example.hermes",
    )
    deletion = store.begin_account_deletion("owner", "owner-scope")

    replacement_generation = store.activate_account_generation(
        "owner",
        replace_deleting=True,
    )
    replacement = store.create_session(
        user_id="owner",
        device=_device("device-new", "Replacement iPhone"),
    )
    store.register_apns(
        device_id=replacement.session.device_id,
        token="bb" * 32,
        environment="production",
        bundle_id="com.example.hermes",
    )

    claimed = store.claim_account_deletions(limit=1)[0]
    cleanup = store.finish_account_deletion(
        claimed["id"],
        "delivered",
        deliveries={},
        lease_token=claimed["lease_token"],
    )

    assert cleanup == {
        "updated": True,
        "state": "delivered",
        "devices": 1,
        "sessions": 1,
        "apns": 1,
    }
    assert replacement.session.account_generation == replacement_generation
    assert store.verify_access(replacement.access_token, touch=False) is not None
    assert [item["id"] for item in store.list_devices(user_id="owner")] == [
        "device-new"
    ]
    assert store.account_deletion_status(
        "owner",
        deletion["account_generation"],
    )["state"] == "delivered"


def test_account_generation_is_stable_and_rotates_after_completed_deletion(tmp_path):
    store = MobileDeviceStore(tmp_path / "mobile-auth.db")
    store.create_session(
        user_id="owner",
        device=_device("device-primary", "Owner iPhone"),
    )
    first_generation = store.account_generation("owner")
    assert first_generation.startswith("acctgen_")
    assert MobileDeviceStore(store.db_path).account_generation("OWNER") == first_generation

    store.begin_account_deletion("owner", "owner-scope")
    with store.connection() as conn:
        conn.execute(
            "UPDATE mobile_account_deletion_outbox SET state='delivered' "
            "WHERE user_id='owner'"
        )
        conn.commit()
    assert store.clear_completed_account_deletion("owner") is True
    replacement_generation = store.account_generation("owner")
    assert replacement_generation != first_generation
    assert store.account_deletion_status(
        "owner",
        first_generation,
    )["state"] == "delivered"
    assert store.account_deletion_status("owner") is None

    store.create_session(
        user_id="owner",
        device=_device("device-new", "Replacement iPhone"),
    )
    assert store.account_generation("owner") == replacement_generation


def test_active_account_generation_fails_closed_after_deletion_begins(tmp_path):
    store = MobileDeviceStore(tmp_path / "mobile-auth.db")
    generation = store.account_generation("owner", create=True)

    deletion = store.begin_account_deletion("owner", "owner-scope")

    assert deletion["account_generation"] == generation
    assert store.account_generation("owner", create=False) == generation
    with pytest.raises(PermissionError, match="deletion tombstone"):
        store.account_generation("owner", create=True)


def test_account_deletion_persists_generation_before_any_cleanup_store_is_touched(
    tmp_path,
):
    store = MobileDeviceStore(tmp_path / "mobile-auth.db")
    assert store.account_generation("owner") == ""

    deletion = store.begin_account_deletion("owner", "owner-scope")

    generation = store.account_generation("owner")
    assert generation.startswith("acctgen_")
    assert deletion["account_generation"] == generation


def test_refresh_replay_grace_preserves_the_winning_token_family(tmp_path):
    store = MobileDeviceStore(tmp_path / "mobile-auth.db")
    first = store.create_session(
        user_id="owner",
        device=_device("device-primary", "Owner iPhone"),
    )

    rotated = store.rotate_refresh(first.refresh_token)

    assert rotated is not None
    assert rotated.access_token != first.access_token
    assert rotated.refresh_token != first.refresh_token
    assert store.verify_access(first.access_token, touch=False) is None
    assert store.rotate_refresh(first.refresh_token) is None
    assert store.verify_access(rotated.access_token, touch=False) is not None
    assert store.rotate_refresh(rotated.refresh_token) is not None


def test_logout_revoke_invalidates_access_and_refresh(tmp_path):
    store = MobileDeviceStore(tmp_path / "mobile-auth.db")
    tokens = store.create_session(
        user_id="owner",
        device=_device("device-primary", "Owner iPhone"),
    )

    assert store.revoke_session(refresh_token=tokens.refresh_token) is True
    assert store.verify_access(tokens.access_token, touch=False) is None
    assert store.rotate_refresh(tokens.refresh_token) is None
    assert store.revoke_session(refresh_token=tokens.refresh_token) is False


def test_access_and_refresh_expire_independently(tmp_path):
    now = [1_800_000_000]
    store = MobileDeviceStore(tmp_path / "mobile-auth.db", clock=lambda: now[0])
    tokens = store.create_session(
        user_id="owner",
        device=_device("device-primary", "Owner iPhone"),
    )

    now[0] = tokens.session.access_expires_at
    assert store.verify_access(tokens.access_token, touch=False) is None
    rotated = store.rotate_refresh(tokens.refresh_token)
    assert rotated is not None

    now[0] = rotated.session.refresh_expires_at
    assert store.rotate_refresh(rotated.refresh_token) is None


def test_device_revoke_does_not_affect_other_device(tmp_path):
    store = MobileDeviceStore(tmp_path / "mobile-auth.db")
    phone = store.create_session(
        user_id="owner",
        device=_device("device-phone", "Owner iPhone"),
    )
    tablet = store.create_session(
        user_id="owner",
        device=_device("device-tablet", "Owner iPad"),
    )

    assert store.revoke_device("device-phone") is True
    assert store.verify_access(phone.access_token, touch=False) is None
    assert store.rotate_refresh(phone.refresh_token) is None
    assert store.verify_access(tablet.access_token, touch=False) is not None

    devices = store.list_devices(current_device_id="device-tablet")
    by_id = {item["id"]: item for item in devices}
    assert by_id["device-phone"]["active"] is False
    assert by_id["device-tablet"]["active"] is True
    assert by_id["device-tablet"]["current"] is True


def test_relogin_same_device_replaces_prior_session(tmp_path):
    store = MobileDeviceStore(tmp_path / "mobile-auth.db")
    first = store.create_session(
        user_id="owner",
        device=_device("device-primary", "Old name"),
    )
    second = store.create_session(
        user_id="owner",
        device=_device("device-primary", "New name"),
    )

    assert store.verify_access(first.access_token, touch=False) is None
    assert store.rotate_refresh(first.refresh_token) is None
    assert store.verify_access(second.access_token, touch=False) is not None
    devices = store.list_devices(current_device_id="device-primary")
    assert len(devices) == 1
    assert devices[0]["name"] == "New name"


def test_device_id_cannot_rebind_across_accounts(tmp_path):
    store = MobileDeviceStore(tmp_path / "mobile-auth.db")
    first = store.create_session(
        user_id="owner-a",
        device=_device("shared-device", "Owner A phone"),
    )

    with pytest.raises(PermissionError, match="already bound"):
        store.create_session(
            user_id="owner-b",
            device=_device("shared-device", "Owner B phone"),
        )

    assert store.verify_access(first.access_token, touch=False) is not None
    assert [item["id"] for item in store.list_devices(user_id="owner-a")] == ["shared-device"]
    assert store.list_devices(user_id="owner-b") == []


def test_list_and_revoke_devices_are_scoped_to_user(tmp_path):
    store = MobileDeviceStore(tmp_path / "mobile-auth.db")
    owner_a = store.create_session(
        user_id="owner-a",
        device=_device("owner-a-phone", "A"),
    )
    owner_b = store.create_session(
        user_id="owner-b",
        device=_device("owner-b-phone", "B"),
    )

    listed_a = store.list_devices(user_id="owner-a", current_device_id="owner-a-phone")
    listed_b = store.list_devices(user_id="owner-b", current_device_id="owner-b-phone")
    assert [item["id"] for item in listed_a] == ["owner-a-phone"]
    assert [item["id"] for item in listed_b] == ["owner-b-phone"]

    assert store.revoke_device("owner-b-phone", user_id="owner-a") is False
    assert store.verify_access(owner_b.access_token, touch=False) is not None
    assert store.revoke_device("owner-a-phone", user_id="owner-a") is True
    assert store.verify_access(owner_a.access_token, touch=False) is None


def test_apns_registration_is_redacted_rotated_and_disabled_on_revoke(tmp_path):
    store = MobileDeviceStore(tmp_path / "mobile-auth.db")
    store.create_session(
        user_id="owner",
        device=_device("device-primary", "Owner iPhone"),
    )
    first_token = "a1" * 32
    second_token = "b2" * 32

    first = store.register_apns(
        device_id="device-primary",
        token=f"<{first_token}>",
        environment="sandbox",
        bundle_id="com.given33.hermesagent.nativebeta",
    )
    second = store.register_apns(
        device_id="device-primary",
        token=second_token,
        environment="sandbox",
        bundle_id="com.given33.hermesagent.nativebeta",
    )

    assert first["id"] == second["id"]
    assert second["token_suffix"] == second_token[-8:]
    listed = store.list_devices()[0]["apns"]
    assert listed == [second]
    assert first_token not in str(listed)
    assert second_token not in str(listed)

    assert store.revoke_device("device-primary") is True
    assert store.list_devices()[0]["apns"] == []
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute(
            "SELECT disabled_at FROM mobile_apns_tokens"
        ).fetchone()[0] is not None


def test_logout_disables_apns_delivery_for_last_active_session(tmp_path):
    store = MobileDeviceStore(tmp_path / "mobile-auth.db")
    tokens = store.create_session(
        user_id="owner",
        device=_device("device-primary", "Owner iPhone"),
    )
    store.register_apns(
        device_id="device-primary",
        token="a1" * 32,
        environment="production",
        bundle_id="com.given33.hermesagent.nativebeta",
    )
    assert len(store.list_active_apns_registrations(user_id="owner")) == 1

    assert store.revoke_session(refresh_token=tokens.refresh_token) is True

    assert store.list_active_apns_registrations(user_id="owner") == []
    assert store.list_devices()[0]["apns"] == []


def test_apns_unregister_can_target_one_bundle(tmp_path):
    store = MobileDeviceStore(tmp_path / "mobile-auth.db")
    store.create_session(
        user_id="owner",
        device=_device("device-primary", "Owner iPhone"),
    )
    store.register_apns(
        device_id="device-primary",
        token="a1" * 32,
        environment="sandbox",
        bundle_id="com.given33.hermesagent.nativebeta",
    )

    removed = store.unregister_apns(
        device_id="device-primary",
        environment="sandbox",
        bundle_id="com.given33.hermesagent.nativebeta",
    )

    assert removed == 1
    assert store.list_devices()[0]["apns"] == []


def test_active_apns_delivery_is_strictly_scoped_to_one_account(tmp_path):
    store = MobileDeviceStore(tmp_path / "mobile-auth.db")
    for owner, device_id, token in (
        ("owner-a", "owner-a-phone", "a1" * 32),
        ("owner-b", "owner-b-phone", "b2" * 32),
    ):
        store.create_session(user_id=owner, device=_device(device_id, owner))
        store.register_apns(
            device_id=device_id,
            token=token,
            environment="production",
            bundle_id="com.given33.hermesagent.nativebeta",
        )

    owner_a = store.list_active_apns_registrations(user_id="owner-a")
    owner_b = store.list_active_apns_registrations(user_id="owner-b")
    assert [item["device_id"] for item in owner_a] == ["owner-a-phone"]
    assert [item["device_id"] for item in owner_b] == ["owner-b-phone"]
    assert store.list_active_apns_registrations(user_id="") == []


def test_account_deletion_outbox_retries_then_purges_retained_apns_rows(
    tmp_path,
    monkeypatch,
):
    now = [1_800_000_000]
    bundle_id = "app.sunstone1029.fig1171"
    monkeypatch.setenv("HERMES_APNS_BUNDLE_ID", bundle_id)
    store = MobileDeviceStore(tmp_path / "mobile-auth.db", clock=lambda: now[0])
    tokens = store.create_session(
        user_id="owner",
        device=_device("device-primary", "Owner iPhone"),
    )
    store.register_apns(
        device_id="device-primary",
        token="a1" * 32,
        environment="production",
        bundle_id=bundle_id,
    )

    deletion = store.begin_account_deletion(
        "owner",
        "https://hermes.example|owner",
    )

    assert deletion["state"] == "pending"
    assert deletion["devices"] == deletion["sessions"] == deletion["apns"] == 1
    assert store.verify_access(tokens.access_token, touch=False) is None
    assert store.rotate_refresh(tokens.refresh_token) is None
    assert store.list_active_apns_registrations(user_id="owner") == []
    assert len(store.list_account_deletion_apns_registrations(user_id="owner")) == 1

    payloads = []
    first = process_account_deletion_outbox(
        device_store=store,
        owner_id="owner",
        sender=lambda _registration, payload, _collapse_id: (
            payloads.append(payload) or (503, "Shutdown")
        ),
    )

    assert first[0]["state"] == "retry"
    assert first[0]["cleanup"]["state"] == "retry"
    assert payloads[0]["hermes"]["data"]["owner_scope"] == (
        "https://hermes.example|owner"
    )
    assert payloads[0]["hermes"]["data"]["valid_until"] > now[0]
    assert len(store.list_account_deletion_apns_registrations(user_id="owner")) == 1
    assert store.account_deletion_status("owner")["state"] == "retry"

    now[0] += 60
    second = process_account_deletion_outbox(
        device_store=MobileDeviceStore(store.db_path, clock=lambda: now[0]),
        owner_id="owner",
        sender=lambda _registration, _payload, _collapse_id: (200, ""),
    )

    assert second[0]["state"] == "delivered"
    assert second[0]["cleanup"] == {
        "updated": True,
        "state": "delivered",
        "devices": 1,
        "sessions": 1,
        "apns": 1,
    }
    assert store.list_devices() == []
    status = store.account_deletion_status("owner")
    assert status["state"] == "delivered"
    assert status["attempts"] == 2
    assert status["completed_at"] == now[0]


def test_account_deletion_claim_recovers_after_worker_lease_expiry(tmp_path):
    now = [1_800_000_000]
    store = MobileDeviceStore(tmp_path / "mobile-auth.db", clock=lambda: now[0])
    store.begin_account_deletion("owner", "https://hermes.example|owner")

    first = store.claim_account_deletions(lease_seconds=30)
    assert len(first) == 1
    assert store.claim_account_deletions(lease_seconds=30) == []

    now[0] += 30
    recovered = MobileDeviceStore(
        store.db_path,
        clock=lambda: now[0],
    ).claim_account_deletions(lease_seconds=30)
    assert len(recovered) == 1
    assert recovered[0]["id"] == first[0]["id"]
    assert recovered[0]["lease_token"] != first[0]["lease_token"]
    assert recovered[0]["attempts"] == 2


@pytest.mark.parametrize(
    "state",
    [
        "pending",
        "retry",
        "delivering",
        "delivered",
        "no_recipients",
        "permanent_failure",
    ],
)
def test_account_deletion_tombstone_blocks_session_recreation_until_cleared(
    tmp_path,
    state,
):
    store = MobileDeviceStore(tmp_path / "mobile-auth.db")
    original = store.create_session(
        user_id="owner",
        device=_device("device-primary", "Owner iPhone"),
    )
    store.begin_account_deletion("owner", "https://hermes.example|owner")
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE mobile_account_deletion_outbox SET state=? WHERE user_id='owner'",
            (state,),
        )

    with pytest.raises(PermissionError, match="deletion tombstone"):
        store.create_session(
            user_id="owner",
            device=_device("device-primary", "Owner iPhone"),
        )

    assert store.verify_access(original.access_token, touch=False) is None
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM mobile_devices WHERE revoked_at IS NULL"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM mobile_sessions WHERE revoked_at IS NULL"
        ).fetchone()[0] == 0

    terminal = state in {"delivered", "no_recipients", "permanent_failure"}
    assert store.clear_completed_account_deletion("owner") is terminal
    if terminal:
        replacement = store.create_session(
            user_id="owner",
            device=_device("device-primary", "Owner iPhone"),
        )
        assert store.verify_access(replacement.access_token, touch=False) is not None


@pytest.mark.parametrize("first_writer", ["delete", "login"])
def test_account_deletion_and_session_creation_serialize_fail_closed(
    tmp_path,
    monkeypatch,
    first_writer,
):
    store = MobileDeviceStore(tmp_path / "mobile-auth.db")
    old_tokens = store.create_session(
        user_id="owner",
        device=_device("device-primary", "Owner iPhone"),
    )
    real_write_txn = mobile_device_store.write_txn
    attempted = {name: threading.Event() for name in ("delete", "login")}
    acquired = {name: threading.Event() for name in ("delete", "login")}
    release_first = threading.Event()

    @contextlib.contextmanager
    def ordered_write_txn(conn):
        actor = threading.current_thread().name
        attempted[actor].set()
        with real_write_txn(conn):
            acquired[actor].set()
            if actor == first_writer:
                assert release_first.wait(timeout=5)
            yield conn

    monkeypatch.setattr(mobile_device_store, "write_txn", ordered_write_txn)
    results = {}
    errors = {}

    def delete_account():
        try:
            results["delete"] = store.begin_account_deletion(
                "owner",
                "https://hermes.example|owner",
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors["delete"] = exc

    def create_login():
        try:
            results["login"] = store.create_session(
                user_id="owner",
                device=_device("device-primary", "Owner iPhone"),
            )
        except BaseException as exc:
            errors["login"] = exc

    workers = {
        "delete": threading.Thread(target=delete_account, name="delete"),
        "login": threading.Thread(target=create_login, name="login"),
    }
    second_writer = "login" if first_writer == "delete" else "delete"
    workers[first_writer].start()
    assert acquired[first_writer].wait(timeout=5)
    workers[second_writer].start()
    assert attempted[second_writer].wait(timeout=5)
    assert not acquired[second_writer].is_set()
    release_first.set()
    for worker in workers.values():
        worker.join(timeout=5)
        assert not worker.is_alive()

    assert "delete" not in errors
    assert store.account_deletion_status("owner")["state"] == "pending"
    if first_writer == "delete":
        assert isinstance(errors.get("login"), PermissionError)
    else:
        assert "login" not in errors
        assert store.verify_access(
            results["login"].access_token,
            touch=False,
        ) is None
    assert store.verify_access(old_tokens.access_token, touch=False) is None
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM mobile_devices WHERE revoked_at IS NULL"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM mobile_sessions WHERE revoked_at IS NULL"
        ).fetchone()[0] == 0


def test_account_deletion_progress_heartbeats_prevent_slow_batch_reclaim(
    tmp_path,
    monkeypatch,
):
    now = [1_800_000_000]
    bundle_id = "app.sunstone1029.fig1171"
    monkeypatch.setenv("HERMES_APNS_BUNDLE_ID", bundle_id)
    store = MobileDeviceStore(tmp_path / "mobile-auth.db", clock=lambda: now[0])
    for index in range(7):
        device_id = f"device-{index}"
        store.create_session(
            user_id="owner",
            device=_device(device_id, f"Owner iPhone {index}"),
        )
        store.register_apns(
            device_id=device_id,
            token=f"{index + 1:02x}" * 32,
            environment="production",
            bundle_id=bundle_id,
        )
    store.begin_account_deletion("owner", "https://hermes.example|owner")
    second_store = MobileDeviceStore(store.db_path, clock=lambda: now[0])
    sends = []
    second_claims = []

    def slow_sender(registration, _payload, _collapse_id):
        sends.append(registration["device_id"])
        now[0] += 11
        if len(sends) == 6:
            second_claims.extend(second_store.claim_account_deletions())
        return 200, ""

    outcomes = process_account_deletion_outbox(
        device_store=store,
        owner_id="owner",
        sender=slow_sender,
    )

    assert second_claims == []
    assert len(sends) == 7
    assert outcomes[0]["state"] == "delivered"
    assert outcomes[0]["cleanup"]["updated"] is True
    status = store.account_deletion_status("owner")
    assert status["state"] == "delivered"
    assert status["attempts"] == 1


def test_account_deletion_worker_stops_after_lease_is_reclaimed(
    tmp_path,
    monkeypatch,
):
    now = [1_800_000_000]
    bundle_id = "app.sunstone1029.fig1171"
    monkeypatch.setenv("HERMES_APNS_BUNDLE_ID", bundle_id)
    store = MobileDeviceStore(tmp_path / "mobile-auth.db", clock=lambda: now[0])
    for index in range(3):
        device_id = f"device-{index}"
        store.create_session(
            user_id="owner",
            device=_device(device_id, f"Owner iPhone {index}"),
        )
        store.register_apns(
            device_id=device_id,
            token=f"{index + 1:02x}" * 32,
            environment="production",
            bundle_id=bundle_id,
        )
    store.begin_account_deletion("owner", "https://hermes.example|owner")
    second_store = MobileDeviceStore(store.db_path, clock=lambda: now[0])
    sends = []
    reclaimed = []

    def stalled_sender(registration, _payload, _collapse_id):
        sends.append(registration["device_id"])
        now[0] += mobile_device_store.ACCOUNT_DELETION_LEASE_SECONDS + 1
        reclaimed.extend(second_store.claim_account_deletions())
        return 200, ""

    outcomes = process_account_deletion_outbox(
        device_store=store,
        owner_id="owner",
        sender=stalled_sender,
    )

    assert len(reclaimed) == 1
    assert len(sends) == 1
    assert outcomes[0]["state"] == "retry"
    assert outcomes[0]["error"] == "account deletion lease lost"
    assert outcomes[0]["cleanup"]["updated"] is False
    status = store.account_deletion_status("owner")
    assert status["state"] == "delivering"
    assert status["attempts"] == 2


def test_v6_migration_binds_devices_and_deletion_outbox_to_generation(tmp_path):
    db_path = tmp_path / "mobile-auth.db"
    generation = "acctgen_existing"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE mobile_devices (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '', os_version TEXT NOT NULL DEFAULT '',
                app_version TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL,
                revoked_at INTEGER, revoke_reason TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE mobile_sessions (
                id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES mobile_devices(id),
                user_id TEXT NOT NULL, account_generation TEXT NOT NULL DEFAULT '',
                access_token_hash TEXT NOT NULL UNIQUE,
                refresh_token_hash TEXT NOT NULL UNIQUE,
                access_expires_at INTEGER NOT NULL, refresh_expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL, revoked_at INTEGER,
                revoke_reason TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE mobile_account_deletion_outbox (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL UNIQUE,
                owner_scope TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                device_deliveries_json TEXT NOT NULL DEFAULT '{}',
                attempts INTEGER NOT NULL DEFAULT 0, available_at INTEGER NOT NULL,
                lease_token TEXT NOT NULL DEFAULT '', leased_until INTEGER NOT NULL DEFAULT 0,
                requested_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                completed_at INTEGER, last_error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX idx_mobile_account_deletion_due
                ON mobile_account_deletion_outbox(state, available_at, leased_until);
            CREATE TABLE mobile_account_generations (
                user_id TEXT PRIMARY KEY COLLATE NOCASE,
                generation TEXT NOT NULL UNIQUE,
                created_at INTEGER NOT NULL
            );
            INSERT INTO mobile_account_generations VALUES('owner','acctgen_existing',1);
            INSERT INTO mobile_devices VALUES(
                'device-old','owner','Old iPhone','','','',1,1,1,NULL,''
            );
            INSERT INTO mobile_sessions VALUES(
                'session-old','device-old','owner','acctgen_existing',
                'access-hash','refresh-hash',999,999,1,1,1,NULL,''
            );
            INSERT INTO mobile_account_deletion_outbox VALUES(
                'deletion-old','owner','owner-scope','pending','{}',0,1,'',0,1,1,NULL,''
            );
            PRAGMA user_version=6;
            """
        )

    store = MobileDeviceStore(db_path)
    with store.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert conn.execute(
            "SELECT account_generation FROM mobile_devices WHERE id='device-old'"
        ).fetchone()[0] == generation
        assert conn.execute(
            "SELECT account_generation FROM mobile_account_deletion_outbox "
            "WHERE id='deletion-old'"
        ).fetchone()[0] == generation
        conn.execute(
            "INSERT INTO mobile_account_deletion_outbox ("
            "id,user_id,account_generation,owner_scope,available_at,requested_at,updated_at"
            ") VALUES('deletion-new','owner','acctgen_new','owner-scope',1,1,1)"
        )


def test_v4_migration_creates_missing_account_generation_table(tmp_path):
    db_path = tmp_path / "mobile-auth.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE mobile_devices (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '', os_version TEXT NOT NULL DEFAULT '',
                app_version TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL,
                revoked_at INTEGER, revoke_reason TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE mobile_sessions (
                id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES mobile_devices(id),
                user_id TEXT NOT NULL, access_token_hash TEXT NOT NULL UNIQUE,
                refresh_token_hash TEXT NOT NULL UNIQUE,
                access_expires_at INTEGER NOT NULL, refresh_expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL, revoked_at INTEGER,
                revoke_reason TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE mobile_account_deletion_outbox (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL UNIQUE,
                owner_scope TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                device_deliveries_json TEXT NOT NULL DEFAULT '{}',
                attempts INTEGER NOT NULL DEFAULT 0, available_at INTEGER NOT NULL,
                lease_token TEXT NOT NULL DEFAULT '', leased_until INTEGER NOT NULL DEFAULT 0,
                requested_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                completed_at INTEGER, last_error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX idx_mobile_account_deletion_due
                ON mobile_account_deletion_outbox(state, available_at, leased_until);
            INSERT INTO mobile_devices VALUES(
                'device-old','owner','Old iPhone','','','',1,1,1,NULL,''
            );
            INSERT INTO mobile_sessions VALUES(
                'session-old','device-old','owner','access-hash','refresh-hash',
                999,999,1,1,1,NULL,''
            );
            INSERT INTO mobile_account_deletion_outbox VALUES(
                'deletion-old','owner','owner-scope','pending','{}',0,1,'',0,1,1,NULL,''
            );
            PRAGMA user_version=4;
            """
        )

    with MobileDeviceStore(db_path).connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert conn.execute(
            "SELECT generation FROM mobile_account_generations"
        ).fetchall() == []
        assert conn.execute(
            "SELECT account_generation FROM mobile_sessions WHERE id='session-old'"
        ).fetchone()[0] == ""
        assert conn.execute(
            "SELECT account_generation FROM mobile_devices WHERE id='device-old'"
        ).fetchone()[0] == "legacy"
        assert conn.execute(
            "SELECT account_generation FROM mobile_account_deletion_outbox "
            "WHERE id='deletion-old'"
        ).fetchone()[0] == "legacy"


def test_schema_initialization_is_idempotent_and_preserves_rows(tmp_path):
    db_path = tmp_path / "mobile-auth.db"
    store = MobileDeviceStore(db_path)
    tokens = store.create_session(
        user_id="owner",
        device=_device("device-primary", "Owner iPhone"),
    )

    for _ in range(3):
        with MobileDeviceStore(db_path).connection() as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] >= 1

    assert MobileDeviceStore(db_path).verify_access(
        tokens.access_token,
        touch=False,
    ) is not None


def test_newer_schema_is_rejected_without_overwriting_version(tmp_path):
    db_path = tmp_path / "mobile-auth.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA user_version=999")

    with pytest.raises(RuntimeError, match="newer Hermes version"):
        MobileDeviceStore(db_path).connect()

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 999
