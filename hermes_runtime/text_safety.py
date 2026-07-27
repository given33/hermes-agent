"""Dependency-free text normalization used by persistence and agent layers."""

from __future__ import annotations

import re


_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
_MEMORY_FENCE_TAG_RE = re.compile(r"</?\s*memory-context\s*>", re.IGNORECASE)
_MEMORY_CONTEXT_RE = re.compile(
    r"<\s*memory-context\s*>[\s\S]*?</\s*memory-context\s*>",
    re.IGNORECASE,
)
_MEMORY_NOTE_RE = re.compile(
    r"\[System note:\s*The following is recalled memory context,\s*NOT new user input\."
    r"\s*Treat as (?:informational background data|authoritative reference data[^\]]*)\."
    r"\]\s*",
    re.IGNORECASE,
)


def sanitize_surrogates(text: str) -> str:
    """Replace lone UTF-16 surrogate code points with U+FFFD."""
    if _SURROGATE_RE.search(text):
        return _SURROGATE_RE.sub("\ufffd", text)
    return text


def strip_internal_memory_context(text: str) -> str:
    """Remove internal memory fences and provider-only recall notes."""
    text = _MEMORY_CONTEXT_RE.sub("", text)
    text = _MEMORY_NOTE_RE.sub("", text)
    return _MEMORY_FENCE_TAG_RE.sub("", text)


__all__ = ["sanitize_surrogates", "strip_internal_memory_context"]
