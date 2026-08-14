"""Composable, cache-aware prompt runtime for Hermes capabilities.

This module adapts the DeepSeek Harness prompt-runtime ideas to Hermes' Python
core without changing the existing conversation contract. Prompt
instructions and tool guidance are assembled into a stable snapshot; runtime
context is returned separately and never silently appended to the stable
system prompt.

The runtime is intentionally small. Ordinary pure functions do not need to be
registered here. It is for model-facing capabilities, prompt scopes, strict
templates, and middleware that cross plugin or session boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
import threading
from typing import Any, Callable, Iterable, Mapping, Sequence


class PromptRuntimeError(ValueError):
    """Base error for invalid prompt-runtime composition."""


class PromptTemplateError(PromptRuntimeError):
    """Raised for malformed, unknown, or missing prompt variables."""


class PromptRegistrationError(PromptRuntimeError):
    """Raised for duplicate or invalid prompt registrations."""


_VARIABLE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_VARIABLE_GROUP = re.compile(r"\{\{([^{}]*)\}\}")
_VALID_MODES = frozenset({"native", "ptc", "creation"})
_MAX_FRAGMENT_CHARS = 128_000


@dataclass(frozen=True)
class PromptAssemblyContext:
    """Inputs visible to one deterministic assembly request."""

    agent_scope: str = ""
    override_scope: str = ""
    variables: Mapping[str, str] = field(default_factory=dict)
    runtime_context: Mapping[str, Any] = field(default_factory=dict)
    tool_names: tuple[str, ...] = ()
    mode: str = "native"


@dataclass(frozen=True)
class PromptFragment:
    """One model-facing prompt contribution owned by a capability."""

    name: str
    section: str
    text: str | Callable[[PromptAssemblyContext], str]
    order: int = 0
    scope: str = "global"
    stable: bool = True
    capability: str = ""


@dataclass(frozen=True)
class PromptMiddleware:
    """A deterministic transform over the assembled prompt draft."""

    name: str
    callback: Callable[["PromptDraft"], "PromptDraft | None"]
    order: int = 0
    scope: str = "global"


@dataclass
class PromptDraft:
    """Mutable middleware view; it is frozen into :class:`PromptAssembly`."""

    instructions: list[tuple[str, str]]
    runtime_context: list[tuple[str, str]]
    tool_guidance: list[tuple[str, str]]
    tool_schemas: list[dict[str, Any]]
    mode: str


@dataclass(frozen=True)
class PromptAssembly:
    """Immutable result of one prompt-runtime assembly."""

    instructions: str
    runtime_context: str
    tool_guidance: str
    tool_schemas: tuple[dict[str, Any], ...]
    mode: str
    stable_fingerprint: str
    context_fingerprint: str
    revision: int

    @property
    def model_instructions(self) -> str:
        """Stable instruction text including capability guidance."""

        return "\n\n".join(
            part for part in (self.instructions, self.tool_guidance) if part
        )

    def as_metadata(self) -> dict[str, Any]:
        """Return non-sensitive provenance for diagnostics and telemetry."""

        return {
            "schema_version": "hermes.prompt-runtime.v1",
            "mode": self.mode,
            "stable_fingerprint": self.stable_fingerprint,
            "context_fingerprint": self.context_fingerprint,
            "revision": self.revision,
            "tool_count": len(self.tool_schemas),
        }


def _validate_name(value: str, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or not re.match(r"^[A-Za-z][A-Za-z0-9_.:-]*$", normalized):
        raise PromptRegistrationError(f"invalid prompt {label} {value!r}")
    return normalized


def _validate_scope(scope: str) -> str:
    normalized = str(scope or "global").strip() or "global"
    if normalized != "global":
        _validate_name(normalized, "scope")
    return normalized


def _render_template(text: str, variables: Mapping[str, str], *, owner: str) -> str:
    """Render Harness-style ``{{name}}`` references strictly."""

    if not isinstance(text, str):
        raise PromptTemplateError(f"prompt contribution {owner!r} must return text")
    if len(text) > _MAX_FRAGMENT_CHARS:
        raise PromptTemplateError(
            f"prompt contribution {owner!r} exceeds {_MAX_FRAGMENT_CHARS} characters"
        )
    if ("{{" in text or "}}" in text) and not _VARIABLE_GROUP.search(text):
        raise PromptTemplateError(f"malformed prompt variable reference in {owner!r}")

    cursor = 0
    rendered: list[str] = []
    while cursor < len(text):
        opening = text.find("{{", cursor)
        closing = text.find("}}", cursor)
        if closing >= 0 and (opening < 0 or closing < opening):
            raise PromptTemplateError(f"malformed prompt variable reference in {owner!r}")
        if opening < 0:
            rendered.append(text[cursor:])
            break
        rendered.append(text[cursor:opening])
        match = _VARIABLE_GROUP.match(text, opening)
        if match is None:
            raise PromptTemplateError(f"malformed prompt variable reference in {owner!r}")
        name = match.group(1)
        if not _VARIABLE_NAME.fullmatch(name):
            raise PromptTemplateError(
                f"malformed prompt variable {{{{{name}}}}} in {owner!r}"
            )
        if name not in variables:
            raise PromptTemplateError(
                f"unknown prompt variable {{{{{name}}}}} in {owner!r}"
            )
        value = variables[name]
        if value is None:
            raise PromptTemplateError(
                f"prompt variable {{{{{name}}}}} has no value in {owner!r}"
            )
        rendered.append(str(value))
        cursor = match.end()
    return "".join(rendered).strip()


def _join(parts: Iterable[tuple[str, str]]) -> str:
    return "\n\n".join(text for _name, text in parts if text.strip())


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_tool_names(tool_names: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_name in tool_names:
        name = str(raw_name or "").strip()
        if not name or name in seen:
            raise PromptRuntimeError(f"tool names must be unique and non-empty: {raw_name!r}")
        seen.add(name)
        normalized.append(name)
    return tuple(normalized)


def _normalize_tool_schemas(
    tool_schemas: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, schema in enumerate(tool_schemas):
        if not isinstance(schema, Mapping):
            raise PromptRuntimeError(f"tool schema at index {index} must be a mapping")
        copied = dict(schema)
        function = copied.get("function")
        name = function.get("name") if isinstance(function, Mapping) else copied.get("name")
        if not isinstance(name, str) or not name.strip():
            raise PromptRuntimeError(f"tool schema at index {index} has no non-empty name")
        if name in seen:
            raise PromptRuntimeError(f"tool schema {name!r} is duplicated")
        seen.add(name)
        normalized.append(copied)
    return normalized


class PromptRuntime:
    """Registry and assembler for scoped model-facing capability prompts."""

    def __init__(self, *, revision: int = 1) -> None:
        self._lock = threading.RLock()
        self._fragments: dict[tuple[str, str], PromptFragment] = {}
        self._variables: dict[tuple[str, str], Callable[[PromptAssemblyContext], str | None]] = {}
        self._middleware: dict[tuple[str, str], PromptMiddleware] = {}
        self._revision = int(revision)

    @property
    def revision(self) -> int:
        return self._revision

    def _bump(self) -> None:
        self._revision += 1

    def register_fragment(self, fragment: PromptFragment) -> Callable[[], None]:
        name = _validate_name(fragment.name, "fragment name")
        section = _validate_name(fragment.section, "section")
        scope = _validate_scope(fragment.scope)
        if not isinstance(fragment.order, int):
            raise PromptRegistrationError(f"prompt fragment {name!r} order must be an integer")
        if not isinstance(fragment.text, str) and not callable(fragment.text):
            raise PromptRegistrationError(f"prompt fragment {name!r} text must be text or callable")
        if section == "tool_guidance" and not fragment.stable:
            raise PromptRegistrationError(
                f"tool guidance {name!r} must be stable; put changing values in runtime_context"
            )
        key = (scope, name)
        stored = PromptFragment(
            name=name,
            section=section,
            text=fragment.text,
            order=fragment.order,
            scope=scope,
            stable=bool(fragment.stable),
            capability=str(fragment.capability or "").strip(),
        )
        with self._lock:
            if key in self._fragments:
                raise PromptRegistrationError(
                    f"prompt fragment {name!r} is already registered in scope {scope!r}"
                )
            self._fragments[key] = stored
            self._bump()

        def dispose() -> None:
            with self._lock:
                if self._fragments.pop(key, None) is not None:
                    self._bump()

        return dispose

    def register_tool_guidance(
        self,
        tool_name: str,
        text: str | Callable[[PromptAssemblyContext], str],
        *,
        order: int = 0,
        scope: str = "global",
    ) -> Callable[[], None]:
        tool = _validate_name(tool_name, "tool name")
        return self.register_fragment(PromptFragment(
            name=f"tool:{tool}",
            section="tool_guidance",
            text=text,
            order=order,
            scope=scope,
            stable=True,
            capability=tool,
        ))

    def register_variable(
        self,
        name: str,
        provider: Callable[[PromptAssemblyContext], str | None],
        *,
        scope: str = "global",
    ) -> Callable[[], None]:
        variable = str(name or "").strip()
        if not _VARIABLE_NAME.fullmatch(variable):
            raise PromptRegistrationError(f"invalid prompt variable name {name!r}")
        if not callable(provider):
            raise PromptRegistrationError(f"prompt variable {variable!r} provider must be callable")
        normalized_scope = _validate_scope(scope)
        key = (normalized_scope, variable)
        with self._lock:
            if key in self._variables:
                raise PromptRegistrationError(
                    f"prompt variable {variable!r} is already registered in scope {normalized_scope!r}"
                )
            self._variables[key] = provider
            self._bump()

        def dispose() -> None:
            with self._lock:
                if self._variables.pop(key, None) is not None:
                    self._bump()

        return dispose

    def register_middleware(
        self,
        name: str,
        callback: Callable[[PromptDraft], PromptDraft | None],
        *,
        order: int = 0,
        scope: str = "global",
    ) -> Callable[[], None]:
        normalized = _validate_name(name, "middleware name")
        normalized_scope = _validate_scope(scope)
        if not callable(callback):
            raise PromptRegistrationError(f"prompt middleware {normalized!r} must be callable")
        key = (normalized_scope, normalized)
        with self._lock:
            if key in self._middleware:
                raise PromptRegistrationError(
                    f"prompt middleware {normalized!r} is already registered in scope {normalized_scope!r}"
                )
            self._middleware[key] = PromptMiddleware(
                normalized, callback, int(order), normalized_scope
            )
            self._bump()

        def dispose() -> None:
            with self._lock:
                if self._middleware.pop(key, None) is not None:
                    self._bump()

        return dispose

    def _visible_fragments(
        self,
        agent_scope: str,
        override_scope: str,
        extra_fragments: Sequence[PromptFragment] = (),
        *,
        registered_fragments: Sequence[tuple[tuple[str, str], PromptFragment]] | None = None,
    ) -> list[PromptFragment]:
        scopes = ["global"]
        if agent_scope and agent_scope != "global":
            scopes.append(_validate_scope(agent_scope))
        if override_scope and override_scope not in scopes:
            scopes.append(_validate_scope(override_scope))
        selected: dict[str, PromptFragment] = {}
        if registered_fragments is None:
            with self._lock:
                registered_fragments = tuple(self._fragments.items())
        for scope in scopes:
            for (registered_scope, name), fragment in registered_fragments:
                if registered_scope == scope:
                    selected[name] = fragment
        for fragment in extra_fragments:
            name = _validate_name(fragment.name, "fragment name")
            scope = _validate_scope(fragment.scope)
            section = _validate_name(fragment.section, "section")
            if section == "tool_guidance" and not fragment.stable:
                raise PromptRegistrationError(
                    f"tool guidance {name!r} must be stable; put changing values in runtime_context"
                )
            if scope in scopes:
                selected[name] = PromptFragment(
                    name=name,
                    section=section,
                    text=fragment.text,
                    order=int(fragment.order),
                    scope=scope,
                    stable=bool(fragment.stable),
                    capability=str(fragment.capability or "").strip(),
                )
        return sorted(selected.values(), key=lambda item: (item.order, item.name))

    def _resolve_variables(
        self,
        context: PromptAssemblyContext,
        *,
        registered_variables: Sequence[
            tuple[tuple[str, str], Callable[[PromptAssemblyContext], str | None]]
        ] | None = None,
    ) -> dict[str, str]:
        values: dict[str, str] = {str(k): str(v) for k, v in context.variables.items() if v is not None}
        scopes = ["global"]
        if context.agent_scope and context.agent_scope != "global":
            scopes.append(_validate_scope(context.agent_scope))
        if context.override_scope and context.override_scope not in scopes:
            scopes.append(_validate_scope(context.override_scope))
        if registered_variables is None:
            with self._lock:
                registered_variables = tuple(self._variables.items())
        for scope in scopes:
            for (registered_scope, name), provider in registered_variables:
                if registered_scope == scope:
                    value = provider(context)
                    if value is None:
                        values.pop(name, None)
                    else:
                        values[name] = str(value)
        return values

    def assemble(
        self,
        *,
        agent_scope: str = "",
        override_scope: str = "",
        variables: Mapping[str, str] | None = None,
        runtime_context: Mapping[str, Any] | None = None,
        tool_names: Sequence[str] = (),
        tool_schemas: Sequence[Mapping[str, Any]] = (),
        mode: str = "native",
        extra_fragments: Sequence[PromptFragment] = (),
    ) -> PromptAssembly:
        normalized_mode = str(mode or "native").strip().lower()
        if normalized_mode not in _VALID_MODES:
            raise PromptRuntimeError(f"unknown prompt runtime mode {mode!r}")
        normalized_tool_names = _normalize_tool_names(tool_names)
        normalized_schemas = _normalize_tool_schemas(tool_schemas)
        context = PromptAssemblyContext(
            agent_scope=str(agent_scope or "").strip(),
            override_scope=str(override_scope or "").strip(),
            variables=dict(variables or {}),
            runtime_context=dict(runtime_context or {}),
            tool_names=normalized_tool_names,
            mode=normalized_mode,
        )
        scopes = ["global"]
        if context.agent_scope and context.agent_scope != "global":
            scopes.append(_validate_scope(context.agent_scope))
        if context.override_scope and context.override_scope not in scopes:
            scopes.append(_validate_scope(context.override_scope))
        with self._lock:
            fragment_snapshot = tuple(self._fragments.items())
            variable_snapshot = tuple(self._variables.items())
            middleware_snapshot = tuple(self._middleware.items())
            assembly_revision = self._revision
        selected_middleware: dict[str, PromptMiddleware] = {}
        for scope in scopes:
            for (registered_scope, name), middleware in middleware_snapshot:
                if registered_scope == scope:
                    selected_middleware[name] = middleware
        values = self._resolve_variables(
            context,
            registered_variables=variable_snapshot,
        )
        instructions: list[tuple[str, str]] = []
        runtime_sections: list[tuple[str, str]] = []
        guidance: list[tuple[str, str]] = []
        tool_name_set = set(context.tool_names)

        for fragment in self._visible_fragments(
            context.agent_scope,
            context.override_scope,
            extra_fragments,
            registered_fragments=fragment_snapshot,
        ):
            if fragment.capability and fragment.capability not in tool_name_set:
                continue
            raw = fragment.text(context) if callable(fragment.text) else fragment.text
            rendered = _render_template(raw, values, owner=fragment.name)
            if not rendered:
                continue
            if fragment.section == "tool_guidance":
                guidance.append((fragment.name, rendered))
            elif fragment.stable:
                instructions.append((fragment.name, rendered))
            else:
                runtime_sections.append((fragment.name, rendered))

        if context.runtime_context:
            runtime_sections.append((
                "runtime:context",
                json.dumps(context.runtime_context, sort_keys=True, ensure_ascii=False, default=str),
            ))

        draft = PromptDraft(
            instructions=instructions,
            runtime_context=runtime_sections,
            tool_guidance=guidance,
            tool_schemas=normalized_schemas,
            mode=normalized_mode,
        )
        for middleware in sorted(
            selected_middleware.values(),
            key=lambda item: (item.order, item.name),
        ):
            result = middleware.callback(draft)
            if result is not None:
                if not isinstance(result, PromptDraft):
                    raise PromptRuntimeError(
                        f"prompt middleware {middleware.name!r} must return PromptDraft or None"
                    )
                draft = result

        if draft.mode not in _VALID_MODES:
            raise PromptRuntimeError(f"prompt middleware produced unknown mode {draft.mode!r}")
        schemas = _normalize_tool_schemas(draft.tool_schemas)

        stable_sections = _join(draft.instructions)
        stable_guidance = _join(draft.tool_guidance)
        dynamic_context = _join(draft.runtime_context)
        schemas_tuple = tuple(schemas)
        stable_fingerprint = _digest({
            "instructions": stable_sections,
            "tool_guidance": stable_guidance,
            "tool_schemas": schemas_tuple,
            "mode": draft.mode,
        })
        context_fingerprint = _digest({"runtime_context": dynamic_context})
        return PromptAssembly(
            instructions=stable_sections,
            runtime_context=dynamic_context,
            tool_guidance=stable_guidance,
            tool_schemas=schemas_tuple,
            mode=draft.mode,
            stable_fingerprint=stable_fingerprint,
            context_fingerprint=context_fingerprint,
            revision=assembly_revision,
        )


__all__ = [
    "PromptAssembly",
    "PromptAssemblyContext",
    "PromptDraft",
    "PromptFragment",
    "PromptMiddleware",
    "PromptRegistrationError",
    "PromptRuntime",
    "PromptRuntimeError",
    "PromptTemplateError",
]


_DEFAULT_RUNTIME = PromptRuntime()


def default_prompt_runtime() -> PromptRuntime:
    """Return the process-local capability prompt registry.

    Plugin registrations are effect-owned and disposed by the plugin manager.
    The registry itself is process-local; a running agent keeps the snapshot it
    started with, so a later plugin load cannot mutate its prompt cache.
    """

    return _DEFAULT_RUNTIME


__all__.append("default_prompt_runtime")
