import importlib.util
import io
import json
from pathlib import Path
import tarfile
import subprocess
import os
from types import SimpleNamespace

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
               'deploy/recovery/hermes-fabric-peer-watchdog.sh': b'#!/usr/bin/env bash\nexit 0\n',
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


def test_updates_wait_for_chat_or_real_worker_but_ignore_exited_processes(updater, tmp_path):
    home = tmp_path / 'home'
    state = home / 'collaboration/single.json'
    state.parent.mkdir(parents=True)
    processes = tmp_path / 'proc'
    (processes / '123').mkdir(parents=True)
    command = processes / '123/cmdline'
    command.write_bytes(b'')
    state.write_text(json.dumps({'conversations': [{'hosted_turns': {'one': {'status': 'running'}}}]}))
    assert updater.has_active_execution([str(home)], processes)
    state.write_text(json.dumps({'conversations': [{'hosted_turns': {'one': {'status': 'completed'}}}]}))
    assert not updater.has_active_execution([str(home)], processes)
    command.write_bytes(b'hermes\0--cli\0chat\0-q\0work kanban task t_1\0-Q\0')
    assert updater.has_active_execution([str(home)], processes)


def test_current_github_release_does_not_depend_on_rest_api_quota(updater, monkeypatch):
    commit = 'a' * 40
    monkeypatch.setattr(updater, 'run', lambda *args, **kwargs: SimpleNamespace(stdout=commit + '\trefs/heads/main\n'))
    monkeypatch.setattr(updater, 'request_json', lambda *args: pytest.fail('REST quota must not block current main'))
    assert updater.release_target('hub') == {'commit': commit}
    updater.verify_approved_commit(commit)
    monkeypatch.setattr(updater, 'run', lambda *args, **kwargs: SimpleNamespace(stdout=commit + '\trefs/heads/other\n'))
    with pytest.raises(ValueError, match='approved main'):
        updater.github_main_commit()


def test_runtime_symlink_is_rejected_before_it_can_touch_a_profile(updater, tmp_path):
    archive = tmp_path / 'release.tar'
    with tarfile.open(archive, 'w') as output:
        member = tarfile.TarInfo('release/hermes_cli/config.py')
        member.type = tarfile.SYMTYPE
        member.linkname = '/home/hermes/.hermes/config.yaml'
        output.addfile(member)
    with pytest.raises(ValueError, match='cannot contain links'):
        updater.extract_runtime(archive, tmp_path / 'stage')


def test_github_git_timeout_uses_independent_verified_ref_api(updater, monkeypatch):
    commit = 'b' * 40
    def unavailable(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs['timeout'])
    monkeypatch.setattr(updater, 'run', unavailable)
    monkeypatch.setattr(updater, 'request_json', lambda url: {
        'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': commit},
    })
    updater.verify_approved_commit(commit)
    monkeypatch.setattr(updater, 'request_json', lambda url: {
        'ref': 'refs/heads/unapproved', 'object': {'type': 'commit', 'sha': commit},
    })
    with pytest.raises(ValueError, match='approved main branch'):
        updater.github_main_commit()


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


def test_same_release_repairs_evidence_without_a_deployment(updater, tmp_path):
    root = tmp_path / 'runtime'
    root.mkdir()
    (root / 'run_agent.py').write_text('pass')
    receipt = {'status': 'committed', 'commit': 'a' * 40, 'version': 'test',
               'files': {'run_agent.py': updater.digest_file(root / 'run_agent.py')}}
    for role in updater.NODES:
        assert updater.runtime_is_current(root, receipt, 'a' * 40)
        if role == 'hub':
            continue
        state = tmp_path / role
        updater.publish_release(role, state, receipt)
        first = (state / 'release.json').stat().st_mtime_ns
        updater.publish_release(role, state, receipt)
        assert (state / 'release.json').stat().st_mtime_ns == first
    (root / 'run_agent.py').write_text('changed')
    assert not updater.runtime_is_current(root, receipt, 'a' * 40)
    assert not updater.runtime_is_current(root, {'status': 'committed', 'commit': 'a' * 40, 'files': {}}, 'a' * 40)


def test_backup_retention_keeps_current_rollback_and_unrelated_data(updater, tmp_path):
    root, state = tmp_path / 'runtime', tmp_path / 'state'
    for parent in (state, root / '.runtime-backups'):
        parent.mkdir(parents=True)
        for index in range(7):
            folder = parent / ('rollback-' + 'a' * 40 + '-' + str(index))
            folder.mkdir()
            (folder / 'code').write_text('backup')
        (parent / 'profile').mkdir()
    protected = 'rollback-' + 'a' * 40 + '-0'
    receipt = {'file_backups': [['run_agent.py', str(state / protected / 'code'), True]]}
    removed = updater.prune_backups(root, state, receipt)
    assert len(removed) == 6
    for parent in (state, root / '.runtime-backups'):
        assert (parent / protected).is_dir()
        assert (parent / 'profile').is_dir()
        assert len(list(parent.iterdir())) == 5
    assert updater.prune_backups(root, state, receipt) == []


def test_code_only_update_reuses_only_a_verified_dependency_environment(updater, tmp_path):
    environment = tmp_path / 'generation' / '.venv'
    (environment / 'bin').mkdir(parents=True)
    (environment / 'bin/python').write_text('python')
    lock = tmp_path / 'requirements.lock'
    lock.write_text('same dependencies')
    assert not updater.reusable_environment(environment, lock)
    ready = environment.parent / '.dependencies-ready'
    ready.write_text(updater.digest_file(lock))
    assert updater.reusable_environment(environment, lock)
    lock.write_text('new dependencies')
    assert not updater.reusable_environment(environment, lock)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX runtime symlinks are exercised on Linux')
def test_retention_reclaims_archives_and_code_without_removing_live_environment(updater, tmp_path):
    root, state, generations = tmp_path / 'root', tmp_path / 'state', tmp_path / 'native-code'
    for parent in (root, state, generations):
        parent.mkdir()
    commits = [str(index) * 40 for index in range(1, 8)]
    for index, commit in enumerate(commits):
        archive = state / (commit + '.tar.gz')
        archive.write_bytes(b'archive')
        generation = generations / commit
        generation.mkdir()
        (generation / 'code.py').write_text('pass')
        os.utime(archive, (index, index))
        os.utime(generation, (index, index))
    # A real environment belongs to an old generation and is shared by the
    # current one. A still-live older code generation must also survive.
    environment = generations / commits[0] / '.venv'
    environment.mkdir()
    (environment / 'python').write_text('runtime')
    os.utime(generations / commits[0], (0, 0))
    (root / 'venv').symlink_to(environment, target_is_directory=True)
    (root / '.fabric-current').symlink_to(generations / commits[1], target_is_directory=True)
    (state / 'user-upload.tar.gz').write_bytes(b'user data')
    receipt = {'commit': commits[-1]}
    updater.prune_backups(root, state, receipt, generation_root=generations)
    assert (environment / 'python').read_text() == 'runtime'
    assert (root / '.fabric-current' / 'code.py').is_file()
    assert not (generations / commits[2]).exists()
    assert not (generations / commits[3]).exists()
    assert len(list(state.glob('*.tar.gz'))) == 4  # 3 releases + unrelated upload
    assert (state / 'user-upload.tar.gz').read_bytes() == b'user data'
