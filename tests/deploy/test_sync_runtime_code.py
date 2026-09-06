import importlib.util
import io
from pathlib import Path
import tarfile

import pytest


@pytest.fixture
def updater():
    path = Path(__file__).resolve().parents[2] / 'deploy/automation/sync-runtime-code.py'
    spec = importlib.util.spec_from_file_location('sync_runtime_code', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_archive_contains_code_but_never_instance_state(updater, tmp_path):
    archive = tmp_path / 'release.tar.gz'
    sources = {'run_agent.py': b'answer = 3973', 'hermes_cli/main.py': b'pass',
               'pyproject.toml': b'[project]\nversion = "0.21.0"',
               'deploy/public/runtime-requirements.lock': b'',
               '.env': b'PRIVATE', '.hermes-home/config.yaml': b'PRIVATE',
               'profiles/worker/SOUL.md': b'PRIVATE', 'workspace/result.txt': b'PRIVATE'}
    with tarfile.open(archive, 'w:gz') as output:
        for name, data in sources.items():
            member = tarfile.TarInfo('release/' + name)
            member.size = len(data)
            output.addfile(member, io.BytesIO(data))
    stage = tmp_path / 'stage'
    stage.mkdir()
    files = updater.extract_runtime(archive, stage)
    assert set(files) == set(sources) - {'.env', '.hermes-home/config.yaml', 'profiles/worker/SOUL.md', 'workspace/result.txt'}
    assert (stage / 'run_agent.py').read_bytes() == sources['run_agent.py']
    assert not (stage / 'profiles').exists()


def test_runtime_member_rejects_traversal_and_nested_secret_files(updater):
    for path in ['../tools/escape.py', '/tools/escape.py', 'tools/../../.env', 'plugins/example/.env']:
        assert updater.code_member(path) is False


def test_runtime_symlink_is_rejected_before_it_can_touch_a_profile(updater, tmp_path):
    archive = tmp_path / 'release.tar'
    with tarfile.open(archive, 'w') as output:
        member = tarfile.TarInfo('release/hermes_cli/config.py')
        member.type = tarfile.SYMTYPE
        member.linkname = '/home/hermes/.hermes/config.yaml'
        output.addfile(member)
    with pytest.raises(ValueError, match='cannot contain links'):
        updater.extract_runtime(archive, tmp_path / 'stage')


def test_each_machine_keeps_a_distinct_home_and_existing_profile_content(updater, tmp_path):
    homes = [path for node in updater.NODES.values() for path in node['homes']]
    assert len(homes) == len(set(homes)) == 4
    home = tmp_path / 'home'
    profile = home / 'profiles' / 'worker'
    profile.mkdir(parents=True)
    (profile / 'SOUL.md').write_text('Independent role')
    before = updater.protected_config_hashes([str(home)])
    assert before == {str(profile / 'SOUL.md'): updater.digest_file(profile / 'SOUL.md')}
    assert (profile / 'SOUL.md').read_text() == 'Independent role'
