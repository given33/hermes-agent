from __future__ import annotations

import time

from hermes_cli import managed_installations


def _complete(
    db_path,
    *,
    request_id: str,
    identifier: str,
    detail: dict,
    owner_id: str | None = None,
    account_generation: str | None = None,
):
    if detail:
        detail = {
            "proof_schema": 1,
            "proof_source": "local_filesystem",
            **detail,
        }
    operation = managed_installations.create_managed_installation(
        kind="skill",
        identifier=identifier,
        request_id=request_id,
        targets=["server"],
        db_path=db_path,
        owner_id=owner_id,
        account_generation=account_generation,
    )
    claimed = managed_installations._claim_target(
        db_path,
        now=time.time(),
        lease_seconds=30,
    )
    assert claimed is not None
    assert claimed["id"] == operation["id"]
    assert managed_installations._finish_target(
        db_path,
        claimed,
        state="completed",
        detail=detail,
    ) is True
    managed_installations._release_execution_fence(claimed)
    return operation


def test_completed_installation_publishes_provenance_and_cursor_event(tmp_path):
    db = tmp_path / "managed-installations.db"
    operation = _complete(
        db,
        request_id="install-one",
        identifier="github.com/example/useful-skill",
        detail={"commit": "a" * 40, "sha256": "f" * 64},
    )

    catalog = managed_installations.list_managed_resources(db_path=db)

    assert catalog["cursor"] == 1
    assert catalog["has_more"] is False
    assert len(catalog["events"]) == 1
    resource = catalog["resources"][0]
    assert resource["operation_id"] == operation["id"]
    assert resource["kind"] == "skill"
    assert resource["target_nodes"] == ["server"]
    assert resource["loaded_nodes"] == ["server"]
    assert resource["resolved_commit_or_version"] == "a" * 40
    assert resource["content_hash"] == "f" * 64
    assert resource["health"] == "healthy"


def test_name_collision_reports_deterministic_winner_and_loser(tmp_path):
    db = tmp_path / "managed-installations.db"
    _complete(
        db,
        request_id="install-one",
        identifier="My-Skill",
        detail={"version": "1.0.0"},
    )
    _complete(
        db,
        request_id="install-two",
        identifier="my_skill",
        detail={"version": "2.0.0"},
    )

    catalog = managed_installations.list_managed_resources(db_path=db)

    assert len(catalog["resources"]) == 1
    assert len(catalog["diagnostics"]) == 1
    diagnostic = catalog["diagnostics"][0]
    assert diagnostic["code"] == "resource_name_collision"
    assert diagnostic["winner"]["resource_id"] != diagnostic["loser"]["resource_id"]


def test_catalog_event_cursor_supports_incremental_refresh(tmp_path):
    db = tmp_path / "managed-installations.db"
    _complete(
        db,
        request_id="install-one",
        identifier="one-skill",
        detail={},
    )
    first = managed_installations.list_managed_resources(db_path=db)
    _complete(
        db,
        request_id="install-two",
        identifier="two-skill",
        detail={},
    )

    incremental = managed_installations.list_managed_resources(
        db_path=db,
        since_cursor=first["cursor"],
    )

    assert [event["cursor"] for event in incremental["events"]] == [2]
    assert incremental["cursor"] == 2


def test_catalog_future_cursor_requests_an_authoritative_reset(tmp_path):
    db = tmp_path / "managed-installations.db"

    catalog = managed_installations.list_managed_resources(
        db_path=db,
        since_cursor=99,
    )

    assert catalog["resources"] == []
    assert catalog["events"] == []
    assert catalog["cursor"] == 0
    assert catalog["reset_cursor"] is True
    assert catalog["reset_reason"] == "future_cursor"
    assert catalog["has_more"] is False


def test_completed_resource_without_immutable_proof_is_not_reported_healthy(tmp_path):
    db = tmp_path / "managed-installations.db"
    _complete(
        db,
        request_id="install-unverified",
        identifier="unverified-skill",
        detail={},
    )

    resource = managed_installations.list_managed_resources(db_path=db)["resources"][0]

    assert resource["health"] == "degraded"
    assert resource["enabled"] is False
    assert resource["trust_state"] == "pending"
    assert resource["conflicts"][0]["code"] == "resource_proof_missing"


def test_multi_node_resource_proof_mismatch_fails_closed(tmp_path):
    db = tmp_path / "managed-installations.db"
    managed_installations.create_managed_installation(
        kind="project",
        identifier="https://github.com/example/project.git",
        project_name="project",
        request_id="mismatched-project",
        targets=["dbb3", "wsl"],
        db_path=db,
    )
    first = managed_installations._claim_target(db, now=time.time(), lease_seconds=30)
    assert first and first["node_id"] == "dbb3"
    assert managed_installations._finish_target(
        db, first, state="completed", detail={
            "head": "a" * 40,
            "proof_schema": 1,
            "proof_source": "local_filesystem",
        }
    )
    managed_installations._release_execution_fence(first)
    second = managed_installations._claim_target(db, now=time.time(), lease_seconds=30)
    assert second and second["node_id"] == "wsl"
    assert managed_installations._finish_target(
        db, second, state="completed", detail={
            "head": "b" * 40,
            "proof_schema": 1,
            "proof_source": "local_filesystem",
        }
    )
    managed_installations._release_execution_fence(second)

    resource = managed_installations.list_managed_resources(db_path=db)["resources"][0]

    assert resource["health"] == "failed"
    assert resource["enabled"] is False
    assert resource["trust_state"] == "blocked"
    assert resource["resolved_commit_or_version"] == ""
    assert resource["conflicts"][0]["code"] == "resource_proof_mismatch"


