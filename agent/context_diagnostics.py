"""Static diagnostics for the context Hermes assembles before a session.

The runtime deliberately keeps its cached prompt stable.  These checks therefore
run from ``hermes doctor`` instead of mutating or pruning a live conversation.
They identify expensive always-on sources, project files hidden by precedence,
and exact guidance repeated across active layers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable, Sequence

from agent.skill_utils import iter_skill_index_files


_KIND_CHAR_LIMITS = {
    "persona": 8_000,
    "memory": 8_000,
    "user": 8_000,
    "project": 16_000,
    "skill": 20_000,
}
_ALWAYS_ON_TOTAL_CHAR_LIMIT = 32_000
_MIN_DUPLICATE_LINE_CHARS = 60
_MARKDOWN_PREFIX_RE = re.compile(r"^(?:[-*+]\s+|\d+[.)]\s+|>\s*)+")
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class ContextSource:
    """One file that contributes context or can be loaded as a skill."""

    kind: str
    path: Path
    chars: int
    active: bool = True

    @property
    def estimated_tokens(self) -> int:
        return (self.chars + 3) // 4


@dataclass(frozen=True)
class ContextFinding:
    """An actionable, non-destructive context-health observation."""

    code: str
    message: str
    severity: str = "warning"
    paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ContextDiagnosticsReport:
    """Context inventory and findings returned to CLI/UI callers."""

    sources: tuple[ContextSource, ...]
    findings: tuple[ContextFinding, ...]

    @property
    def always_on_chars(self) -> int:
        return sum(
            source.chars
            for source in self.sources
            if source.active and source.kind != "skill"
        )

    @property
    def always_on_estimated_tokens(self) -> int:
        return (self.always_on_chars + 3) // 4


def _git_root(start: Path) -> Path | None:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _nearest_hermes_md(cwd: Path) -> Path | None:
    git_root = _git_root(cwd)
    search_dirs = (cwd, *cwd.parents) if git_root else (cwd,)
    for directory in search_dirs:
        for name in (".hermes.md", "HERMES.md"):
            candidate = directory / name
            if candidate.is_file():
                return candidate
        if git_root and directory == git_root:
            break
    return None


def _project_context_families(cwd: Path) -> list[tuple[str, tuple[Path, ...]]]:
    """Return present project-context families in runtime precedence order."""
    families: list[tuple[str, tuple[Path, ...]]] = []

    hermes_md = _nearest_hermes_md(cwd)
    if hermes_md:
        families.append(("Hermes", (hermes_md,)))

    agents = next(
        (cwd / name for name in ("AGENTS.md", "agents.md") if (cwd / name).is_file()),
        None,
    )
    if agents:
        families.append(("AGENTS", (agents,)))

    claude = next(
        (cwd / name for name in ("CLAUDE.md", "claude.md") if (cwd / name).is_file()),
        None,
    )
    if claude:
        families.append(("CLAUDE", (claude,)))

    cursor_paths: list[Path] = []
    cursorrules = cwd / ".cursorrules"
    if cursorrules.is_file():
        cursor_paths.append(cursorrules)
    cursor_dir = cwd / ".cursor" / "rules"
    if cursor_dir.is_dir():
        cursor_paths.extend(sorted(cursor_dir.glob("*.mdc")))
    if cursor_paths:
        families.append(("Cursor", tuple(cursor_paths)))

    return families


def _read_source(kind: str, path: Path, *, active: bool = True) -> ContextSource | None:
    try:
        content = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    if not content:
        return None
    return ContextSource(kind=kind, path=path, chars=len(content), active=active)


def _normalized_guidance_lines(path: Path) -> set[str]:
    """Extract long prose lines suitable for exact cross-layer deduplication."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return set()

    normalized: set[str] = set()
    in_fence = False
    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped or stripped.startswith(("#", "|", "<!--")):
            continue
        stripped = _MARKDOWN_PREFIX_RE.sub("", stripped)
        if len(stripped) < _MIN_DUPLICATE_LINE_CHARS or "://" in stripped:
            continue
        normalized.add(_WHITESPACE_RE.sub(" ", stripped).casefold())
    return normalized


