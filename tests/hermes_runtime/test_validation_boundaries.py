"""Tests for the local MCP, side-effect, and deployment validation seams."""

from __future__ import annotations

import json
from pathlib import Path
import time
import zipfile

import pytest
from PIL import Image

from hermes_cli import rea_mcp_server as rea
from hermes_cli import visual_evidence_mcp as visual
from hermes_runtime.composability import (
    AllowlistViolation,
    ApprovalRecord,
    ApprovalRequired,
    DeploymentControlError,
    SideEffectClass,
    ValidationDeploymentController,
    make_validation_sandbox,
)


def test_visual_provider_is_root_confined_and_returns_real_evidence(tmp_path: Path) -> None:
    visual_root = tmp_path / "visual"
    visual_root.mkdir()
    first = visual_root / "before.png"
    second = visual_root / "after.png"
    image = Image.new("RGBA", (4, 3), (0, 0, 0, 255))
    image.putpixel((2, 1), (255, 10, 20, 255))
    image.save(first)
    changed = image.copy()
    changed.putpixel((2, 1), (255, 255, 20, 255))
    changed.save(second)
    visual.configure_root(visual_root)

    inspected = visual.inspect_image("before.png")
    assert inspected["result"]["width"] == 4
    assert len(inspected["result"]["sha256"]) == 64
    assert visual.pixel_probe("before.png", 2, 1)["result"]["rgba"] == [255, 10, 20, 255]
    assert visual.regions("before.png")["result"]["regions"][0]["bbox"] == [2, 1, 2, 1]
    diff = visual.pixel_diff("before.png", "after.png")
    assert diff["result"]["changed_pixels"] == 1
    assert diff["result"]["bbox"] == [2, 1, 2, 1]
    outside = visual.inspect_image(str(tmp_path / "missing.png"))
    assert outside["ok"] is False


def test_rea_provider_lists_without_extracting_and_confines_paths(tmp_path: Path) -> None:
    rea_root = tmp_path / "rea"
    rea_root.mkdir()
    archive = rea_root / "sample.ipa"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("Payload/Demo.app/Info.plist", b"plist")
        bundle.writestr("Payload/Demo.app/main", b"hello hermes")
    rea.configure_root(rea_root)

    inspected = rea.inspect_artifact("sample.ipa")
    assert inspected["result"]["magic"] == "zip"
    listing = rea.list_archive("sample.ipa")
    assert listing["result"]["entry_count"] == 2
    bundle = rea.analyze_bundle("sample.ipa")
    assert bundle["result"]["payload_apps"] == ["Payload/Demo.app/"]
    assert bundle["result"]["has_info_plist"] is True
    strings = rea.extract_strings("sample.ipa")
    assert strings["result"]["ok"] is True
    outside = rea.inspect_artifact(str(tmp_path / "outside.bin"))
    assert outside["ok"] is False


def test_irreversible_sandbox_requires_exact_single_use_approval(tmp_path: Path) -> None:
    sandbox = make_validation_sandbox(tmp_path / "effects")
    with pytest.raises(ApprovalRequired):
        sandbox.execute_local(
            operation_id="validation.append_irreversible_record",
            target="irreversible-ledger.jsonl",
            classification=SideEffectClass.IRREVERSIBLE,
            idempotency_key="attempt-1",
        )
    approval = ApprovalRecord(
        approval_id="approval-1",
        operation_id="validation.append_irreversible_record",
        target="irreversible-ledger.jsonl",
        approver="local-validation-operator",
        subject="hermes-validation-irreversible-sandbox",
        expires_at=time.time() + 60,
    )
    receipt = sandbox.execute_local(
        operation_id="validation.append_irreversible_record",
        target="irreversible-ledger.jsonl",
        classification=SideEffectClass.IRREVERSIBLE,
        idempotency_key="attempt-1",
        approval=approval,
        payload={"test": True},
    )
    assert receipt.status == "committed_local_sandbox"
    assert sandbox.execute_local(
        operation_id="validation.append_irreversible_record",
        target="irreversible-ledger.jsonl",
        classification=SideEffectClass.IRREVERSIBLE,
        idempotency_key="attempt-1",
        approval=approval,
    ) == receipt
    recovered = make_validation_sandbox(tmp_path / "effects")
    assert recovered.execute_local(
        operation_id="validation.append_irreversible_record",
        target="irreversible-ledger.jsonl",
        classification=SideEffectClass.IRREVERSIBLE,
        idempotency_key="attempt-1",
    ) == receipt
    with pytest.raises(AllowlistViolation):
        sandbox.execute_local(
            operation_id="validation.append_irreversible_record",
            target="other-ledger.jsonl",
            classification=SideEffectClass.IRREVERSIBLE,
            idempotency_key="attempt-2",
            approval=approval,
        )
    assert len((tmp_path / "effects" / "irreversible-ledger.jsonl").read_text().splitlines()) == 1
    assert len((tmp_path / "effects" / "side-effects.jsonl").read_text().splitlines()) == 1


def test_validation_deployment_health_gates_and_owner_gates_rollback(tmp_path: Path) -> None:
    controller = ValidationDeploymentController(
        tmp_path / "deployment",
        rollback_owner="local-validation-operator",
        drain_deadline_seconds=5,
    )
    digest_one = controller.stage_release("v1", {"health.txt": "healthy"})
    digest_two = controller.stage_release("v2", {"health.txt": "healthy-v2"})
    first = controller.deploy("v1", health_check=lambda path: path.joinpath("health.txt").exists())
    assert first.committed is True and first.release_digest == digest_one
    rejected = controller.deploy("v2", health_check=lambda _path: False)
    assert rejected.committed is False
    assert controller.status()["current_version"] == "v1"
    second = controller.deploy("v2", health_check=lambda path: path.joinpath("health.txt").read_text() == "healthy-v2")
    assert second.committed is True and second.release_digest == digest_two
    assert controller.begin_drain()["lifecycle"] == "draining"
    with pytest.raises(DeploymentControlError):
        controller.rollback(owner="wrong-owner")
    rolled = controller.rollback(owner="local-validation-operator")
    assert rolled.rolled_back is True
    assert controller.status()["current_version"] == "v1"
    assert controller.status()["drain_deadline"] is None
