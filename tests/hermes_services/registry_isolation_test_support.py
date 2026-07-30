from __future__ import annotations

from tools.registry import registry


LAMBDA_TOOL_NAME = "registry_import_lambda_probe"
CLOSURE_TOOL_NAME = "registry_import_closure_probe"


def _make_closure(prefix: str):
    def _handler(args: dict, **_kwargs):
        return f"{prefix}:{args.get('value', '')}"

    return _handler


registry.register(
    name=LAMBDA_TOOL_NAME,
    toolset="test-registry-import",
    schema={"name": LAMBDA_TOOL_NAME, "parameters": {"type": "object"}},
    handler=lambda args, **_kwargs: f"lambda:{args.get('value', '')}",
)
registry.register(
    name=CLOSURE_TOOL_NAME,
    toolset="test-registry-import",
    schema={"name": CLOSURE_TOOL_NAME, "parameters": {"type": "object"}},
    handler=_make_closure("closure"),
)
