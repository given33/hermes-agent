"""Explicit boundary for recoverable and irreversible operations.

EffectScope disposers clean up owned runtime resources.  They cannot undo an
email, payment, remote deletion, or deployment that already happened.  This
module models that distinction and provides a local append-only ledger for
validation.  The ledger is the only operation this sandbox executes; it is
not a production side-effect gateway.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
from threading import RLock
import time
from typing import Any, Callable, Iterable
import uuid


class SideEffectError(RuntimeError):
    """Base class for side-effect boundary violations."""


class AllowlistViolation(SideEffectError):
    """The operation or exact target is not allowlisted."""


class ApprovalRequired(SideEffectError):
    """The operation lacks a valid, unexpired, single-use approval."""


class SideEffectClass(str, Enum):
    REVERSIBLE = "reversible"
    COMPENSATABLE = "compensatable"
    IRREVERSIBLE = "irreversible"


@dataclass(frozen=True)
class SideEffectRule:
    operation_id: str
    target: str
    classification: SideEffectClass
    approval_subject: str
    enabled: bool = True


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    operation_id: str
    target: str
    approver: str
    subject: str
    expires_at: float
    single_use: bool = True
    granted: bool = True


@dataclass(frozen=True)
class SideEffectReceipt:
    operation_id: str
    target: str
    classification: SideEffectClass
    idempotency_key: str
    approval_id: str | None
    audit_digest: str
    status: str


class SideEffectSandbox:
    """A root-confined, allowlisted, approval-aware local side-effect ledger."""

    def __init__(
        self,
        root: str | Path,
        rules: Iterable[SideEffectRule],
        *,
        audit_name: str = "side-effects.jsonl",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.audit_path = self.root / audit_name
        self._rules = {
            (str(rule.operation_id), str(rule.target)): rule for rule in rules
        }
        self._used_approvals: set[str] = set()
        self._receipts: dict[str, SideEffectReceipt] = {}
        self._clock = clock
        self._lock = RLock()
        self._load_existing_ledger()

    @property
    def rules(self) -> tuple[SideEffectRule, ...]:
        return tuple(self._rules.values())

    def _rule(self, operation_id: str, target: str) -> SideEffectRule:
        rule = self._rules.get((str(operation_id), str(target)))
        if rule is None or not rule.enabled:
            raise AllowlistViolation(
                f"operation/target is not allowlisted: {operation_id}:{target}"
            )
        return rule

    def _load_existing_ledger(self) -> None:
        """Recover receipts so idempotency survives a CLI/process restart."""

        if not self.audit_path.exists():
            return
        try:
            raw_lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise SideEffectError("side-effect audit ledger cannot be read") from exc
        if len(raw_lines) > 100_000:
            raise SideEffectError("side-effect audit ledger exceeds recovery limit")
        for line in raw_lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                raise SideEffectError("side-effect audit ledger is malformed") from exc
            if record.get("event") != "local_side_effect_committed":
                continue
            supplied_digest = str(record.get("audit_digest") or "")
            unsigned = dict(record)
            unsigned.pop("audit_digest", None)
            expected_digest = hashlib.sha256(
                json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            if supplied_digest != expected_digest:
                raise SideEffectError("side-effect audit digest mismatch")
            try:
                classification = SideEffectClass(str(record["classification"]))
                receipt = SideEffectReceipt(
                    operation_id=str(record["operation_id"]),
                    target=str(record["target"]),
                    classification=classification,
                    idempotency_key=str(record["idempotency_key"]),
                    approval_id=record.get("approval_id"),
                    audit_digest=supplied_digest,
                    status="committed_local_sandbox",
                )
            except (KeyError, ValueError, TypeError) as exc:
                raise SideEffectError("side-effect audit record is incomplete") from exc
            self._receipts.setdefault(receipt.idempotency_key, receipt)
            if receipt.approval_id:
                self._used_approvals.add(receipt.approval_id)

    def _validate_approval(
        self,
        rule: SideEffectRule,
        approval: ApprovalRecord | None,
    ) -> None:
        if rule.classification is not SideEffectClass.IRREVERSIBLE:
            return
        if approval is None:
            raise ApprovalRequired("irreversible operation requires approval")
        if not approval.granted:
            raise ApprovalRequired("approval was denied")
        if approval.approval_id in self._used_approvals and approval.single_use:
            raise ApprovalRequired("approval has already been consumed")
        if approval.operation_id != rule.operation_id or approval.target != rule.target:
            raise ApprovalRequired("approval does not match the exact operation target")
        if approval.subject != rule.approval_subject:
            raise ApprovalRequired("approval subject does not match the allowlist")
        if approval.expires_at <= self._clock():
            raise ApprovalRequired("approval has expired")
        if not approval.approver.strip():
            raise ApprovalRequired("approval must identify an approver")

    def _target_path(self, target: str) -> Path:
        """Resolve an allowlisted local sink without accepting traversal."""

        candidate = Path(target)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise AllowlistViolation("side-effect target must be a relative local path")
        resolved = (self.root / candidate).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise AllowlistViolation("side-effect target escapes sandbox root") from exc
        return resolved

    @staticmethod
    def _request_digest(
        operation_id: str,
        target: str,
        classification: SideEffectClass,
        idempotency_key: str,
    ) -> str:
        payload = "|".join((operation_id, target, classification.value, idempotency_key))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def execute_local(
        self,
        *,
        operation_id: str,
        target: str,
        classification: SideEffectClass,
        idempotency_key: str,
        approval: ApprovalRecord | None = None,
        payload: dict[str, Any] | None = None,
    ) -> SideEffectReceipt:
        """Record one safe local effect; never calls a remote side-effect API."""

        operation_id = str(operation_id or "").strip()
        target = str(target or "").strip()
        idempotency_key = str(idempotency_key or "").strip()
        if not operation_id or not target or not idempotency_key:
            raise ValueError("operation_id, target, and idempotency_key are required")
        if not isinstance(classification, SideEffectClass):
            classification = SideEffectClass(str(classification))
        with self._lock:
            rule = self._rule(operation_id, target)
            if rule.classification is not classification:
                raise AllowlistViolation("requested classification differs from allowlist")
            previous = self._receipts.get(idempotency_key)
            if previous is not None:
                if (
                    previous.operation_id != operation_id
                    or previous.target != target
                    or previous.classification is not classification
                ):
                    raise AllowlistViolation("idempotency key is bound to another operation")
                return previous
            self._validate_approval(rule, approval)
            approval_id = approval.approval_id if approval is not None else None
            record = {
                "schema_version": "1.0",
                "event": "local_side_effect_committed",
                "operation_id": operation_id,
                "target": target,
                "classification": classification.value,
                "idempotency_key": idempotency_key,
                "approval_id": approval_id,
                "approver": approval.approver if approval else None,
                "payload_digest": hashlib.sha256(
                    json.dumps(payload or {}, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
                "committed_at": self._clock(),
            }
            encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
            audit_digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            record["audit_digest"] = audit_digest
            target_path = self._target_path(target)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_encoded = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            with target_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(target_encoded)
            with self.audit_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(target_encoded)
            if approval is not None and approval.single_use:
                self._used_approvals.add(approval.approval_id)
            receipt = SideEffectReceipt(
                operation_id=operation_id,
                target=target,
                classification=classification,
                idempotency_key=idempotency_key,
                approval_id=approval_id,
                audit_digest=audit_digest,
                status="committed_local_sandbox",
            )
            self._receipts[idempotency_key] = receipt
            return receipt

    def describe_boundaries(self) -> dict[str, Any]:
        """Return a redacted policy view suitable for readiness reports."""

        return {
            "mode": "local_sandbox_only",
            "production_side_effects_enabled": False,
            "audit_path": str(self.audit_path),
            "target_root": str(self.root),
            "rules": [
                {
                    "operation_id": rule.operation_id,
                    "target": rule.target,
                    "classification": rule.classification.value,
                    "approval_subject": rule.approval_subject,
                    "enabled": rule.enabled,
                }
                for rule in self.rules
            ],
        }


def make_validation_sandbox(root: str | Path) -> SideEffectSandbox:
    """Create the explicitly local validation target used by the runbook."""

    return SideEffectSandbox(
        root,
        (
            SideEffectRule(
                operation_id="validation.append_irreversible_record",
                target="irreversible-ledger.jsonl",
                classification=SideEffectClass.IRREVERSIBLE,
                approval_subject="hermes-validation-irreversible-sandbox",
            ),
            SideEffectRule(
                operation_id="validation.write_compensatable_record",
                target="compensatable-ledger.jsonl",
                classification=SideEffectClass.COMPENSATABLE,
                approval_subject="hermes-validation-compensatable-sandbox",
            ),
        ),
    )


__all__ = [
    "AllowlistViolation",
    "ApprovalRecord",
    "ApprovalRequired",
    "SideEffectClass",
    "SideEffectError",
    "SideEffectReceipt",
    "SideEffectRule",
    "SideEffectSandbox",
    "make_validation_sandbox",
]