def _duplicate_finding(sources: Sequence[ContextSource]) -> ContextFinding | None:
    by_line: dict[str, set[Path]] = {}
    for source in sources:
        if not source.active or source.kind == "skill":
            continue
        for line in _normalized_guidance_lines(source.path):
            by_line.setdefault(line, set()).add(source.path)

    repeated = [paths for paths in by_line.values() if len(paths) > 1]
    if not repeated:
        return None

    affected = tuple(sorted({path for paths in repeated for path in paths}, key=str))
    return ContextFinding(
        code="duplicate_guidance",
        message=(
            f"{len(repeated)} long guidance line(s) are repeated across active "
            "context layers; keep each rule in one authoritative source."
        ),
        paths=affected,
    )


def analyze_context_sources(
    *,
    cwd: Path,
    hermes_home: Path,
    skill_dirs: Iterable[Path] = (),
) -> ContextDiagnosticsReport:
    """Inspect the static context a fresh Hermes session can assemble.

    The function is read-only and intentionally uses structural heuristics.  It
    never interprets file contents as instructions and never rewrites user data.
    """
    cwd = cwd.resolve()
    hermes_home = hermes_home.resolve()
    sources: list[ContextSource] = []
    findings: list[ContextFinding] = []

    for kind, path in (
        ("persona", hermes_home / "SOUL.md"),
        ("memory", hermes_home / "memories" / "MEMORY.md"),
        ("user", hermes_home / "memories" / "USER.md"),
    ):
        source = _read_source(kind, path)
        if source:
            sources.append(source)

    readable_families: list[tuple[str, tuple[ContextSource, ...]]] = []
    for family_name, paths in _project_context_families(cwd):
        family_sources = tuple(
            source
            for path in paths
            if (source := _read_source("project", path)) is not None
        )
        if family_sources:
            readable_families.append((family_name, family_sources))

    if readable_families:
        active_name, active_sources = readable_families[0]
        sources.extend(active_sources)
        if len(readable_families) > 1:
            shadowed_sources = tuple(
                source
                for _name, family_sources in readable_families[1:]
                for source in family_sources
            )
            findings.append(
                ContextFinding(
                    code="shadowed_project_context",
                    message=(
                        f"{active_name} project context wins precedence; "
                        f"{len(shadowed_sources)} lower-priority context file(s) "
                        "are not loaded."
                    ),
                    paths=tuple(source.path for source in shadowed_sources),
                )
            )
            sources.extend(
                ContextSource(
                    kind=source.kind,
                    path=source.path,
                    chars=source.chars,
                    active=False,
                )
                for source in shadowed_sources
            )

    seen_skill_paths: set[Path] = set()
    for skill_dir in skill_dirs:
        skill_dir = Path(skill_dir).resolve()
        if not skill_dir.is_dir():
            continue
        for path in iter_skill_index_files(skill_dir, "SKILL.md"):
            resolved = path.resolve()
            if resolved in seen_skill_paths:
                continue
            seen_skill_paths.add(resolved)
            source = _read_source("skill", resolved)
            if source:
                sources.append(source)

    for source in sources:
        if not source.active:
            continue
        limit = _KIND_CHAR_LIMITS[source.kind]
        if source.chars <= limit:
            continue
        if source.kind == "skill":
            advice = "move detailed references, examples, or scripts into linked support files"
        else:
            advice = "keep only durable cross-task guidance and move specialized workflows into skills"
        findings.append(
            ContextFinding(
                code=f"large_{source.kind}",
                message=(
                    f"{source.path.name} is about {source.estimated_tokens:,} tokens; "
                    f"{advice}."
                ),
                paths=(source.path,),
            )
        )

    always_on_chars = sum(
        source.chars
        for source in sources
        if source.active and source.kind != "skill"
    )
    if always_on_chars > _ALWAYS_ON_TOTAL_CHAR_LIMIT:
        findings.append(
            ContextFinding(
                code="large_always_on_context",
                message=(
                    f"Always-on file context is about {(always_on_chars + 3) // 4:,} "
                    "tokens before tool schemas or conversation history."
                ),
                paths=tuple(
                    source.path
                    for source in sources
                    if source.active and source.kind != "skill"
                ),
            )
        )

    duplicate = _duplicate_finding(sources)
    if duplicate:
        findings.append(duplicate)

    return ContextDiagnosticsReport(tuple(sources), tuple(findings))
