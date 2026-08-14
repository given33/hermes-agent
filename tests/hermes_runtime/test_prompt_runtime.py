import pytest

from hermes_runtime.prompt_runtime import (
    PromptDraft,
    PromptFragment,
    PromptRegistrationError,
    PromptRuntime,
    PromptTemplateError,
)


def test_runtime_assembles_stable_guidance_and_dynamic_context_separately():
    runtime = PromptRuntime()
    runtime.register_fragment(PromptFragment(
        name="identity",
        section="identity",
        text="You are {{model}}.",
    ))
    runtime.register_fragment(PromptFragment(
        name="cwd",
        section="context",
        text="cwd is supplied at runtime",
        stable=False,
    ))
    runtime.register_tool_guidance("execute_code", "Use PTC for bounded multi-step work.")

    assembly = runtime.assemble(
        variables={"model": "hermes-test"},
        runtime_context={"cwd": "C:\\workspace"},
        tool_names=("execute_code",),
        mode="ptc",
    )

    assert assembly.instructions == "You are hermes-test."
    assert "cwd is supplied" in assembly.runtime_context
    assert '"cwd": "C:\\\\workspace"' in assembly.runtime_context
    assert "PTC" in assembly.tool_guidance
    assert assembly.stable_fingerprint != assembly.context_fingerprint
    assert assembly.as_metadata()["schema_version"] == "hermes.prompt-runtime.v1"


def test_scope_override_replaces_same_fragment_and_variable():
    runtime = PromptRuntime()
    runtime.register_fragment(PromptFragment(
        name="persona", section="persona", text="global {{role}}"
    ))
    runtime.register_fragment(PromptFragment(
        name="persona", section="persona", text="agent {{role}}", scope="agent"
    ))
    runtime.register_variable("role", lambda _ctx: "base")
    runtime.register_variable("role", lambda _ctx: "scoped", scope="agent")

    assembly = runtime.assemble(agent_scope="agent")

    assert assembly.instructions == "agent scoped"


def test_unknown_and_malformed_variables_fail_closed():
    runtime = PromptRuntime()
    runtime.register_fragment(PromptFragment(
        name="unknown", section="persona", text="{{missing}}"
    ))
    with pytest.raises(PromptTemplateError, match="unknown prompt variable"):
        runtime.assemble()

    runtime = PromptRuntime()
    runtime.register_fragment(PromptFragment(
        name="malformed", section="persona", text="{{bad-name}}"
    ))
    with pytest.raises(PromptTemplateError, match="malformed prompt variable"):
        runtime.assemble()


def test_duplicate_registration_and_bad_mode_are_rejected():
    runtime = PromptRuntime()
    runtime.register_fragment(PromptFragment(name="one", section="a", text="x"))
    with pytest.raises(PromptRegistrationError):
        runtime.register_fragment(PromptFragment(name="one", section="a", text="y"))
    with pytest.raises(ValueError, match="unknown prompt runtime mode"):
        runtime.assemble(mode="unsupported")


def test_middleware_is_ordered_and_must_return_prompt_draft():
    runtime = PromptRuntime()
    runtime.register_fragment(PromptFragment(name="one", section="a", text="one"))
    calls = []

    def first(draft):
        calls.append("first")
        draft.instructions.append(("first", "two"))
        return draft

    def second(draft):
        calls.append("second")
        return draft

    runtime.register_middleware("second", second, order=20)
    runtime.register_middleware("first", first, order=10)
    assembly = runtime.assemble()

    assert calls == ["first", "second"]
    assert assembly.instructions == "one\n\ntwo"


def test_middleware_scope_follows_global_agent_override_precedence():
    runtime = PromptRuntime()
    runtime.register_middleware(
        "decorate",
        lambda draft: (draft.instructions.append(("global", "global")) or draft),
    )
    runtime.register_middleware(
        "decorate",
        lambda draft: (draft.instructions.append(("agent", "agent")) or draft),
        scope="agent",
    )
    runtime.register_middleware(
        "decorate",
        lambda draft: (draft.instructions.append(("override", "override")) or draft),
        scope="override",
    )
    assert runtime.assemble(agent_scope="agent").instructions == "agent"
    assert runtime.assemble(
        agent_scope="agent", override_scope="override"
    ).instructions == "override"


def test_ptc_guidance_can_be_capability_owned():
    runtime = PromptRuntime()
    runtime.register_tool_guidance("execute_code", "PTC is available")

    assert "PTC" in runtime.assemble(tool_names=("execute_code",), mode="ptc").model_instructions
    assert runtime.assemble(tool_names=("terminal",), mode="ptc").tool_guidance == ""


def test_tool_names_and_schemas_are_strictly_unique():
    runtime = PromptRuntime()
    with pytest.raises(ValueError, match="unique"):
        runtime.assemble(tool_names=("read_file", "read_file"))
    with pytest.raises(ValueError, match="duplicated"):
        runtime.assemble(tool_schemas=(
            {"type": "function", "function": {"name": "read_file"}},
            {"type": "function", "function": {"name": "read_file"}},
        ))


def test_unloading_a_registration_changes_revision_and_removes_guidance():
    runtime = PromptRuntime()
    dispose = runtime.register_tool_guidance("execute_code", "PTC")
    first = runtime.assemble(tool_names=("execute_code",))
    dispose()
    second = runtime.assemble(tool_names=("execute_code",))
    assert first.tool_guidance == "PTC"
    assert second.tool_guidance == ""
    assert second.revision > first.revision


def test_assembly_uses_one_registry_snapshot_when_disposal_is_concurrent():
    runtime = PromptRuntime()
    dispose = runtime.register_fragment(PromptFragment(
        name="stable", section="identity", text="stable"
    ))
    first = runtime.assemble()
    dispose()
    second = runtime.assemble()
    assert first.instructions == "stable"
    assert second.instructions == ""


def test_plugin_context_disposes_prompt_contributions_with_its_effect_scope():
    from hermes_cli.plugins import PluginContext, PluginManifest, PluginManager
    from hermes_runtime.prompt_runtime import default_prompt_runtime

    runtime = default_prompt_runtime()
    context = PluginContext(
        PluginManifest(name="prompt-runtime-test", key="prompt-runtime-test"),
        PluginManager(),
    )
    context.register_prompt_fragment(
        "test:plugin-owned",
        section="persona",
        text="plugin-owned",
    )
    try:
        assert "plugin-owned" in runtime.assemble().instructions
    finally:
        context.close_effects()
    assert "plugin-owned" not in runtime.assemble().instructions


def test_tool_guidance_is_part_of_the_tool_registration_fingerprint():
    from tools.registry import ToolRegistry

    registry = ToolRegistry()

    def handler(_args):
        return "ok"

    schema = {
        "name": "fingerprint_test_tool",
        "description": "test",
        "parameters": {"type": "object", "properties": {}},
    }
    registry.register(
        name="fingerprint_test_tool",
        toolset="fingerprint-test",
        schema=schema,
        handler=handler,
        prompt_guidance="first guidance",
    )
    first = registry.registration_fingerprint("fingerprint_test_tool")
    registry.register(
        name="fingerprint_test_tool",
        toolset="fingerprint-test",
        schema=schema,
        handler=handler,
        prompt_guidance="second guidance",
    )
    second = registry.registration_fingerprint("fingerprint_test_tool")
    assert first != second
