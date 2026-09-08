from hermes_constants import (
    hermes_home_key, hermes_home_resolution_scope,
    set_hermes_home_override, reset_hermes_home_override,
)


def test_scoped_resolution_keeps_nested_profiles_distinct(monkeypatch, tmp_path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(first))
    first_key, second_key = hermes_home_key(first), hermes_home_key(second)
    with hermes_home_resolution_scope():
        assert hermes_home_key() == first_key
        token = set_hermes_home_override(second)
        try:
            assert hermes_home_key() == second_key
            with hermes_home_resolution_scope():
                assert hermes_home_key(first) == first_key
                assert hermes_home_key() == second_key
        finally:
            reset_hermes_home_override(token)
        assert hermes_home_key() == first_key
    monkeypatch.setenv('HERMES_HOME', str(second))
    assert hermes_home_key() == second_key


def test_next_operation_observes_retargeted_profile_symlink(monkeypatch, tmp_path):
    import pytest
    first, second, link = tmp_path / 'first', tmp_path / 'second', tmp_path / 'profile'
    first.mkdir()
    second.mkdir()
    try:
        link.symlink_to(first, target_is_directory=True)
    except OSError:
        pytest.skip('Symlink creation unavailable')
    monkeypatch.setenv('HERMES_HOME', str(link))
    with hermes_home_resolution_scope():
        assert hermes_home_key() == hermes_home_key(first)
    link.unlink()
    link.symlink_to(second, target_is_directory=True)
    with hermes_home_resolution_scope():
        assert hermes_home_key() == hermes_home_key(second)