def test_catalog_and_events_are_isolated_by_owner_generation(tmp_path):
    db = tmp_path / "managed-installations.db"
    _complete(
        db,
        request_id="same-request",
        identifier="same-skill",
        detail={"version": "1.0.1"},
        owner_id="alice",
        account_generation="alice-gen-1",
    )
    _complete(
        db,
        request_id="same-request",
        identifier="same-skill",
        detail={"version": "1.0.2"},
        owner_id="bob",
        account_generation="bob-gen-1",
    )

    alice = managed_installations.list_managed_resources(
        db_path=db, owner_id="alice", account_generation="alice-gen-1"
    )
    bob = managed_installations.list_managed_resources(
        db_path=db, owner_id="bob", account_generation="bob-gen-1"
    )

    assert alice["account_generation"] == "alice-gen-1"
    assert bob["account_generation"] == "bob-gen-1"
    assert [item["resolved_commit_or_version"] for item in alice["resources"]] == ["1.0.1"]
    assert [item["resolved_commit_or_version"] for item in bob["resources"]] == ["1.0.2"]
    assert alice["resources"][0]["resource_id"] != bob["resources"][0]["resource_id"]
    assert len(alice["events"]) == len(bob["events"]) == 1


def test_account_deletion_removes_only_that_owners_operations_catalog_and_events(tmp_path):
    db = tmp_path / "managed-installations.db"
    _complete(
        db,
        request_id="alice-install",
        identifier="alice-skill",
        detail={},
        owner_id="alice",
        account_generation="alice-gen-1",
    )
    _complete(
        db,
        request_id="bob-install",
        identifier="bob-skill",
        detail={},
        owner_id="bob",
        account_generation="bob-gen-1",
    )

    deleted = managed_installations.delete_owner_managed_resources(
        "alice",
        account_generation="alice-gen-1",
        include_known_generations=True,
        db_path=db,
    )

    assert deleted == {"resources": 1, "events": 1, "operations": 1}
    assert managed_installations.list_managed_resources(
        db_path=db, owner_id="alice", account_generation="alice-gen-1"
    )["resources"] == []
    assert len(managed_installations.list_managed_resources(
        db_path=db, owner_id="bob", account_generation="bob-gen-1"
    )["resources"]) == 1


def test_one_node_retry_publishes_only_after_all_targets_complete(tmp_path):
    db = tmp_path / "managed-installations.db"
    operation = managed_installations.create_managed_installation(
        kind="skill",
        identifier="retry-skill",
        request_id="retry-install",
        targets=["server", "dbb3"],
        db_path=db,
        owner_id="alice",
        account_generation="alice-gen-1",
    )
    first = managed_installations._claim_target(db, now=time.time(), lease_seconds=30)
    assert first and first["node_id"] == "server"
    assert managed_installations._finish_target(db, first, state="retry", error="temporary")
    managed_installations._release_execution_fence(first)
    second = managed_installations._claim_target(db, now=time.time() + 10_000, lease_seconds=30)
    assert second and second["node_id"] == "dbb3"
    assert managed_installations._finish_target(db, second, state="completed", detail={
        "version": "1",
        "proof_schema": 1,
        "proof_source": "local_filesystem",
    })
    managed_installations._release_execution_fence(second)
    third = managed_installations._claim_target(db, now=time.time() + 10_000, lease_seconds=30)
    assert third and third["node_id"] == "server"
    assert managed_installations._finish_target(db, third, state="completed", detail={
        "version": "1",
        "proof_schema": 1,
        "proof_source": "local_filesystem",
    })
    managed_installations._release_execution_fence(third)

    catalog = managed_installations.list_managed_resources(
        db_path=db, owner_id="alice", account_generation="alice-gen-1"
    )
    assert operation["id"] == catalog["resources"][0]["operation_id"]
    assert catalog["resources"][0]["loaded_nodes"] == ["server", "dbb3"]
    assert len(catalog["events"]) == 1


def test_mutable_or_malformed_proof_is_blocked_even_when_nodes_agree(tmp_path):
    db = tmp_path / "managed-installations.db"
    operation = managed_installations.create_managed_installation(
        kind="project",
        identifier="https://github.com/example/project.git",
        project_name="project",
        request_id="invalid-proof",
        targets=["dbb3", "wsl"],
        db_path=db,
    )
    for node_id in ("dbb3", "wsl"):
        claimed = managed_installations._claim_target(
            db, now=time.time() + 10_000, lease_seconds=30
        )
        assert claimed and claimed["node_id"] == node_id
        assert managed_installations._finish_target(
            db,
            claimed,
            state="completed",
            detail={
                "proof_schema": 1,
                "proof_source": "local_filesystem",
                "resolved_commit": "main",
                "sha256": "not-a-sha256",
            },
        )
        managed_installations._release_execution_fence(claimed)

    resource = managed_installations.list_managed_resources(db_path=db)["resources"][0]
    assert resource["operation_id"] == operation["id"]
    assert resource["health"] == "failed"
    assert resource["trust_state"] == "blocked"
    assert resource["enabled"] is False
    assert resource["conflicts"][0]["code"] == "resource_proof_invalid"


def test_malformed_proof_schema_is_rejected_without_breaking_publication():
    proof, reason = managed_installations._validated_managed_resource_proof({
        "proof_schema": "broken",
        "proof_source": "local_filesystem",
        "resolved_commit": "a" * 40,
    })

    assert proof == ("", "")
    assert "proof source" in reason
