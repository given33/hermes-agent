from __future__ import annotations

import pytest
from fastapi import HTTPException

from hermes_services.tool_output_artifacts import EncryptedToolArtifactStore
from plugins.collaboration.dashboard import plugin_api


def test_authenticated_artifact_endpoints_are_generation_scoped(tmp_path, monkeypatch):
    store = EncryptedToolArtifactStore(tmp_path)
    old = store.put(
        owner_id="alice",
        account_generation="generation-1",
        conversation_id="conversation-1",
        turn_id="turn-1",
        tool_call_id="tool-old",
        tool_name="terminal",
        content="old secret",
    )
    new = store.put(
        owner_id="alice",
        account_generation="generation-2",
        conversation_id="conversation-2",
        turn_id="turn-2",
        tool_call_id="tool-new",
        tool_name="terminal",
        content="new secret",
    )
    monkeypatch.setattr(plugin_api, "get_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr(plugin_api, "owner_id_from_request", lambda _request: "alice")
    monkeypatch.setattr(
        plugin_api, "_account_generation_for_owner", lambda _owner: "generation-2"
    )

    listing = plugin_api.list_tool_output_artifacts(object())
    response = plugin_api.download_tool_output_artifact(new["id"], object())

    assert [item["id"] for item in listing["artifacts"]] == [new["id"]]
    assert listing["total"] == 1
    assert listing["limit"] == 100
    assert listing["offset"] == 0
    assert response.body == b"new secret"
    with pytest.raises(HTTPException) as exc:
        plugin_api.download_tool_output_artifact(old["id"], object())
    assert exc.value.status_code == 404


def test_artifact_listing_combines_search_dates_and_filtered_total(tmp_path, monkeypatch):
    store = EncryptedToolArtifactStore(tmp_path)
    now = [1_800_000_000]
    monkeypatch.setattr(
        "hermes_services.tool_output_artifacts.time.time", lambda: now[0]
    )
    artifacts = []
    for index, (created_at, tool_name) in enumerate(
        (
            (1_800_000_000, "terminal"),
            (1_800_000_060, "Deploy Runner"),
            (1_800_000_120, "deploy verifier"),
            (1_800_000_180, "Deploy cleanup"),
        )
    ):
        now[0] = created_at
        artifacts.append(
            store.put(
                owner_id="alice",
                account_generation="generation-2",
                conversation_id=f"conversation-{index}",
                turn_id=f"turn-{index}",
                tool_call_id=f"tool-{index}",
                tool_name=tool_name,
                content=f"output-{index}",
            )
        )
    now[0] = 1_800_000_200
    monkeypatch.setattr(plugin_api, "get_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr(plugin_api, "owner_id_from_request", lambda _request: "alice")
    monkeypatch.setattr(
        plugin_api, "_account_generation_for_owner", lambda _owner: "generation-2"
    )

    listing = plugin_api.list_tool_output_artifacts(
        object(),
        q="DEPLOY",
        date_from="1800000060",
        date_to="1800000120",
        filter_contract="account-files-v1",
        limit=1,
    )

    assert [item["id"] for item in listing["artifacts"]] == [artifacts[2]["id"]]
    assert listing["total"] == 2
    assert listing["limit"] == 1
    assert listing["filter_contract"] == "account-files-v1"


def test_artifact_listing_rejects_unknown_filter_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_api, "get_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr(plugin_api, "owner_id_from_request", lambda _request: "alice")

    with pytest.raises(HTTPException) as exc:
        plugin_api.list_tool_output_artifacts(
            object(), filter_contract="account-files-v2"
        )

    assert exc.value.status_code == 422


def test_artifact_listing_rejects_reversed_dates(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_api, "get_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr(plugin_api, "owner_id_from_request", lambda _request: "alice")
    monkeypatch.setattr(
        plugin_api, "_account_generation_for_owner", lambda _owner: "generation-2"
    )

    with pytest.raises(HTTPException) as exc:
        plugin_api.list_tool_output_artifacts(
            object(), date_from="1800000120", date_to="1800000060"
        )

    assert exc.value.status_code == 422


def test_authenticated_delete_only_removes_current_generation(tmp_path, monkeypatch):
    store = EncryptedToolArtifactStore(tmp_path)
    old = store.put(
        owner_id="alice",
        account_generation="generation-1",
        conversation_id="conversation-1",
        turn_id="turn-1",
        tool_call_id="tool-old",
        tool_name="terminal",
        content="old secret",
    )
    current = store.put(
        owner_id="alice",
        account_generation="generation-2",
        conversation_id="conversation-2",
        turn_id="turn-2",
        tool_call_id="tool-current",
        tool_name="terminal",
        content="current secret",
    )
    monkeypatch.setattr(plugin_api, "get_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr(plugin_api, "owner_id_from_request", lambda _request: "alice")
    monkeypatch.setattr(
        plugin_api, "_account_generation_for_owner", lambda _owner: "generation-2"
    )

    result = plugin_api.delete_tool_output_artifact(current["id"], object())

    assert result == {"id": current["id"], "ok": True}
    assert store.read(
        "alice", old["id"], account_generation="generation-1"
    ) == b"old secret"
    with pytest.raises(HTTPException) as exc:
        plugin_api.delete_tool_output_artifact(old["id"], object())
    assert exc.value.status_code == 404
