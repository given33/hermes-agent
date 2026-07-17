"""Durable mobile device session, rotation, revocation, and APNs contracts."""
from __future__ import annotations

import sqlite3

import pytest

from hermes_cli.dashboard_auth.mobile_device_store import (
    MobileDeviceInfo,
    MobileDeviceStore,
    OwnerMobileTokenProvider,
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
