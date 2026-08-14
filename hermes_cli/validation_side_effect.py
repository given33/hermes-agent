"""CLI for the local irreversible-side-effect validation sandbox."""

from __future__ import annotations

import argparse
import json
import time

from hermes_runtime.composability.side_effects import (
    ApprovalRecord,
    SideEffectClass,
    make_validation_sandbox,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--operation", default="validation.append_irreversible_record")
    parser.add_argument("--target", default="irreversible-ledger.jsonl")
    parser.add_argument("--idempotency-key", default="")
    parser.add_argument("--execute-local", action="store_true")
    parser.add_argument("--approver", default="")
    parser.add_argument("--approval-id", default="")
    parser.add_argument("--approval-expires-in", type=float, default=0.0)
    parser.add_argument("--payload-json", default="{}")
    args = parser.parse_args(argv)
    sandbox = make_validation_sandbox(args.root)
    if not args.execute_local:
        print(json.dumps(sandbox.describe_boundaries(), sort_keys=True, indent=2))
        return 0
    if not args.idempotency_key or not args.approver or not args.approval_id:
        parser.error("--execute-local requires --idempotency-key, --approver, and --approval-id")
    try:
        payload = json.loads(args.payload_json)
    except json.JSONDecodeError as exc:
        parser.error(f"--payload-json must be valid JSON: {exc}")
    if not isinstance(payload, dict):
        parser.error("--payload-json must be a JSON object")
    approval = ApprovalRecord(
        approval_id=args.approval_id,
        operation_id=args.operation,
        target=args.target,
        approver=args.approver,
        subject="hermes-validation-irreversible-sandbox",
        expires_at=time.time() + args.approval_expires_in,
    )
    receipt = sandbox.execute_local(
        operation_id=args.operation,
        target=args.target,
        classification=SideEffectClass.IRREVERSIBLE,
        idempotency_key=args.idempotency_key,
        approval=approval,
        payload=payload,
    )
    print(json.dumps(receipt.__dict__, default=str, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
