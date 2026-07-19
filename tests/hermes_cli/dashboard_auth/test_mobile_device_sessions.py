"""Durable mobile device session, rotation, revocation, and APNs contracts."""
from __future__ import annotations

import contextlib
import sqlite3
import threading

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

    provider = OwnerMobileTokenProvider(lambda: MobileDeviceStore(db_path))
    principal = provider.verify_token(token=tokens.access_token)
    assert principal is not None
    assert principal.principal == "owner"
    assert principal.provider == "owner-mobile"


def test_refresh_replay_revokes_the_rotated_token_family(tmp_path):
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
    assert store.verify_access(rotated.access_token, touch=False) is None
    assert store.rotate_refresh(rotated.refresh_token) is None


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
