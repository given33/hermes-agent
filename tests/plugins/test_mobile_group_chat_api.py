"""Owner-mobile facade contracts for official durable Group Chat."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from fastapi import HTTPException

from gateway import hosted_rooms
from gateway.hosted_room_execution_policy import execution_policy_mapping
from gateway.hosted_room_peer import catalog_mapping


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "collaboration"
    / "dashboard"
    / "plugin_api.py"
)


def _module():
    name = f"mobile_group_chat_api_{id(object())}"
    spec = importlib.util.spec_from_file_location(
        name, MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_mobile_group_create_delegates_owner_binding_to_official_service(
    monkeypatch,
    tmp_path,
):
    module = _module()
    calls = []

    class Service:
        db_path = tmp_path / "state.db"

        def create_room(self, **kwargs):
            calls.append(("create", kwargs))
            return {"room_id": kwargs["room_id"], "name": kwargs["name"]}

    monkeypatch.setattr(
        module,
        "_mobile_group_chat_identity",
        lambda _request: ("owner-a", "generation-a"),
    )
    monkeypatch.setattr(module, "_mobile_group_chat_service", lambda: Service())
    result = module.mobile_create_group_chat_room(
        module.MobileGroupChatCreateBody(
            idempotency_key="create-room-001",
            name="Release discussion",
            members=[
                {"member_id": "default", "profile": "default", "handle": "hermes"},
                {"member_id": "reviewer", "profile": "reviewer", "handle": "review"},
            ],
        ),
        object(),
    )

    assert result["room"]["room_id"].startswith("mobile-group-")
    assert calls[0][0] == "create"
    assert calls[0][1]["members"] == [
        {"member_id": "default", "profile": "default", "handle": "hermes"},
        {"member_id": "reviewer", "profile": "reviewer", "handle": "review"},
    ]
    assert calls[0][1]["owner_id"] == "owner-a"
    assert calls[0][1]["account_generation"] == "generation-a"
    assert len(calls) == 1


def test_mobile_group_create_requires_the_official_minimum_two_members():
    module = _module()

    with pytest.raises(ValidationError):
        module.MobileGroupChatCreateBody(
            idempotency_key="create-room-001",
            name="Release discussion",
            members=[{"member_id": "default", "profile": "default", "handle": "hermes"}],
        )


def test_mobile_group_create_rejects_cross_gateway_member_fields():
    module = _module()

    with pytest.raises(HTTPException, match="unsupported mobile fields"):
        module._mobile_group_chat_local_members([
            {
                "member_id": "peer",
                "profile": "peer-profile",
                "handle": "peer",
                "target": {"kind": "peer"},
            },
            {"member_id": "default", "profile": "default", "handle": "hermes"},
        ])


def test_mobile_group_gateway_catalog_is_secret_free_and_lists_connector_only_nodes(
    monkeypatch,
    tmp_path,
):
    module = _module()

    class Service:
        db_path = tmp_path / "state.db"

        def local_profiles(self):
            return ("default", "reviewer")

    monkeypatch.setattr(module, "_mobile_group_chat_identity", lambda _request: ("owner-a", "generation-a"))
    monkeypatch.setattr(module, "_mobile_group_chat_service", lambda: Service())
    monkeypatch.setattr(
        module,
        "_mobile_group_chat_peer_entries",
        lambda: {
            "hk": {
                "gateway_id": "hk",
                "label": "Hong Kong",
                "profiles": ("default",),
                "profiles_declared": False,
                "ready": True,
                "reason": "",
                "peer": {"url": "https://hk.example.test"},
                "url": "https://hk.example.test",
                "api_key": "secret-that-must-not-leak",
            }
        },
    )
    monkeypatch.setattr(hosted_rooms, "local_authority_gateway_id", lambda: "install:home")

    result = module.mobile_group_chat_gateways(object())
    encoded = json.dumps(result, ensure_ascii=False)

    assert {gateway["gateway_id"] for gateway in result["gateways"]} == {"local", "hk"}
    assert result["gateways"][1]["room_link_ready"] is True
    assert {node["node_id"] for node in result["execution_nodes"]} == {"dbb3", "wsl", "hk"}
    assert "https://hk.example.test" not in encoded
    assert "secret-that-must-not-leak" not in encoded
    assert "api_key" not in encoded


def test_mobile_group_create_admits_peer_via_official_roomlink_without_returning_credentials(
    monkeypatch,
    tmp_path,
):
    module = _module()
    policy = execution_policy_mapping(target_profile="default", config={})
    catalog = catalog_mapping(
        installation_id="install:peer",
        protocol_versions=(2,),
        link_modes=("direct",),
        persistent_process=True,
        endpoint={
            "available": True,
            "url": "https://peer-room.example.test",
            "transport_security": "tls",
        },
        target_profile="default",
        execution_policy=policy,
    )
    calls = []

    class FakeClient:
        instances = []

        def __init__(self, *, base_url, api_key, receipt_db_path=None):
            self.base_url = base_url
            self.api_key = api_key
            self.receipt_db_path = receipt_db_path
            self.__class__.instances.append(self)

        def issue_invitation(self, **kwargs):
            calls.append(("invite", kwargs, self.api_key))
            self.invitation_kwargs = kwargs
            return {"grant": "grant-secret", "catalog": catalog}

        def probe(self, *, grant):
            calls.append(("probe", grant, self.base_url))
            invitation_kwargs = self.__class__.instances[0].invitation_kwargs
            return {
                "catalog": catalog,
                "room_id": invitation_kwargs["room_id"],
                "home_install_id": "install:home",
                "authority_gateway_id": "install:home",
                "authority_epoch": 1,
                "member_id": invitation_kwargs["member_id"],
                "target_profile": invitation_kwargs["target_profile"] if "target_profile" in invitation_kwargs else "default",
            }

        def revoke_grant(self, *, grant):
            calls.append(("revoke", grant))
            return {"revoked": True}

    class Service:
        db_path = tmp_path / "state.db"

        def local_profiles(self):
            return ("default",)

        def create_room(self, **kwargs):
            calls.append(("create", kwargs))
            return {"room_id": kwargs["room_id"], "name": kwargs["name"], "members": kwargs["members"]}

        def register_peer_route(self, **kwargs):
            calls.append(("register", kwargs))

    monkeypatch.setattr(module, "_mobile_group_chat_identity", lambda _request: ("owner-a", "generation-a"))
    monkeypatch.setattr(module, "_mobile_group_chat_service", lambda: Service())
    monkeypatch.setattr(hosted_rooms, "local_authority_gateway_id", lambda: "install:home")
    monkeypatch.setattr(
        hosted_rooms,
        "room_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(hosted_rooms.RoomNotFoundError("missing")),
    )
    monkeypatch.setattr(
        module,
        "_mobile_group_chat_peer_entries",
        lambda: {
            "hk": {
                "gateway_id": "hk",
                "label": "Hong Kong",
                "profiles": ("default",),
                "profiles_declared": True,
                "ready": True,
                "reason": "",
                "peer": {"url": "https://hk-api.example.test"},
                "url": "https://hk-api.example.test",
                "api_key": "server-side-peer-key-123456",
            }
        },
    )
    monkeypatch.setattr(
        "tui_gateway.hosted_room_peer_http.PeerRunsHTTPClient",
        FakeClient,
    )

    result = module.mobile_create_group_chat_room(
        module.MobileGroupChatCreateBody(
            idempotency_key="create-peer-room-001",
            name="Fleet discussion",
            members=[
                {"member_id": "local-default", "profile": "default", "handle": "local"},
                {
                    "member_id": "hk-default",
                    "profile": "default",
                    "handle": "hk",
                    "gateway_id": "hk",
                },
            ],
        ),
        object(),
    )

    room = result["room"]
    assert room["members"][1]["target"] == {
        "kind": "peer",
        "peer_id": "hk",
        "installation_id": "install:peer",
        "profile": "default",
        "capability_digest": catalog["catalog_digest"],
    }
    assert calls[0][0] == "invite"
    assert calls[0][2] == "server-side-peer-key-123456"
    assert any(call[0] == "register" for call in calls)
    assert result["room"].get("grant") is None
    assert "server-side-peer-key-123456" not in json.dumps(result)
    assert "grant-secret" not in json.dumps(result)


def test_mobile_group_create_does_not_disband_idempotent_room_on_route_failure(
    monkeypatch,
    tmp_path,
):
    """A retry must not roll back a room created by another request."""

    module = _module()
    calls = []

    class Service:
        db_path = tmp_path / "state.db"

        def create_room(self, **kwargs):
            calls.append(("create", kwargs))
            return {
                "room_id": kwargs["room_id"],
                "name": kwargs["name"],
                "members": kwargs["members"],
                "idempotent": True,
            }

        def register_peer_route(self, **kwargs):
            calls.append(("register", kwargs))
            raise RuntimeError("route registration failed")

    monkeypatch.setattr(module, "_mobile_group_chat_identity", lambda _request: ("owner-a", "generation-a"))
    monkeypatch.setattr(module, "_mobile_group_chat_service", lambda: Service())
    monkeypatch.setattr(
        hosted_rooms,
        "room_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(hosted_rooms.RoomNotFoundError("missing")),
    )
    monkeypatch.setattr(
        module,
        "_mobile_group_chat_prepare_peer_routes",
        lambda _service, *, room_id, records: (
            [
                {
                    "member_id": "local",
                    "profile": "default",
                    "handle": "local",
                    "target": {"kind": "local", "profile": "default"},
                },
                {
                    "member_id": "hk",
                    "profile": "default",
                    "handle": "hk",
                    "target": {
                        "kind": "peer",
                        "peer_id": "hk",
                        "profile": "default",
                    },
                },
            ],
            [{
                "member_id": "hk",
                "route": object(),
                "client": object(),
                "target_url": "https://peer.example.test",
                "catalog": object(),
                "grant": "grant-secret",
            }],
        ),
    )
    monkeypatch.setattr(
        module,
        "_disband_mobile_group_chat_room",
        lambda *_args, **kwargs: calls.append(("disband", kwargs)),
    )
    monkeypatch.setattr(
        module,
        "_mobile_group_chat_revoke_pending_peer_routes",
        lambda setups: calls.append(("revoke", setups)),
    )

    with pytest.raises(HTTPException) as exc_info:
        module.mobile_create_group_chat_room(
            module.MobileGroupChatCreateBody(
                idempotency_key="create-peer-room-retry-001",
                name="Fleet discussion",
                members=[
                    {"member_id": "local", "profile": "default", "handle": "local"},
                    {
                        "member_id": "hk",
                        "profile": "default",
                        "handle": "hk",
                        "gateway_id": "hk",
                    },
                ],
            ),
            object(),
        )

    assert exc_info.value.status_code == 503
    assert [item[0] for item in calls] == ["create", "register"]


def test_mobile_group_message_uses_official_idempotent_send(monkeypatch, tmp_path):
    module = _module()
    calls = []

    class Service:
        db_path = tmp_path / "state.db"

        def send(self, **kwargs):
            calls.append(kwargs)
            return {"event_id": kwargs["event_id"], "payload": kwargs["payload"]}

    service = Service()
    monkeypatch.setattr(
        module,
        "_mobile_group_chat_owned_room",
        lambda _request, _room_id: (service, "owner-a", "generation-a"),
    )

    result = module.mobile_group_chat_send_message(
        "mobile-group-1",
        module.MobileGroupChatMessageBody(
            idempotency_key="send-message-001",
            text="@hermes inspect the build",
            thread_id="thread-1",
        ),
        object(),
    )

    assert result["accepted"] is True
    assert result["driver_started"] is True
    assert calls == [{
        "room_id": "mobile-group-1",
        "event_id": hosted_rooms.user_event_id("send-message-001"),
        "payload": {"text": "@hermes inspect the build", "thread_id": "thread-1"},
    }]


def test_account_deletion_keeps_binding_until_official_disband(monkeypatch, tmp_path):
    module = _module()
    calls = []
    db_path = tmp_path / "state.db"

    class Service:
        def __init__(self):
            self.db_path = db_path

        def stop_room(self, room_id, **kwargs):
            calls.append(("stop", room_id, kwargs))
            return 1

        def revoke_room_routes(self, room_id):
            calls.append(("revoke", room_id))
            return 0

    monkeypatch.setattr(hosted_rooms, "default_db_path", lambda: db_path)
    monkeypatch.setattr(hosted_rooms, "list_mobile_room_ids", lambda *_args, **_kwargs: ["room-1"])
    monkeypatch.setattr(
        hosted_rooms,
        "room_state",
        lambda *_args, **_kwargs: {
            "authority_gateway_id": "install:gateway-a",
            "authority_epoch": 2,
            "disbanded_at": None,
        },
    )
    monkeypatch.setattr(
        hosted_rooms,
        "disband_room",
        lambda _db_path, **kwargs: calls.append(("disband", kwargs)),
    )
    monkeypatch.setattr(
        hosted_rooms,
        "remove_mobile_room_owner",
        lambda _db_path, **kwargs: calls.append(("unbind", kwargs)) or True,
    )
    service = Service()
    monkeypatch.setattr(module, "_mobile_group_chat_service", lambda: service)

    assert module._delete_mobile_group_chat_account_data(
        "owner-a", account_generation="generation-a"
    ) == 1
    assert [item[0] for item in calls] == ["stop", "revoke", "disband", "unbind"]
    assert calls[-1][1]["owner_id"] == "owner-a"
    assert calls[-1][1]["account_generation"] == "generation-a"


def test_mobile_group_delete_uses_the_official_disband_sequence(monkeypatch, tmp_path):
    module = _module()
    calls = []

    class Service:
        db_path = tmp_path / "state.db"

        def stop_room(self, room_id, **kwargs):
            calls.append(("stop", room_id, kwargs))
            return 1

        def revoke_room_routes(self, room_id):
            calls.append(("revoke", room_id))
            return 0

    service = Service()
    monkeypatch.setattr(
        module,
        "_mobile_group_chat_owned_room",
        lambda _request, _room_id: (service, "owner-a", "generation-a"),
    )
    monkeypatch.setattr(
        hosted_rooms,
        "room_state",
        lambda *_args, **_kwargs: {
            "room_id": "room-1",
            "authority_gateway_id": "install:gateway-a",
            "authority_epoch": 2,
            "disbanded_at": None,
        },
    )
    monkeypatch.setattr(
        hosted_rooms,
        "disband_room",
        lambda _db_path, **kwargs: calls.append(("disband", kwargs)) or {"ok": True},
    )
    monkeypatch.setattr(
        hosted_rooms,
        "remove_mobile_room_owner",
        lambda _db_path, **kwargs: calls.append(("unbind", kwargs)) or True,
    )

    result = module.mobile_group_chat_delete("room-1", object())

    assert result["disbanded"] is True
    assert [item[0] for item in calls] == ["stop", "revoke", "disband", "unbind"]
    assert calls[0][2]["require_acknowledged"] is True


def test_mobile_group_facade_never_exposes_api_server_or_room_grant_credentials():
    source = MODULE_PATH.read_text(encoding="utf-8")
    start = source.index("class MobileGroupChatCreateBody")
    end = source.index("def _mobile_profile_home", start)
    facade = source[start:end]

    assert "API_SERVER_KEY" not in facade
    assert "HermesRoom" not in facade
    assert "/v1/runs" not in facade
