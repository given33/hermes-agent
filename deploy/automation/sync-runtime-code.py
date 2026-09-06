#!/usr/bin/env python3
"""Pull one GitHub commit and update code without copying instance profiles."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request

REPOSITORY = 'given33/hermes-agent'
PACKAGES = {'agent', 'gateway', 'hermes_cli', 'hermes_runtime', 'hermes_services',
            'tools', 'tui_gateway', 'providers', 'cron', 'acp_adapter', 'plugins'}
SUPPORT = {'pyproject.toml', 'uv.lock', 'README.md', 'LICENSE',
           'deploy/public/runtime-requirements.lock', 'deploy/dbb3/dbb3_cloud_connector.py',
           'deploy/automation/sync-runtime-code.py'}
NODES = {
    'hub': {'root': '/opt/hermes-agent', 'environment': '.venv',
            'units': ['hermes-agent', 'hermes-gateway', 'hermes-studio'], 'user_units': [],
            'homes': ['/var/lib/hermes-agent']},
    'dbb3': {'root': '/usr/local/lib/hermes-agent', 'environment': 'venv',
             'units': ['hermes-dashboard', 'hermes-gateway'], 'user_units': ['dbb3-cloud-connector'],
             'homes': ['/home/hermes/.hermes'], 'connector': '/opt/dbb3-team/dbb3_cloud_connector.py'},
    'wsl': {'root': '/mnt/d/Hermes/hermes-agent', 'environment': 'venv', 'units': [],
            'user_units': ['hermes-wsl-gateway', 'pc-cloud-connector'],
            'homes': ['/mnt/d/Hermes/home'], 'connector': '/opt/pc-team/pc_cloud_connector.py'},
    'hk': {'root': '/opt/hk-hermes', 'environment': '.venv', 'units': ['hermes-gateway-hk-worker'],
           'user_units': ['hk-cloud-connector'], 'homes': ['/var/lib/hermes-hk'],
           'connector': '/opt/hk-team/hk_cloud_connector.py'},
}


def code_member(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name and not path.is_absolute() and '..' not in path.parts
                and re.fullmatch(r'[A-Za-z0-9_.+/-]+', name)
                and not any(part in {'tests', '__pycache__', 'node_modules', '.env'} for part in path.parts)
                and not path.name.startswith('test_')
                and (path.parts[0] in PACKAGES or name in SUPPORT
                     or len(path.parts) == 1 and path.suffix == '.py'))


def request_json(url: str, headers: dict | None = None) -> dict:
    request = urllib.request.Request(url, headers={'User-Agent': 'Hermes-Runtime-Update', **(headers or {})})
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)


def release_target(role: str) -> dict:
    if role == 'hub':
        return {'commit': request_json(f'https://api.github.com/repos/{REPOSITORY}/commits/main')['sha']}
    family = {'dbb3': 'dbb3', 'wsl': 'pc', 'hk': 'hk'}[role]
    token_path = Path(f'/etc/{family}-team/cloud_connector_token')
    if token_path.is_symlink():
        raise ValueError('Connector credential path must not be a symlink')
    token = token_path.read_text().strip()
    payload = request_json('https://daxueshenmai.top/api/plugins/collaboration/connector/deployment-health',
                           {'Authorization': 'Bearer ' + token, 'X-Connector-ID': family + '-primary'})
    if payload.get('ok') is not True:
        raise RuntimeError('The hub has not published a healthy release')
    return payload['release']


def run(args: list[str], *, check: bool = True, **kwargs):
    return subprocess.run(args, check=check, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=kwargs.pop('timeout', 180), **kwargs)


def digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract_runtime(archive: Path, destination: Path) -> dict[str, str]:
    files = {}
    with tarfile.open(archive) as source:
        for member in source:
            parts = PurePosixPath(member.name).parts
            name = '/'.join(parts[1:])
            if not code_member(name):
                continue
            if not member.isfile():
                if member.isdir():
                    continue
                raise ValueError('Runtime archives cannot contain links: ' + name)
            target = destination / name
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError('Archive path escaped runtime directory')
            content = source.extractfile(member).read()
            if name.endswith('.py'):
                compile(content, name, 'exec')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(0o644)
            files[name] = hashlib.sha256(content).hexdigest()
    for required in ['run_agent.py', 'hermes_cli/main.py', 'pyproject.toml', 'deploy/public/runtime-requirements.lock']:
        if required not in files:
            raise ValueError('Incomplete runtime archive: ' + required)
    return files


def protected_config_hashes(homes: list[str]) -> dict[str, str]:
    result = {}
    for home in map(Path, homes):
        folders = [home, home / '.hermes']
        if (home / 'profiles').is_dir():
            folders += [path for path in (home / 'profiles').iterdir() if path.is_dir() and not path.is_symlink()]
        for folder in folders:
            for name in ['config.yaml', '.env', 'auth.json', 'SOUL.md', 'MEMORY.md']:
                path = folder / name
                if path.is_file():
                    result[str(path)] = digest_file(path)
    return result


def main():
    import fcntl
    import pwd
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role', choices=NODES)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('This system updater must run as root')
    node = NODES[args.role]
    root = Path(node['root'])
    if root.resolve() != root:
        raise ValueError('Runtime root must be canonical')
    state = Path('/var/lib/hermes-agent-fabric-update') / args.role
    state.mkdir(parents=True, exist_ok=True)
    with (state / 'runtime-update.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        target = release_target(args.role)
        commit = str(target.get('commit') or '')
        if not re.fullmatch('[0-9a-f]{40}', commit):
            raise ValueError('Release must identify a full Git commit')
        receipt_path = state / 'runtime-release.json'
        receipt = json.loads(receipt_path.read_text()) if receipt_path.is_file() else {}
        if receipt.get('commit') == commit and all(
            (root / name).is_file() and digest_file(root / name) == digest
            for name, digest in receipt.get('files', {}).items()
        ) and receipt.get('files'):
            print(json.dumps({'role': args.role, 'commit': commit, 'status': 'current'}))
            return
        ancestry = request_json(f'https://api.github.com/repos/{REPOSITORY}/compare/{commit}...main')
        if ancestry.get('status') not in {'ahead', 'identical'}:
            raise ValueError('Release is not part of the approved main branch')
        generation = root / '.fabric-generations' / commit
        if generation.resolve() != generation:
            raise ValueError('Release generation must not be a symlink')
        generation.mkdir(parents=True, exist_ok=True)
        archive = state / (commit + '.tar.gz')
        if not archive.exists():
            temporary = archive.with_suffix('.download')
            request = urllib.request.Request(f'https://codeload.github.com/{REPOSITORY}/tar.gz/{commit}',
                                             headers={'User-Agent': 'Hermes-Runtime-Update'})
            with urllib.request.urlopen(request, timeout=120) as response, temporary.open('wb') as output:
                shutil.copyfileobj(response, output)
            os.replace(temporary, archive)
        files = extract_runtime(archive, generation)
        version_match = re.search(r'^version\s*=\s*"([^"]+)"', (generation / 'pyproject.toml').read_text(), re.M)
        version = version_match.group(1) if version_match else ''
        if not version or target.get('version') and target['version'] != version:
            raise ValueError('GitHub code version does not match the hub release')
        environment = generation / '.venv'
        original = root / node['environment']
        if not environment.exists():
            shutil.copytree(original.resolve(), environment, symlinks=True)
        python = str(environment / 'bin/python')
        dependency_lock = generation / 'deploy/public/runtime-requirements.lock'
        ready = generation / '.dependencies-ready'
        if not ready.exists() or ready.read_text().strip() != digest_file(dependency_lock):
            if run([python, '-m', 'pip', '--version'], check=False).returncode:
                run([python, '-m', 'ensurepip', '--upgrade'])
            result = run([python, '-m', 'pip', 'install', '--disable-pip-version-check', '--require-hashes',
                          '--no-deps', '-r', str(dependency_lock)], timeout=600)
            (generation / '.dependency-install.log').write_text(result.stdout + result.stderr)
            run([python, '-m', 'pip', 'install', '--no-deps', '-e', str(generation)], timeout=180)
            ready.write_text(digest_file(dependency_lock))
        environment_owner = original.stat()
        for directory, subdirectories, filenames in os.walk(environment):
            os.chown(directory, environment_owner.st_uid, environment_owner.st_gid)
            for filename in subdirectories + filenames:
                os.chown(Path(directory) / filename, environment_owner.st_uid, environment_owner.st_gid,
                         follow_symlinks=False)
        import tempfile
        with tempfile.TemporaryDirectory(prefix='hermes-update-check-') as home:
            run([python, '-c', 'import hermes_cli.web_server, run_agent'], cwd=generation,
                env={**os.environ, 'PYTHONPATH': str(generation), 'HERMES_HOME': home}, timeout=90)
        print(json.dumps({'role': args.role, 'commit': commit, 'version': version, 'prepared': True}), flush=True)
        if args.check:
            return
        prefixes = [(['systemctl'], unit) for unit in node['units']]
        if node['user_units']:
            user = pwd.getpwnam('hermes')
            prefix = ['sudo', '-u', 'hermes', 'env', f'XDG_RUNTIME_DIR=/run/user/{user.pw_uid}', 'systemctl', '--user']
            prefixes += [(prefix, unit) for unit in node['user_units']]
        active = [(prefix, unit) for prefix, unit in prefixes if run(prefix + ['is-active', unit], check=False).stdout.strip() == 'active']
        if not active:
            raise RuntimeError('No active Hermes service found')
        backup = state / ('rollback-' + commit + '-' + str(time.time_ns()))
        backup.mkdir(mode=0o700)
        link_backup = root / '.runtime-backups' / backup.name
        link_backup.mkdir(parents=True, mode=0o700)
        changed, links = [], []
        for prefix, unit in active:
            run(prefix + ['stop', unit])
        # Gateways can persist auth while stopping. Fence the code transaction
        # after that graceful shutdown has finished.
        before = protected_config_hashes(node['homes'])
        report = {'commit': commit, 'version': version, 'files': files, 'configs': before, 'status': 'preparing'}
        try:
            pairs = [(generation / name, root / name) for name in files]
            for name in ['.hermes-source-commit', '.hermes-product-commit']:
                (generation / name).write_text(commit + '\n')
                pairs.append((generation / name, root / name))
            if node.get('connector'):
                pairs.append((generation / 'deploy/dbb3/dbb3_cloud_connector.py', Path(node['connector'])))
            for index, (source, destination) in enumerate(pairs):
                if not destination.resolve().is_relative_to(root) and str(destination) != node.get('connector'):
                    raise ValueError('Runtime file escapes the configured code directory')
                saved = backup / str(index)
                exists = destination.exists()
                if exists:
                    shutil.copy2(destination, saved)
                changed.append((destination, saved, exists))
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_name(destination.name + '.runtime-update')
                shutil.copy2(source, temporary)
                if exists:
                    previous = destination.stat()
                    os.chown(temporary, previous.st_uid, previous.st_gid)
                    temporary.chmod(previous.st_mode & 0o777)
                os.replace(temporary, destination)
            # Services run from the stable install root, which also owns the
            # separately built dashboard assets. The candidate import check
            # above uses the isolated generation; activation binds its venv
            # to the now-verified stable code path.
            run([python, '-m', 'pip', 'install', '--no-deps', '-e', str(root)], timeout=180)
            for name, destination in [(node['environment'], environment), ('.fabric-current', generation)]:
                link = root / name
                saved = link_backup / name
                existed = link.exists() or link.is_symlink()
                if existed:
                    link.rename(saved)
                links.append((link, saved, existed))
                link.symlink_to(destination, target_is_directory=True)
            after = protected_config_hashes(node['homes'])
            if after != before:
                affected = sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))
                raise RuntimeError('Instance configuration changed during code update: ' + ', '.join(affected))
            for name, digest in files.items():
                if digest_file(root / name) != digest:
                    raise RuntimeError('Runtime checksum mismatch: ' + name)
            for prefix, unit in reversed(active):
                run(prefix + ['start', unit])
            for prefix, unit in active:
                deadline = time.monotonic() + 90
                while run(prefix + ['is-active', unit], check=False).stdout.strip() != 'active':
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Updated service did not become active: ' + unit)
                    time.sleep(1)
            if args.role == 'hub':
                deadline = time.monotonic() + 90
                while True:
                    try:
                        # The dashboard mounts its health route under /api; /health is
                        # redirected through the authenticated UI shell and is not a
                        # reliable release verification endpoint.
                        with urllib.request.urlopen('http://127.0.0.2:9119/api/health', timeout=5) as response:
                            if response.status == 200:
                                break
                    except Exception:
                        if time.monotonic() >= deadline:
                            raise RuntimeError('Updated dashboard did not become healthy')
                    time.sleep(1)
            report['status'] = 'committed'
        except BaseException:
            for prefix, unit in active:
                run(prefix + ['stop', unit], check=False)
            for link, saved, existed in reversed(links):
                if link.is_symlink():
                    link.unlink()
                if existed:
                    saved.rename(link)
            for destination, saved, existed in reversed(changed):
                if existed:
                    shutil.copy2(saved, destination)
                elif destination.is_file():
                    destination.unlink()
            for prefix, unit in reversed(active):
                run(prefix + ['start', unit], check=False)
            report['status'] = 'rolled-back'
            raise
        finally:
            report['file_backups'] = [[str(path), str(saved), existed] for path, saved, existed in changed]
            report['link_backups'] = [[str(path), str(saved), existed] for path, saved, existed in links]
            (backup / 'transaction.json').write_text(json.dumps(report, indent=2))
        receipt_path.write_text(json.dumps(report, indent=2))
        evidence_path = (Path('/var/lib/hermes-agent-release/release-evidence.json') if args.role == 'hub'
                         else state / 'release.json')
        evidence = json.loads(evidence_path.read_text()) if evidence_path.is_file() else {}
        evidence.update({'schema': 'hermes.release-evidence.v1' if args.role == 'hub' else 'hermes.fabric-release.v1',
                         'phase': 'committed', 'node_id': args.role, 'commit': commit, 'version': version,
                         'deployed_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                         'runtime_sha256': hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()})
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = evidence_path.with_suffix('.new')
        temporary.write_text(json.dumps(evidence, indent=2))
        temporary.chmod(0o644)
        os.replace(temporary, evidence_path)
        print(json.dumps({'role': args.role, 'commit': commit, 'version': version,
                          'status': 'committed', 'config_unchanged': protected_config_hashes(node['homes']) == before}))


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as error:
        print('Runtime update failed: ' + (error.stderr or error.stdout or str(error))[-3000:], file=sys.stderr)
        raise SystemExit(1)
