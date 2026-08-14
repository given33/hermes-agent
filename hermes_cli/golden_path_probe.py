"""Run the deterministic local golden-path acceptance probe.

This is a validation command, not a replacement for the native agent loop.
It uses the real read-only visual provider implementation behind an admitted
MCP-shaped tool adapter so the full artifact/verdict/event contract can be
checked without sending a production task or model credential.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from hermes_cli import visual_evidence_mcp as visual
from hermes_runtime.composability import DependencySpec, ProviderCatalog
from hermes_runtime.golden_path import (
    GoldenPathPlan,
    GoldenPathRunner,
    GoldenPathSettings,
    GoldenPathTool,
    GoldenPathToolCall,
    JsonArtifactStore,
    StaticGoldenPathProvider,
)


def build_runner(*, task: str, root: Path, image: str, artifact_root: Path, turn_id: str) -> GoldenPathRunner:
    visual.configure_root(root)
    catalog = ProviderCatalog()
    catalog.register(
        provider_id="model:golden-validation",
        interface_key="model:golden",
        version="1",
        health="healthy",
        metadata={"logical_provider_id": "model:golden", "validation_only": True},
    )
    binding = catalog.resolve(DependencySpec(key="model:golden", version_range="^1.0"))
    if binding is None:
        raise RuntimeError("validation model provider did not resolve")

    def read_task(arguments):
        value = str(arguments.get("task") or task).strip()
        return {"task_digest": hashlib.sha256(value.encode("utf-8")).hexdigest(), "source": "validation-local"}

    def inspect_image(arguments):
        return visual.inspect_image(str(arguments.get("path") or image))

    provider = StaticGoldenPathProvider(
        model="golden-validation-static-provider",
        planner=lambda *_: GoldenPathPlan(
            (
                GoldenPathToolCall("local.read_task", {"task": task}),
                GoldenPathToolCall("mcp.visual.inspect_image", {"path": image}),
            )
        ),
    )
    settings = GoldenPathSettings(
        enabled=True,
        runtime_layer_enabled=True,
        task_id=f"validation-{turn_id}",
        conversation_id=f"validation-conversation-{turn_id}",
        turn_id=turn_id,
        source_revision="validation:local",
        prompt_version="prompt:golden-path-v1",
        provider_model=provider.model,
        max_retries=1,
    )
    return GoldenPathRunner(
        catalog=catalog,
        binding=binding,
        provider=provider,
        tools=(
            GoldenPathTool("local.read_task", "local", read_task),
            GoldenPathTool(
                "mcp.visual.inspect_image",
                "mcp",
                inspect_image,
                registry_generation=1,
                effect_metadata={"read_only": True, "provider_id": visual.PROVIDER_ID},
            ),
        ),
        artifact_store=JsonArtifactStore(artifact_root),
        settings=settings,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local Hermes golden-path acceptance probe")
    parser.add_argument("--task", default="inspect validation image")
    parser.add_argument("--visual-root", required=True)
    parser.add_argument("--image", default="dashboard.png")
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--turn-id", default="golden-validation")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    runner = build_runner(
        task=args.task,
        root=Path(args.visual_root),
        image=args.image,
        artifact_root=Path(args.artifact_root),
        turn_id=args.turn_id,
    )
    result = runner.run(args.task).as_dict()
    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output:
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result.get("status") == "completed" else 2


if __name__ == "__main__":
    sys.exit(main())
