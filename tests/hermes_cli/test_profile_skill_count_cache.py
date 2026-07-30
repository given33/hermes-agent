from __future__ import annotations

from hermes_cli import profiles


def _profile_with_skill(tmp_path, name: str):
    profile = tmp_path / name
    skill = profile / "skills" / "category" / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: test\n---\n", encoding="utf-8")
    return profile


def test_skill_count_cache_is_bounded_lru(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "_SKILL_COUNT_CACHE_MAX", 2)
    with profiles._SKILL_COUNT_CACHE_LOCK:
        profiles._SKILL_COUNT_CACHE.clear()
    first = _profile_with_skill(tmp_path, "first")
    second = _profile_with_skill(tmp_path, "second")
    third = _profile_with_skill(tmp_path, "third")

    try:
        assert profiles._count_skills(first) == 1
        assert profiles._count_skills(second) == 1
        assert profiles._count_skills(first) == 1
        assert profiles._count_skills(third) == 1

        assert list(profiles._SKILL_COUNT_CACHE) == [
            str(first / "skills"),
            str(third / "skills"),
        ]
    finally:
        with profiles._SKILL_COUNT_CACHE_LOCK:
            profiles._SKILL_COUNT_CACHE.clear()
