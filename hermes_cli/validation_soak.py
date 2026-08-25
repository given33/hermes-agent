"""Bounded MCP visual/REA soak runner.

The runner keeps real stdio MCP sessions open, repeatedly calls both local
providers, checks provider identity/generation and latency, and closes both
sessions on success, stop condition, or interruption.  It is safe to run for
hours against the validation roots; it never connects to a production host or
performs an external side effect.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, AsyncIterator

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from hermes_runtime.composability.providers import ProviderCatalog


EXPECTED = {
    "hermes_cli.visual_evidence_mcp": "hermes-visual-evidence",
    "hermes_cli.rea_mcp_server": "rea-local-readonly",
}


@asynccontextmanager
async def _session(module: str, root: str) -> AsyncIterator[ClientSession]:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", module, "--root", root],
        env=dict(os.environ),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=20)
            listed = await asyncio.wait_for(session.list_tools(), timeout=20)
            names = {tool.name for tool in listed.tools}
            if module == "hermes_cli.visual_evidence_mcp" and "inspect_image" not in names:
                raise RuntimeError("visual inspect_image tool is unavailable")
            if module == "hermes_cli.rea_mcp_server" and "inspect_artifact" not in names:
                raise RuntimeError("REA inspect_artifact tool is unavailable")
            yield session


def _decode_tool_result(result: Any) -> dict[str, Any]:
    # MCP 2.0 renamed isError to is_error; support both SDK generations.
    if getattr(result, "is_error", getattr(result, "isError", False)):
        raise RuntimeError("MCP tool returned an error")
    for block in getattr(result, "content", ()):
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeError("MCP tool did not return a JSON object")


async def run_soak(
    *,
    visual_root: str,
    rea_root: str,
    duration_seconds: float,
    interval_seconds: float,
    max_consecutive_errors: int,
    visual_path: str = "dashboard.png",
    rea_path: str = "sample.ipa",
) -> dict[str, Any]:
    if duration_seconds <= 0 or interval_seconds <= 0:
        raise ValueError("duration and interval must be positive")
    if max_consecutive_errors <= 0:
        raise ValueError("max_consecutive_errors must be positive")
    started = time.monotonic()
    calls = 0
    errors = 0
    consecutive_errors = 0
    max_latency_ms = 0.0
    stop_reason = "duration_reached"
    provider_generations: dict[str, int] = {}
    drain_verified = False
    catalog = ProviderCatalog()
    soak_provider = catalog.register(
        provider_id="soak:local-mcp",
        interface_key="mcp:validation",
        version="1.0.0",
        health="healthy",
    )
    provider_generations[soak_provider.provider_id] = soak_provider.generation
    async with AsyncExitStack() as stack:
        visual_session = await stack.enter_async_context(
            _session("hermes_cli.visual_evidence_mcp", visual_root)
        )
        rea_session = await stack.enter_async_context(
            _session("hermes_cli.rea_mcp_server", rea_root)
        )
        while time.monotonic() - started < duration_seconds:
            cycle_start = time.monotonic()
            try:
                catalog.begin_call(soak_provider.provider_id)
                visual_result = _decode_tool_result(
                    await asyncio.wait_for(
                        visual_session.call_tool(
                            "inspect_image", arguments={"path": visual_path}
                        ),
                        timeout=20,
                    )
                )
                rea_result = _decode_tool_result(
                    await asyncio.wait_for(
                        rea_session.call_tool(
                            "inspect_artifact", arguments={"path": rea_path}
                        ),
                        timeout=20,
                    )
                )
                for result in (visual_result, rea_result):
                    expected = EXPECTED[
                        "hermes_cli.visual_evidence_mcp"
                        if result.get("provider_id") == "hermes-visual-evidence"
                        else "hermes_cli.rea_mcp_server"
                    ]
                    if result.get("provider_id") != expected or result.get("provider_generation") != 1:
                        raise RuntimeError("provider identity/generation changed during soak")
                    if result.get("result", {}).get("ok") is not True:
                        raise RuntimeError("provider returned a structured failure")
                calls += 2
                consecutive_errors = 0
            except Exception as exc:
                errors += 1
                consecutive_errors += 1
                stop_reason = f"error:{type(exc).__name__}"
                if consecutive_errors >= max_consecutive_errors:
                    break
            finally:
                if catalog.get(soak_provider.provider_id) and catalog.get(soak_provider.provider_id).inflight:
                    catalog.end_call(soak_provider.provider_id)
            max_latency_ms = max(max_latency_ms, (time.monotonic() - cycle_start) * 1000)
            remaining = interval_seconds - (time.monotonic() - cycle_start)
            if remaining > 0:
                await asyncio.sleep(remaining)
        try:
            catalog.begin_drain(soak_provider.provider_id, deadline_seconds=5)
            catalog.unload(soak_provider.provider_id)
            drain_verified = True
        except Exception:
            errors += 1
            stop_reason = "drain_failure"
    elapsed = time.monotonic() - started
    return {
        "schema_version": "1.0",
        "mode": "local_validation_only",
        "duration_seconds": round(elapsed, 3),
        "requested_duration_seconds": duration_seconds,
        "calls": calls,
        "errors": errors,
        "max_consecutive_errors": max_consecutive_errors,
        "max_latency_ms": round(max_latency_ms, 3),
        "stop_reason": stop_reason,
        "provider_generation": provider_generations[soak_provider.provider_id],
        "drain_verified": drain_verified,
        "sessions_closed": True,
        "external_side_effects": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--visual-root", required=True)
    parser.add_argument("--rea-root", required=True)
    parser.add_argument("--duration-seconds", type=float, default=7_200)
    parser.add_argument("--duration-hours", type=float, default=0.0)
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    parser.add_argument("--max-consecutive-errors", type=int, default=3)
    parser.add_argument("--visual-path", default="dashboard.png")
    parser.add_argument("--rea-path", default="sample.ipa")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    duration = args.duration_seconds
    if args.duration_hours > 0:
        duration = args.duration_hours * 3600
    try:
        report = asyncio.run(
            run_soak(
                visual_root=args.visual_root,
                rea_root=args.rea_root,
                duration_seconds=duration,
                interval_seconds=args.interval_seconds,
                max_consecutive_errors=args.max_consecutive_errors,
                visual_path=args.visual_path,
                rea_path=args.rea_path,
            )
        )
    except KeyboardInterrupt:
        report = {
            "schema_version": "1.0",
            "mode": "local_validation_only",
            "stop_reason": "operator_interrupt",
            "sessions_closed": True,
            "external_side_effects": False,
        }
    encoded = json.dumps(report, sort_keys=True, indent=2)
    print(encoded)
    if args.output:
        Path(args.output).write_text(encoded + "\n", encoding="utf-8")
    return 0 if report.get("errors", 0) == 0 else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
