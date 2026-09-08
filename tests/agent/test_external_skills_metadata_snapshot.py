"""External skill metadata persists without freezing session visibility."""
import json
import os
from pathlib import Path

from agent import prompt_builder as pb


def skill(root, name, description, extra=''):
    path = root / name / 'SKILL.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\nname: {name}\ndescription: {description}\n{extra}---\nBody\n', encoding='utf-8')
    return path


def test_disk_reuse_and_source_changes(tmp_path, monkeypatch):
    home, external = tmp_path / 'home', tmp_path / 'external'
    monkeypatch.setenv('HERMES_HOME', str(home))
    path = skill(external, 'alpha', 'Original')
    first = pb._external_skills_metadata(external)
    assert first['alpha/SKILL.md']['description'] == 'Original'
    with monkeypatch.context() as patch:
        patch.setattr(pb, 'parse_frontmatter', lambda text: (_ for _ in ()).throw(AssertionError('warm cache parsed YAML')))
        assert pb._external_skills_metadata(external) == first
    path.write_text('---\nname: alpha\ndescription: Changed\n---\n', encoding='utf-8')
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    skill(external, 'beta', 'Added')
    refreshed = pb._external_skills_metadata(external)
    assert refreshed['alpha/SKILL.md']['description'] == 'Changed'
    assert 'beta/SKILL.md' in refreshed
    path.unlink()
    assert 'alpha/SKILL.md' not in pb._external_skills_metadata(external)
    assert not list(external.rglob('*.json'))
    assert list((home / 'cache').glob('external-skills-*.json'))


def test_corrupt_cache_and_independent_profiles(tmp_path, monkeypatch):
    external = tmp_path / 'external'
    skill(external, 'alpha', 'Shared source')
    for name in ('first', 'second'):
        home = tmp_path / name
        monkeypatch.setenv('HERMES_HOME', str(home))
        assert pb._external_skills_metadata(external)['alpha/SKILL.md']['name'] == 'alpha'
        cache = next((home / 'cache').glob('external-skills-*.json'))
        cache.write_text('not-json', encoding='utf-8')
        assert pb._external_skills_metadata(external)['alpha/SKILL.md']['name'] == 'alpha'
        assert json.loads(cache.read_text())['directory'] == str(external.resolve())


def test_warm_prompt_rechecks_environment_and_local_precedence(tmp_path, monkeypatch):
    home, external = tmp_path / 'home', tmp_path / 'external'
    local = home / 'skills'
    monkeypatch.setenv('HERMES_HOME', str(home))
    skill(local, 'same', 'Local wins')
    skill(external, 'same', 'External loses')
    skill(external, 'worker_only', 'Worker skill', 'environments: [kanban]\n')
    monkeypatch.setattr(pb, 'get_disabled_skill_names', lambda *args: set())
    monkeypatch.setattr(pb, '_current_session_platform_hint', lambda: 'cli')
    def build():
        pb.clear_skills_system_prompt_cache()
        return pb._build_skills_system_prompt_inner(local, [external], None, None, None)
    monkeypatch.setattr(pb, 'skill_matches_environment', lambda fm: not fm.get('environments'))
    first = build()
    assert 'Local wins' in first and 'External loses' not in first
    assert 'worker_only' not in first
    monkeypatch.setattr(pb, 'skill_matches_environment', lambda fm: True)
    assert 'worker_only' in build()
