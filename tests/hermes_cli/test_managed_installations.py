import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

from hermes_cli import managed_installations
from hermes_cli.managed_installations import (
    accept_managed_installation,
    create_managed_installation,
    dispatch_managed_installations_once,
    get_managed_installation,
    get_received_managed_installation,
    resolve_installation_targets,
)
from hermes_cli.managed_node_recovery_service import RecoveryHTTPServer


INSTALL_TOKEN = "installation-private-token-0000000000000001"
STATUS_TOKEN = "status-private-token-00000000000000000001"


def _wait_for_http_server(url: str, headers: dict[str, str] | None = None) -> None:
    deadline = time.monotonic() + 3
    while True:
        try:
            urlopen(Request(url, headers=headers or {}), timeout=0.5).close()
            return
        except HTTPError:
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def test_installation_policy_resolves_skill_mcp_and_project_targets():
    assert resolve_installation_targets("skill") == ["server", "dbb3", "wsl"]
    assert resolve_installation_targets("mcp", locality="network") == [
        "server", "dbb3", "wsl",
    ]
    assert resolve_installation_targets("mcp", locality="server") == ["server"]
    assert resolve_installation_targets("project") == ["dbb3", "wsl"]
    assert resolve_installation_targets("project", targets=["wsl"]) == ["wsl"]
    with pytest.raises(ValueError, match="explicit targets"):
        resolve_installation_targets("mcp", locality="node")


def test_installation_creation_is_idempotent_and_each_target_is_durable(tmp_path):
    db = tmp_path / "installations.db"
    first = create_managed_installation(
        kind="skill",
        identifier="official/example",
        request_id="mobile-request-1",
        db_path=db,
    )
    replay = create_managed_installation(
        kind="skill",
        identifier="official/example",
        request_id="mobile-request-1",
        db_path=db,
    )

    assert first["id"] == replay["id"]
    assert first["state"] == "accepted"
    assert [target["node_id"] for target in first["targets"]] == [
        "server", "dbb3", "wsl",
    ]
    with pytest.raises(ValueError, match="already bound"):
        create_managed_installation(
            kind="mcp",
            identifier="github",
            request_id="mobile-request-1",
            db_path=db,
        )


def test_server_install_uses_allowlisted_argv_and_reaches_terminal_state(tmp_path):
    db = tmp_path / "installations.db"
    calls = []
    operation = create_managed_installation(
        kind="skill",
        identifier="official/example",
        request_id="server-install",
        targets=["server"],
        db_path=db,
    )

    def executor(command, *, timeout):
        calls.append((command, timeout))
        return "installed"

    assert dispatch_managed_installations_once(db_path=db, executor=executor) is True
    current = get_managed_installation(operation["id"], db_path=db)
    assert current["state"] == "completed"
    assert current["targets"][0]["state"] == "completed"
    assert calls[0][0][-4:] == ["skills", "install", "official/example", "--yes"]


def test_skill_install_proof_is_computed_from_the_installed_profile_tree(tmp_path):
    profile_home = tmp_path / "profile"

    def executor(command, *, timeout):
        assert timeout == managed_installations.DEFAULT_COMMAND_TIMEOUT_SECONDS
        destination = profile_home / "skills" / "development" / "example"
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text("# Example\n", encoding="utf-8")
        (destination / "helper.txt").write_text("real content\n", encoding="utf-8")
        lock = profile_home / "skills" / ".hub" / "lock.json"
        lock.parent.mkdir(parents=True)
        lock.write_text(json.dumps({
            "version": 1,
            "installed": {
                "example": {
                    "identifier": "official/example",
                    "install_path": "development/example",
                    "content_hash": "untrusted-lock-value",
                }
            },
        }), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="installed\n", stderr="")

    detail = managed_installations._execute_allowlisted_installation(
        {
            "kind": "skill",
            "identifier": "official/example",
            "profile": "default",
            "_profile_home": str(profile_home),
        },
        executor=executor,
    )

    assert detail["proof_schema"] == 1
    assert detail["proof_source"] == "local_filesystem"
    assert detail["content_hash"] == managed_installations._hash_resource_tree(
        profile_home / "skills" / "development" / "example"
    )
    assert detail["content_hash"] != "untrusted-lock-value"


def test_mcp_install_proof_verifies_local_config_against_catalog_manifest(
    tmp_path, monkeypatch
):
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("manifest_version: 1\nname: demo\n", encoding="utf-8")
    entry = SimpleNamespace(
        name="demo",
        manifest_path=manifest,
        install=None,
        transport=SimpleNamespace(version="1.2.3"),
    )
    monkeypatch.setattr("hermes_cli.mcp_catalog.get_entry", lambda _name: entry)
    monkeypatch.setattr(
        "hermes_cli.mcp_catalog._build_server_config",
        lambda _entry, _install_dir: {"url": "https://mcp.example.test"},
    )

    def executor(command, *, timeout):
        (profile_home / "config.yaml").write_text(
            "mcp_servers:\n  demo:\n    url: https://mcp.example.test\n    enabled: true\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="installed\n", stderr="")

    detail = managed_installations._execute_allowlisted_installation(
        {
            "kind": "mcp",
            "identifier": "demo",
            "profile": "default",
            "_profile_home": str(profile_home),
        },
        executor=executor,
    )

    assert detail["proof_source"] == "local_filesystem"
    assert detail["resolved_version"] == "1.2.3"
    assert detail["content_hash"] == managed_installations.hashlib.sha256(
        manifest.read_bytes()
    ).hexdigest()


def _receiver_config(tmp_path: Path, token_path: Path, installation_url: str = "") -> Path:
    token_path.chmod(0o600)
    payload = {
        "nodes": [],
        "installation_receiver": {
            "node_id": "dbb3",
            "token_file": str(token_path),
            "state_file": "node-installations.db",
            "project_root": "projects",
        },
    }
    if installation_url:
        status_token = tmp_path / "status-token"
        status_token.write_text(STATUS_TOKEN, encoding="utf-8")
        installation_token = tmp_path / "installation-token"
        installation_token.write_text(token_path.read_text(encoding="utf-8"), encoding="utf-8")
        status_token.chmod(0o600)
        installation_token.chmod(0o600)
        payload["nodes"] = [{
            "id": "private-fleet",
            "status_url": "https://status.example/live",
            "token_file": str(status_token),
            "installation_token_file": str(installation_token),
            "installation_urls": {"dbb3": installation_url},
        }]
    path = tmp_path / "managed-nodes.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _main_managed_nodes_config(tmp_path: Path, monkeypatch) -> Path:
    from hermes_cli import managed_nodes

    status_token = tmp_path / "main-status-token"
    installation_token = tmp_path / "main-installation-token"
    status_token.write_text(STATUS_TOKEN, encoding="utf-8")
    installation_token.write_text(INSTALL_TOKEN, encoding="utf-8")
    status_token.chmod(0o600)
    installation_token.chmod(0o600)
    path = tmp_path / "managed-nodes.json"
    path.write_text(json.dumps({
        "nodes": [{
            "id": "private-fleet",
            "status_url": "https://status.example/live",
            "token_file": str(status_token),
            "installation_token_file": str(installation_token),
            "installation_urls": {
                "dbb3": "https://install.example/dbb3",
                "wsl": "https://install.example/wsl",
            },
        }],
    }), encoding="utf-8")
    monkeypatch.setattr(managed_nodes, "managed_nodes_config_path", lambda: path)
    return path


def test_receiver_authenticates_and_deduplicates_concurrent_delivery(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text(INSTALL_TOKEN, encoding="utf-8")
    config = _receiver_config(tmp_path, token)
    started = threading.Event()
    release = threading.Event()
    calls = []

    def execute(operation, **_kwargs):
        calls.append(operation["identifier"])
        started.set()
        assert release.wait(5)
        return {"installed": True}

    monkeypatch.setattr(managed_installations, "_execute_allowlisted_installation", execute)
    payload = {
        "id": "fleet-operation-1",
        "request_id": "mobile-request-1",
        "node_id": "dbb3",
        "kind": "skill",
        "identifier": "official/example",
        "profile": "default",
    }
    first = accept_managed_installation(payload, INSTALL_TOKEN, config)
    assert started.wait(5)
    replay = accept_managed_installation(payload, INSTALL_TOKEN, config)
    with pytest.raises(PermissionError):
        accept_managed_installation(payload, "wrong-token", config)
    release.set()

    deadline = time.monotonic() + 5
    current = None
    while time.monotonic() < deadline:
        current = get_received_managed_installation(
            "fleet-operation-1", INSTALL_TOKEN, config,
        )
        if current["state"] == "completed":
            break
        time.sleep(0.02)

    assert first["accepted"] is True and replay["accepted"] is True
    assert current is not None and current["state"] == "completed"
    assert calls == ["official/example"]


def test_receiver_persists_transient_failure_and_retries_to_completion(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text(INSTALL_TOKEN, encoding="utf-8")
    config = _receiver_config(tmp_path, token)
    calls = []

    def execute(operation, **_kwargs):
        calls.append(operation["identifier"])
        if len(calls) == 1:
            raise OSError("temporary network failure")
        return {"installed": True}

    monkeypatch.setattr(managed_installations, "_execute_allowlisted_installation", execute)
    accept_managed_installation({
        "id": "fleet-retry-1",
        "request_id": "mobile-retry-1",
        "node_id": "dbb3",
        "kind": "skill",
        "identifier": "official/example",
        "profile": "default",
    }, INSTALL_TOKEN, config)

    deadline = time.monotonic() + 6
    current = None
    while time.monotonic() < deadline:
        current = get_received_managed_installation("fleet-retry-1", INSTALL_TOKEN, config)
        if current["state"] == "completed":
            break
        time.sleep(0.02)

    assert current is not None and current["state"] == "completed"
    assert calls == ["official/example", "official/example"]


def test_main_dispatches_to_authenticated_node_and_polls_completion(tmp_path, monkeypatch):
    from hermes_cli import managed_node_recovery_service

    token = tmp_path / "token"
    token.write_text(INSTALL_TOKEN, encoding="utf-8")
    placeholder = _receiver_config(tmp_path, token)
    receiver_entered = threading.Event()
    release_receiver = threading.Event()
    receiver_completed = threading.Event()

    def execute(operation, **_kwargs):
        receiver_entered.set()
        assert release_receiver.wait(5)
        return {"installed": True, "artifact": operation["identifier"]}

    monkeypatch.setattr(
        managed_installations,
        "_execute_allowlisted_installation",
        execute,
    )
    finish_target = managed_installations._finish_target

    def observe_receiver_completion(path, claimed, **kwargs):
        updated = finish_target(path, claimed, **kwargs)
        if Path(path).name == "node-installations.db" and kwargs["state"] == "completed":
            receiver_completed.set()
        return updated

    monkeypatch.setattr(
        managed_installations,
        "_finish_target",
        observe_receiver_completion,
    )
    get_received = managed_node_recovery_service.get_received_managed_installation
    stale_get_observed = threading.Event()

    def complete_receiver_after_get(*args, **kwargs):
        result = get_received(*args, **kwargs)
        if result["state"] == "running" and not stale_get_observed.is_set():
            stale_get_observed.set()
            release_receiver.set()
            assert receiver_completed.wait(5)
        return result

    monkeypatch.setattr(
        managed_node_recovery_service,
        "get_received_managed_installation",
        complete_receiver_after_get,
    )
    server = RecoveryHTTPServer(("127.0.0.1", 0), placeholder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = _receiver_config(
            tmp_path,
            token,
            f"http://127.0.0.1:{server.server_port}/installations",
        )
        main_db = tmp_path / "main-installations.db"
        operation = create_managed_installation(
            kind="mcp",
            identifier="github",
            request_id="remote-mcp-1",
            targets=["dbb3"],
            db_path=main_db,
        )
        assert dispatch_managed_installations_once(db_path=main_db, config_path=config) is True
        assert receiver_entered.wait(5)
        with managed_installations.closing(managed_installations._connect(main_db)) as conn:
            conn.execute("UPDATE managed_installation_targets SET next_attempt_at = 0")

        assert dispatch_managed_installations_once(db_path=main_db, config_path=config) is True
        assert stale_get_observed.is_set()
        assert receiver_completed.is_set()
        current = get_managed_installation(operation["id"], db_path=main_db)
        assert current["targets"][0]["state"] == "running"
        with managed_installations.closing(managed_installations._connect(main_db)) as conn:
            next_attempt_at = float(conn.execute(
                "SELECT next_attempt_at FROM managed_installation_targets "
                "WHERE operation_id = ? AND node_id = 'dbb3'",
                (operation["id"],),
            ).fetchone()[0])
            assert next_attempt_at - time.time() <= (
                managed_installations.REMOTE_POLL_INTERVAL_SECONDS
            )
            conn.execute("UPDATE managed_installation_targets SET next_attempt_at = 0")

        assert dispatch_managed_installations_once(db_path=main_db, config_path=config) is True
        current = get_managed_installation(operation["id"], db_path=main_db)
    finally:
        release_receiver.set()
        server.shutdown()
        server.server_close()

    assert current["state"] == "completed"
    assert current["targets"][0]["node_id"] == "dbb3"
    assert current["targets"][0]["state"] == "completed"


def test_receiver_http_routes_are_hidden_when_capability_is_not_configured(tmp_path):
    token = tmp_path / "token"
    token.write_text(INSTALL_TOKEN, encoding="utf-8")
    token.chmod(0o600)
    recovery_config = tmp_path / "recovery-only.json"
    recovery_config.write_text(json.dumps({
        "nodes": [],
        "recovery_receiver": {
            "node_id": "wsl",
            "token_file": str(token),
            "command": ["true"],
        },
    }), encoding="utf-8")
    recovery_server = RecoveryHTTPServer(("127.0.0.1", 0), recovery_config)
    recovery_thread = threading.Thread(
        target=recovery_server.serve_forever,
        daemon=True,
    )
    recovery_thread.start()
    try:
        base = f"http://127.0.0.1:{recovery_server.server_port}"
        _wait_for_http_server(f"{base}/health")
        for request in (
            Request(
                f"{base}/installations",
                data=b"{}",
                headers={"X-DBB3-Token": INSTALL_TOKEN},
                method="POST",
            ),
            Request(
                f"{base}/installations/mi-hidden",
                headers={"X-DBB3-Token": INSTALL_TOKEN},
                method="GET",
            ),
        ):
            with pytest.raises(HTTPError) as exc:
                urlopen(request, timeout=2)
            assert exc.value.code == 404
    finally:
        recovery_server.shutdown()
        recovery_server.server_close()

    installation_config = _receiver_config(tmp_path, token)
    installation_server = RecoveryHTTPServer(("127.0.0.1", 0), installation_config)
    installation_thread = threading.Thread(
        target=installation_server.serve_forever,
        daemon=True,
    )
    installation_thread.start()
    try:
        _wait_for_http_server(
            f"http://127.0.0.1:{installation_server.server_port}/health",
            {"X-DBB3-Token": INSTALL_TOKEN},
        )
        request = Request(
            f"http://127.0.0.1:{installation_server.server_port}/recover",
            data=b"{}",
            headers={"X-DBB3-Token": INSTALL_TOKEN},
            method="POST",
        )
        with pytest.raises(HTTPError) as exc:
            urlopen(request, timeout=2)
        assert exc.value.code == 404
    finally:
        installation_server.shutdown()
        installation_server.server_close()


def test_installation_health_requires_dedicated_token_and_has_no_side_effect(tmp_path):
    token = tmp_path / "token"
    token.write_text(INSTALL_TOKEN, encoding="utf-8")
    config = _receiver_config(tmp_path, token)
    server = RecoveryHTTPServer(("127.0.0.1", 0), config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/health"
        _wait_for_http_server(url, {"X-DBB3-Token": INSTALL_TOKEN})
        with pytest.raises(HTTPError) as exc:
            urlopen(Request(url), timeout=2)
        assert exc.value.code == 401
        with pytest.raises(HTTPError) as exc:
            urlopen(Request(url, headers={"X-DBB3-Token": STATUS_TOKEN}), timeout=2)
        assert exc.value.code == 401
        response = json.load(urlopen(Request(
            url, headers={"X-DBB3-Token": INSTALL_TOKEN},
        ), timeout=2))
        assert response == {
            "ok": True,
            "node_id": "dbb3",
            "recovery": False,
            "installations": True,
        }
        with sqlite3.connect(tmp_path / "node-installations.db") as conn:
            assert conn.execute("SELECT COUNT(*) FROM managed_installations").fetchone()[0] == 0
    finally:
        server.shutdown()
        server.server_close()


def test_authenticated_probe_post_persists_and_get_reads_same_terminal_operation(tmp_path):
    token = tmp_path / "token"
    token.write_text(INSTALL_TOKEN, encoding="utf-8")
    config = _receiver_config(tmp_path, token)
    server = RecoveryHTTPServer(("127.0.0.1", 0), config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    probe_id = "mi-" + "d" * 32
    headers = {
        "X-DBB3-Token": INSTALL_TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        _wait_for_http_server(f"{base}/health", {"X-DBB3-Token": INSTALL_TOKEN})
        body = json.dumps({
            "id": probe_id,
            "request_id": probe_id,
            "node_id": "dbb3",
            "kind": managed_installations.PROBE_KIND,
            "identifier": managed_installations.PROBE_IDENTIFIER,
            "probe": True,
        }).encode("utf-8")
        for _ in range(2):
            posted = json.load(urlopen(Request(
                f"{base}/installations", data=body, headers=headers, method="POST",
            ), timeout=2))
            assert posted["accepted"] is True
            assert posted["id"] == probe_id
        fetched = json.load(urlopen(Request(
            f"{base}/installations/{probe_id}", headers=headers,
        ), timeout=2))
        assert fetched["state"] == "completed"
        assert fetched["detail"] == {
            "probe": True,
            "persisted": True,
            "node_id": "dbb3",
        }
        with sqlite3.connect(tmp_path / "node-installations.db") as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM managed_installations WHERE request_id = ?",
                (probe_id,),
            ).fetchone()[0] == 1
    finally:
        server.shutdown()
        server.server_close()


def test_remote_installation_route_prefers_dedicated_token(tmp_path):
    status_token = tmp_path / "status-token"
    installation_token = tmp_path / "installation-token"
    status_token.write_text(STATUS_TOKEN, encoding="utf-8")
    installation_token.write_text(INSTALL_TOKEN, encoding="utf-8")
    status_token.chmod(0o600)
    installation_token.chmod(0o600)
    config = tmp_path / "managed-nodes.json"
    config.write_text(json.dumps({
        "nodes": [{
            "id": "fabric",
            "status_url": "https://status.example/live",
            "token_file": str(status_token),
            "installation_token_file": str(installation_token),
            "installation_urls": {
                "dbb3": "https://install.example/dbb3",
            },
        }],
    }), encoding="utf-8")

    route = managed_installations._installation_route("dbb3", config)

    assert route["url"] == "https://install.example/dbb3"
    assert route["token"] == INSTALL_TOKEN


def test_remote_installation_route_rejects_missing_or_reused_dedicated_token(tmp_path):
    status_token = tmp_path / "status-token"
    status_token.write_text(STATUS_TOKEN, encoding="utf-8")
    status_token.chmod(0o600)

    for installation_token in (None, str(status_token)):
        config = tmp_path / f"managed-nodes-{installation_token is None}.json"
        node = {
            "id": "fabric",
            "status_url": "https://status.example/live",
            "token_file": str(status_token),
            "installation_urls": {"dbb3": "https://install.example/dbb3"},
        }
        if installation_token is not None:
            node["installation_token_file"] = installation_token
        config.write_text(json.dumps({"nodes": [node]}), encoding="utf-8")

        with pytest.raises(ValueError, match="installation_token_file"):
            managed_installations._installation_route("dbb3", config)


def test_project_source_rejects_local_paths_and_shell_fragments(tmp_path):
    with pytest.raises(ValueError, match="HTTPS"):
        create_managed_installation(
            kind="project",
            identifier="C:/private/project",
            request_id="bad-project",
            db_path=tmp_path / "state.db",
        )
    with pytest.raises(ValueError):
        create_managed_installation(
            kind="skill",
            identifier="official/example; rm -rf /",
            request_id="bad-skill",
            db_path=tmp_path / "state.db",
        )
    with pytest.raises(ValueError, match="must not contain credentials"):
        create_managed_installation(
            kind="skill",
            identifier="https://token@example.com/catalog",
            request_id="credential-url",
            db_path=tmp_path / "state.db",
        )
    with pytest.raises(ValueError, match="catalog name"):
        create_managed_installation(
            kind="mcp",
            identifier="https://example.com/mcp.json",
            request_id="mcp-url",
            db_path=tmp_path / "state.db",
        )


@pytest.mark.parametrize(
    "identifier",
    [
        "https://127.0.0.1/private.git",
        "https://[::1]/private.git",
        "https://169.254.169.254/latest/meta-data.git",
        "https://10.0.0.1/private.git",
        "https://github.com:8443/example/demo.git",
        "https://github.com%2f@127.0.0.1/private.git",
        "https://github.com.evil.example/example/demo.git",
    ],
)
def test_managed_source_rejects_private_encoded_or_unapproved_targets(identifier):
    with pytest.raises(ValueError):
        managed_installations._normalize_identifier("project", identifier)


def test_runtime_source_host_extension_cannot_expand_trust_root(monkeypatch):
    monkeypatch.setenv("HERMES_MANAGED_SOURCE_HOSTS", "git.example.test")
    source = "https://git.example.test/example/demo.git"
    with pytest.raises(ValueError, match="not approved"):
        managed_installations._normalize_identifier("project", source)


def test_receiver_rejects_persisted_non_builtin_source(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_MANAGED_SOURCE_HOSTS", "git.example.test")
    source = "https://git.example.test/example/demo.git"
    operation = {
        "id": "persisted-untrusted-source",
        "kind": "project",
        "identifier": source,
        "profile": "default",
        "owner_id": "server-admin",
        "account_generation": "",
    }
    commands = []

    with pytest.raises(ValueError, match="not approved"):
        managed_installations._execute_allowlisted_installation(
            operation,
            executor=lambda command, *, timeout: commands.append(command),
            project_root=tmp_path / "projects",
        )
    assert commands == []


def test_nested_git_processes_drop_inherited_config_and_enforce_https(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "attacker-global")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "attacker-system")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "url.https://attacker.invalid/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "http.sslVerify")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "false")
    monkeypatch.setenv("GIT_SSL_NO_VERIFY", "1")

    environment = managed_installations._hardened_command_environment()
    command = managed_installations._managed_git_command(
        "clone", "https://github.com/example/demo.git", "destination",
    )

    assert not any(key.startswith("GIT_CONFIG_KEY_") for key in environment)
    assert not any(key.startswith("GIT_CONFIG_VALUE_") for key in environment)
    assert "GIT_CONFIG_COUNT" not in environment
    assert "GIT_SSL_NO_VERIFY" not in environment
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_CONFIG_SYSTEM"] == os.devnull
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GIT_ALLOW_PROTOCOL"] == "https"
    assert command[:1] == ["git"]
    assert command[1:-3] == [
        "-c", "http.followRedirects=false",
        "-c", "http.sslVerify=true",
        "-c", "credential.helper=",
        "-c", f"core.hooksPath={os.devnull}",
        "-c", "protocol.file.allow=never",
        "-c", "protocol.ext.allow=never",
        "-c", "protocol.git.allow=never",
        "-c", "protocol.ssh.allow=never",
        "-c", "protocol.http.allow=never",
        "-c", "protocol.https.allow=always",
    ]


def test_fenced_runner_drops_inherited_git_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "url.https://attacker.invalid/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/")
    monkeypatch.setenv("GIT_SSL_NO_VERIFY", "1")
    monkeypatch.setenv("git_config_key_9", "http.sslVerify")
    captured = tmp_path / "child-environment.json"
    program = (
        "import json, os, pathlib; "
        "pathlib.Path(os.sys.argv[1]).write_text("
        "json.dumps({key: value for key, value in os.environ.items() "
        "if key.startswith('GIT_')}), encoding='utf-8')"
    )

    managed_installations._run_command_fenced(
        [sys.executable, "-c", program, str(captured)],
        timeout=5,
        ownership_guard=lambda: None,
        fence=None,
    )

    child_environment = json.loads(captured.read_text(encoding="utf-8"))
    assert child_environment == {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "https",
    }


def test_failure_budget_counts_failures_not_successful_remote_polls(tmp_path, monkeypatch):
    db = tmp_path / "polls.db"
    operation = create_managed_installation(
        kind="mcp",
        identifier="github",
        request_id="long-remote-install",
        targets=["dbb3"],
        db_path=db,
    )
    responses = iter([{"state": "running"}] * 10 + [{"state": "completed"}])
    monkeypatch.setattr(
        managed_installations,
        "_dispatch_or_poll_remote",
        lambda *_args, **_kwargs: next(responses),
    )
    for _ in range(11):
        with managed_installations.closing(managed_installations._connect(db)) as conn:
            conn.execute("UPDATE managed_installation_targets SET next_attempt_at = 0")
        assert dispatch_managed_installations_once(db_path=db) is True

    current = get_managed_installation(operation["id"], db_path=db)
    assert current["state"] == "completed"
    assert current["targets"][0]["attempts"] == 11
    assert current["targets"][0]["failure_count"] == 0


def test_dispatch_retries_eight_consecutive_failures_then_stops(tmp_path):
    db = tmp_path / "failures.db"
    operation = create_managed_installation(
        kind="skill",
        identifier="official/example",
        request_id="retry-budget",
        targets=["server"],
        db_path=db,
    )

    def fail(_command, *, timeout):
        raise RuntimeError("api_key=do-not-leak upstream unavailable")

    for expected in range(1, 9):
        with managed_installations.closing(managed_installations._connect(db)) as conn:
            conn.execute("UPDATE managed_installation_targets SET next_attempt_at = 0")
        assert dispatch_managed_installations_once(db_path=db, executor=fail) is True
        current = get_managed_installation(operation["id"], db_path=db)
        target = current["targets"][0]
        assert target["failure_count"] == expected
        assert "do-not-leak" not in target["error"]
        assert target["state"] == ("failed" if expected == 8 else "retry")

    assert dispatch_managed_installations_once(db_path=db, executor=fail) is False


def test_expired_lease_can_be_reclaimed_but_old_worker_cannot_commit(tmp_path):
    db = tmp_path / "lease.db"
    operation = create_managed_installation(
        kind="skill",
        identifier="official/example",
        request_id="lease-cas",
        targets=["server"],
        db_path=db,
    )
    now = time.time()
    first = managed_installations._claim_target(db, now=now - 2, lease_seconds=1)
    assert first is not None
    managed_installations._release_execution_fence(first)
    second = managed_installations._claim_target(db, now=now, lease_seconds=30)

    assert first is not None and second is not None
    assert managed_installations._finish_target(db, first, state="completed") is False
    assert managed_installations._finish_target(db, second, state="completed") is True
    assert get_managed_installation(operation["id"], db_path=db)["state"] == "completed"


def test_heartbeat_sqlite_errors_mark_lease_lost_at_last_confirmed_deadline(
    tmp_path, monkeypatch,
):
    clock = [100.0]
    waits = []
    renewals = iter([101.0, sqlite3.OperationalError("busy")])

    class AdvancingStop:
        def wait(self, timeout):
            waits.append(timeout)
            clock[0] += timeout
            return False

    def renew(*_args, **_kwargs):
        result = next(renewals, sqlite3.OperationalError("busy"))
        if isinstance(result, sqlite3.Error):
            raise result
        return result

    claim = {
        "id": "mi-heartbeat-errors",
        "node_id": "server",
        "lease_token": "lease-token",
        "lease_until": 100.5,
    }
    monkeypatch.setattr(managed_installations.time, "time", lambda: clock[0])
    monkeypatch.setattr(managed_installations, "_renew_target_lease", renew)
    heartbeat = managed_installations._LeaseHeartbeat(
        tmp_path / "unused.db",
        claim,
        lease_seconds=1,
    )
    heartbeat._stop = AdvancingStop()

    heartbeat._run()

    assert heartbeat.lost.is_set()
    assert clock[0] == pytest.approx(101.0)
    assert sum(waits) == pytest.approx(1.0)
    with pytest.raises(RuntimeError, match="lease was lost"):
        heartbeat.ensure_owned()


def test_existing_project_requires_normalized_matching_origin(tmp_path):
    project_root = tmp_path / "projects"
    destination = project_root / "demo"
    (destination / ".git").mkdir(parents=True)
    (destination / "README.md").write_text("complete", encoding="utf-8")
    head = "a" * 40
    (destination / ".git" / "hermes-managed-install.json").write_text(json.dumps({
        "version": 1,
        "origin": "https://github.com/example/demo",
        "head": head,
    }), encoding="utf-8")
    commands = []

    def matching(command, *, timeout):
        commands.append(command)
        output = head if "rev-parse" in command else "https://GITHUB.com/example/demo.git/"
        return subprocess.CompletedProcess(
            command, 0, stdout=output + "\n", stderr="",
        )

    detail = managed_installations._execute_allowlisted_installation(
        {
            "kind": "project",
            "identifier": "https://github.com/example/demo",
            "profile": "default",
            "project_name": "demo",
        },
        executor=matching,
        project_root=project_root,
    )
    assert detail["existing"] is True
    assert commands == [
        managed_installations._managed_git_command(
            "-C", str(destination),
            "config", "--local", "--no-includes", "--get-all",
            "remote.origin.url",
        ),
        managed_installations._managed_git_command(
            "-C", str(destination), "rev-parse", "--verify", "HEAD",
        ),
    ]

    def mismatched(command, *, timeout):
        return subprocess.CompletedProcess(
            command, 0, stdout="https://github.com/attacker/demo.git\n", stderr="",
        )

    with pytest.raises(RuntimeError, match="origin does not match"):
        managed_installations._execute_allowlisted_installation(
            {
                "kind": "project",
                "identifier": "https://github.com/example/demo",
                "profile": "default",
                "project_name": "demo",
            },
            executor=mismatched,
            project_root=project_root,
        )


@pytest.mark.skipif(
    managed_installations.shutil.which("git") is None,
    reason="git is required for raw-origin validation",
)
def test_existing_project_rejects_local_instead_of_origin_spoof(tmp_path):
    project_root = tmp_path / "projects"
    destination = project_root / "demo"
    destination.mkdir(parents=True)

    def git(*arguments):
        return subprocess.run(
            ["git", *arguments],
            cwd=destination,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=True,
            env=managed_installations._hardened_command_environment(),
        )

    git("init")
    git("config", "user.email", "managed-install-test@example.invalid")
    git("config", "user.name", "Managed Install Test")
    (destination / "README.md").write_text("untrusted content\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-m", "test fixture")
    head = git("rev-parse", "HEAD").stdout.strip().lower()
    git("remote", "add", "origin", "https://attacker.invalid/example/demo.git")
    git(
        "config",
        "url.https://github.com/.insteadOf",
        "https://attacker.invalid/",
    )
    (destination / ".git" / "hermes-managed-install.json").write_text(
        json.dumps({
            "version": 1,
            "origin": "https://github.com/example/demo",
            "head": head,
        }),
        encoding="utf-8",
    )

    rewritten = managed_installations._run_command(
        managed_installations._managed_git_command(
            "-C", str(destination), "remote", "get-url", "origin",
        ),
        timeout=30,
    )
    assert rewritten.stdout.strip() == "https://github.com/example/demo.git"

    with pytest.raises(RuntimeError, match="origin does not match"):
        managed_installations._execute_allowlisted_installation(
            {
                "id": "local-origin-spoof",
                "kind": "project",
                "identifier": "https://github.com/example/demo.git",
                "profile": "default",
                "project_name": "demo",
                "owner_id": "server-admin",
            },
            project_root=project_root,
        )

    git("config", "--unset-all", "url.https://github.com/.insteadOf")
    git("config", "--unset-all", "remote.origin.url")
    git("config", "--add", "remote.origin.url", "https://GITHUB.com/example/demo.git/")
    detail = managed_installations._execute_allowlisted_installation(
        {
            "id": "valid-raw-origin",
            "kind": "project",
            "identifier": "https://github.com/example/demo",
            "profile": "default",
            "project_name": "demo",
            "owner_id": "server-admin",
        },
        project_root=project_root,
    )
    assert detail["existing"] is True
    assert detail["head"] == head


def test_interrupted_project_clone_is_not_accepted_as_existing(tmp_path):
    project_root = tmp_path / "projects"
    destination = project_root / "demo"
    (destination / ".git").mkdir(parents=True)
    (destination / "partial.tmp").write_text("partial", encoding="utf-8")

    def runner(command, *, timeout):
        output = "b" * 40 if "rev-parse" in command else "https://github.com/example/demo"
        return subprocess.CompletedProcess(command, 0, stdout=output + "\n", stderr="")

    with pytest.raises(RuntimeError, match="completion marker"):
        managed_installations._execute_allowlisted_installation(
            {
                "kind": "project",
                "identifier": "https://github.com/example/demo.git",
                "profile": "default",
                "project_name": "demo",
            },
            executor=runner,
            project_root=project_root,
        )


def test_project_clone_uses_staging_validation_marker_and_atomic_publish(tmp_path):
    project_root = tmp_path / "projects"
    head = "c" * 40
    commands = []

    def runner(command, *, timeout):
        commands.append(command)
        if "clone" in command:
            staging = Path(command[-1])
            (staging / ".git").mkdir(parents=True)
            (staging / "README.md").write_text("ready", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="cloned\n", stderr="")
        output = head if "rev-parse" in command else "https://github.com/example/demo.git"
        return subprocess.CompletedProcess(command, 0, stdout=output + "\n", stderr="")

    detail = managed_installations._execute_allowlisted_installation(
        {
            "id": "mi-staging",
            "kind": "project",
            "identifier": "https://github.com/example/demo.git",
            "profile": "default",
            "project_name": "demo",
        },
        executor=runner,
        project_root=project_root,
    )

    destination = project_root / "demo"
    assert detail["path"] == str(destination.resolve())
    assert detail["head"] == head
    assert (destination / ".git" / "hermes-managed-install.json").is_file()
    assert not list(project_root.glob(".demo.managed-install-*"))
    clone = next(command for command in commands if "clone" in command)
    assert clone[:-1] == managed_installations._managed_git_command(
        "clone", "--filter=blob:none", "--", "https://github.com/example/demo.git",
    )
    assert Path(clone[-1]).parent == project_root
    assert Path(clone[-1]).name.startswith(".demo.managed-install-")


def test_expired_worker_fence_prevents_second_installation_overlap(tmp_path, monkeypatch):
    db = tmp_path / "fenced.db"
    operation = create_managed_installation(
        kind="skill",
        identifier="official/example",
        request_id="fenced-overlap",
        targets=["server"],
        db_path=db,
    )
    entered = threading.Event()
    release = threading.Event()
    intervals = []
    heartbeat_started = threading.Event()
    heartbeats = []

    monkeypatch.setattr(
        managed_installations,
        "_renew_target_lease",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("busy")),
    )
    enter_heartbeat = managed_installations._LeaseHeartbeat.__enter__

    def capture_heartbeat(heartbeat):
        entered_heartbeat = enter_heartbeat(heartbeat)
        heartbeats.append(entered_heartbeat)
        heartbeat_started.set()
        return entered_heartbeat

    monkeypatch.setattr(
        managed_installations._LeaseHeartbeat,
        "__enter__",
        capture_heartbeat,
    )

    def first_executor(_command, *, timeout):
        started = time.monotonic()
        entered.set()
        assert release.wait(4)
        intervals.append(("first", started, time.monotonic()))
        return "first"

    first_thread = threading.Thread(
        target=dispatch_managed_installations_once,
        kwargs={"db_path": db, "executor": first_executor, "lease_seconds": 1},
    )
    first_thread.start()
    assert entered.wait(2)
    assert heartbeat_started.wait(2)
    heartbeat = heartbeats[0]
    with heartbeat._state_lock:
        heartbeat._lease_deadline = time.time() - 1
    with pytest.raises(RuntimeError, match="lease was lost"):
        heartbeat.ensure_owned()
    with managed_installations.closing(managed_installations._connect(db)) as conn:
        conn.execute(
            "UPDATE managed_installation_targets SET lease_until = ?",
            (time.time() - 1,),
        )

    second_started = []
    assert dispatch_managed_installations_once(
        db_path=db,
        executor=lambda *_args, **_kwargs: second_started.append(time.monotonic()),
        lease_seconds=1,
    ) is False
    assert second_started == []

    release.set()
    first_thread.join(3)
    assert not first_thread.is_alive()
    assert dispatch_managed_installations_once(
        db_path=db,
        executor=lambda *_args, **_kwargs: second_started.append(time.monotonic()) or "second",
        lease_seconds=2,
    ) is True
    assert second_started[0] >= intervals[0][2]
    assert get_managed_installation(operation["id"], db_path=db)["state"] == "completed"


def test_fenced_command_terminates_process_when_lease_is_lost():
    started = time.monotonic()

    def guard():
        if time.monotonic() - started > 0.35:
            raise RuntimeError("lease was lost")

    with pytest.raises(RuntimeError, match="lease was lost"):
        managed_installations._run_command_fenced(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            timeout=10,
            ownership_guard=guard,
            fence=None,
        )
    assert time.monotonic() - started < 2


def test_fenced_command_terminates_descendant_processes_when_lease_is_lost(tmp_path):
    marker = tmp_path / "escaped-grandchild.txt"
    grandchild = (
        "import pathlib,time; time.sleep(1.5); "
        f"pathlib.Path({str(marker)!r}).write_text('escaped', encoding='utf-8')"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
        "time.sleep(10)"
    )
    started = time.monotonic()

    def guard():
        if time.monotonic() - started > 0.35:
            raise RuntimeError("lease was lost")

    with pytest.raises(RuntimeError, match="lease was lost"):
        managed_installations._run_command_fenced(
            [sys.executable, "-c", parent],
            timeout=10,
            ownership_guard=guard,
            fence=None,
        )
    time.sleep(1.7)
    assert not marker.exists()


def test_project_sources_reject_ssh_to_avoid_implicit_known_hosts_trust(tmp_path):
    with pytest.raises(ValueError, match="HTTPS"):
        create_managed_installation(
            kind="project",
            identifier="ssh://git@github.com/example/demo.git",
            request_id="ssh-project",
            db_path=tmp_path / "state.db",
        )


def test_lease_heartbeat_prevents_reclaim_during_long_execution(tmp_path):
    db = tmp_path / "heartbeat.db"
    create_managed_installation(
        kind="skill",
        identifier="official/example",
        request_id="heartbeat-cas",
        targets=["server"],
        db_path=db,
    )
    first = managed_installations._claim_target(
        db,
        now=time.time(),
        lease_seconds=1,
    )
    assert first is not None
    with managed_installations._LeaseHeartbeat(db, first, lease_seconds=1):
        time.sleep(1.1)
        assert managed_installations._claim_target(db, now=time.time()) is None
    assert managed_installations._finish_target(db, first, state="completed") is True


def test_dashboard_api_persists_before_returning_accepted(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from hermes_cli import web_server

    db = tmp_path / "api-installations.db"
    monkeypatch.setattr(managed_installations, "managed_installations_db_path", lambda: db)
    monkeypatch.setattr(
        managed_installations,
        "_resolve_managed_project_source",
        lambda identifier, source_ref: {
            "canonical_source": managed_installations._normalize_project_origin(identifier),
            "source_ref": source_ref,
            "resolved_commit": "a" * 40,
            "resolved_tree": "b" * 40,
            "policy_version": managed_installations.MANAGED_SOURCE_POLICY_VERSION,
        },
    )
    _main_managed_nodes_config(tmp_path, monkeypatch)
    response = asyncio.run(
        web_server.create_managed_installation_api(
            web_server.ManagedInstallationRequest(**{
            "kind": "project",
            "identifier": "https://github.com/example/project.git",
            "request_id": "api-install-1",
            })
        )
    )
    operation = response["operation"]
    assert response["accepted"] is True
    assert operation["state"] == "accepted"
    assert [item["node_id"] for item in operation["targets"]] == ["dbb3", "wsl"]

    listed = asyncio.run(
        web_server.list_managed_installations_api(kind="project", profile="default", limit=50)
    )
    assert [item["id"] for item in listed["operations"]] == [operation["id"]]

    detail = asyncio.run(web_server.get_managed_installation_api(operation["id"]))
    assert detail["request_id"] == "api-install-1"


def test_required_topology_is_checked_before_installation_persistence(tmp_path, monkeypatch):
    from hermes_cli import managed_nodes

    db = tmp_path / "installations.db"
    missing = tmp_path / "missing-managed-nodes.json"
    monkeypatch.setattr(managed_nodes, "managed_nodes_config_path", lambda: missing)

    with pytest.raises(RuntimeError, match="managed-nodes configuration is required"):
        create_managed_installation(
            kind="project",
            identifier="https://github.com/example/project.git",
            request_id="missing-topology",
            db_path=db,
            require_topology=True,
        )
    assert not db.exists()

    _main_managed_nodes_config(tmp_path, monkeypatch)
    monkeypatch.setattr(
        managed_installations,
        "_resolve_managed_project_source",
        lambda identifier, source_ref: {
            "canonical_source": managed_installations._normalize_project_origin(identifier),
            "source_ref": source_ref,
            "resolved_commit": "a" * 40,
            "resolved_tree": "b" * 40,
            "policy_version": managed_installations.MANAGED_SOURCE_POLICY_VERSION,
        },
    )
    operation = create_managed_installation(
        kind="project",
        identifier="https://github.com/example/project.git",
        request_id="valid-topology",
        db_path=db,
        require_topology=True,
    )
    assert operation["state"] == "accepted"
    assert [target["node_id"] for target in operation["targets"]] == ["dbb3", "wsl"]


def test_dashboard_api_rejects_missing_managed_nodes_without_persisting(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import HTTPException
    from hermes_cli import managed_nodes, web_server

    db = tmp_path / "api-installations.db"
    missing = tmp_path / "missing-managed-nodes.json"
    monkeypatch.setattr(managed_installations, "managed_installations_db_path", lambda: db)
    monkeypatch.setattr(managed_nodes, "managed_nodes_config_path", lambda: missing)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            web_server.create_managed_installation_api(
                web_server.ManagedInstallationRequest(**{
                    "kind": "project",
                    "identifier": "https://github.com/example/project.git",
                    "request_id": "api-install-no-topology",
                })
            )
        )

    assert caught.value.status_code == 503
    assert "managed-nodes configuration is required" in str(caught.value.detail)
    assert not db.exists()


def test_managed_installation_tool_schema_requires_https_project_sources():
    from tools import managed_installation_tool as _managed_installation_tool  # noqa: F401
    from tools.registry import registry

    schema = registry.get_schema("managed_installation")
    assert schema is not None
    description = schema["parameters"]["properties"]["identifier"]["description"]
    assert "HTTPS git URL" in description
    assert "SSH" not in description


def _project_clone_runner(head: str):
    def runner(command, *, timeout):
        if "clone" in command:
            staging = Path(command[-1])
            (staging / ".git").mkdir(parents=True)
            (staging / "README.md").write_text("ready\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="cloned\n", stderr="")
        output = head if "rev-parse" in command else "https://github.com/example/demo.git"
        return subprocess.CompletedProcess(command, 0, stdout=output + "\n", stderr="")

    return runner


def test_account_projects_with_same_name_are_physically_isolated(tmp_path):
    project_root = tmp_path / "managed-projects"
    alice = managed_installations._execute_allowlisted_installation(
        {
            "id": "alice-project",
            "kind": "project",
            "identifier": "https://github.com/example/demo.git",
            "profile": "default",
            "project_name": "demo",
            "owner_id": "alice",
            "account_generation": "alice-gen-1",
        },
        executor=_project_clone_runner("a" * 40),
        project_root=project_root,
    )
    bob = managed_installations._execute_allowlisted_installation(
        {
            "id": "bob-project",
            "kind": "project",
            "identifier": "https://github.com/example/demo.git",
            "profile": "default",
            "project_name": "demo",
            "owner_id": "bob",
            "account_generation": "bob-gen-1",
        },
        executor=_project_clone_runner("b" * 40),
        project_root=project_root,
    )

    alice_path = Path(alice["path"])
    bob_path = Path(bob["path"])
    assert alice_path != bob_path
    assert alice_path.name == bob_path.name == "demo"
    assert alice_path.is_dir() and bob_path.is_dir()
    assert alice_path.parent.parent == bob_path.parent.parent


def test_truncated_account_directory_collision_is_rejected_by_full_marker(
    tmp_path, monkeypatch,
):
    real_digest = managed_installations._owner_boundary_digest

    def colliding_digest(owner_id, generation):
        suffix = "1" * 40 if owner_id == "alice" else "2" * 40
        return "a" * 24 + suffix

    monkeypatch.setattr(
        managed_installations, "_owner_boundary_digest", colliding_digest
    )
    first = managed_installations._account_project_root(
        tmp_path / "projects", "alice", "gen-1", create=True,
    )
    with pytest.raises(RuntimeError, match="boundary collision"):
        managed_installations._account_project_root(
            tmp_path / "projects", "bob", "gen-1", create=True,
        )
    assert json.loads((first / ".managed-owner-boundary.json").read_text(
        encoding="utf-8"
    ))["boundary"] == colliding_digest("alice", "gen-1")
    assert real_digest("alice", "gen-1") != real_digest("bob", "gen-1")


def test_account_skill_profiles_use_distinct_physical_homes(tmp_path, monkeypatch):
    profiles_root = tmp_path / "profiles"
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda name: profiles_root / name,
    )
    commands = []

    def install_skill(command, *, timeout):
        commands.append(command)
        profile_home = profiles_root / command[4]
        destination = profile_home / "skills" / "development" / "example"
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text("# Example\n", encoding="utf-8")
        lock = profile_home / "skills" / ".hub" / "lock.json"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"installed": {"example": {
            "identifier": "official/example",
            "install_path": "development/example",
        }}}), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="installed\n", stderr="")

    for owner, generation in (("alice", "gen-1"), ("bob", "gen-1")):
        managed_installations._execute_allowlisted_installation(
            {
                "kind": "skill",
                "identifier": "official/example",
                "profile": "default",
                "owner_id": owner,
                "account_generation": generation,
            },
            executor=install_skill,
        )

    profile_names = [command[4] for command in commands]
    assert len(set(profile_names)) == 2
    assert all(name.startswith("acct-") for name in profile_names)
    assert all((profiles_root / name / ".managed-owner-boundary.json").is_file()
               for name in profile_names)


def test_account_runtime_inherits_base_and_exposes_only_its_generation_resources(
    tmp_path, monkeypatch,
):
    import yaml

    profiles_root = tmp_path / "profiles"
    base_home = tmp_path / "default"
    base_home.mkdir()
    (base_home / "skills" / "base-skill").mkdir(parents=True)
    (base_home / "skills" / "base-skill" / "SKILL.md").write_text(
        "# Base\n", encoding="utf-8"
    )
    (base_home / "config.yaml").write_text(
        yaml.safe_dump({
            "model": "base-model",
            "mcp_servers": {"base-mcp": {"url": "https://base.invalid/mcp"}},
            "skills": {"external_dirs": ["shared-skills"]},
        }, sort_keys=False),
        encoding="utf-8",
    )
    (base_home / ".env").write_text("MODEL_API_KEY=base-secret\n", encoding="utf-8")
    (base_home / "SOUL.md").write_text("Base identity\n", encoding="utf-8")
    (base_home / "shared-skills").mkdir()

    def profile_dir(name):
        return base_home if name == "default" else profiles_root / name

    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", profile_dir)
    monkeypatch.setattr(managed_installations, "get_hermes_home", lambda: str(tmp_path))
    db = tmp_path / "managed-installations.db"

    alice_home = managed_installations._account_resource_home(
        "alice", "alice-gen-1", create=True,
    )
    (alice_home / "skills" / "alice-skill").mkdir(parents=True)
    (alice_home / "skills" / "alice-skill" / "SKILL.md").write_text(
        "# Alice\n", encoding="utf-8"
    )
    (alice_home / "config.yaml").write_text(
        yaml.safe_dump({
            "mcp_servers": {"alice-mcp": {"url": "https://alice.invalid/mcp"}},
        }),
        encoding="utf-8",
    )
    managed_installations._record_account_mcp_server(alice_home, "alice-mcp")

    runtime = managed_installations.managed_account_runtime_home(
        "alice",
        "alice-gen-1",
        "default",
        db_path=db,
        base_profile_home=base_home,
    )
    config = yaml.safe_load((runtime / "config.yaml").read_text(encoding="utf-8"))
    assert config["model"] == "base-model"
    assert set(config["mcp_servers"]) == {"base-mcp", "alice-mcp"}
    assert str((base_home / "skills").resolve()) in config["skills"]["external_dirs"]
    assert str((base_home / "shared-skills").resolve()) in config["skills"]["external_dirs"]
    assert str((alice_home / "skills").resolve()) in config["skills"]["external_dirs"]
    assert (alice_home / "skills" / "alice-skill" / "SKILL.md").is_file()
    assert (runtime / ".env").read_text(encoding="utf-8") == "MODEL_API_KEY=base-secret\n"
    assert (runtime / "SOUL.md").read_text(encoding="utf-8") == "Base identity\n"

    bob_home = managed_installations.managed_account_runtime_home(
        "bob",
        "bob-gen-1",
        "default",
        db_path=db,
        base_profile_home=base_home,
    )
    bob_config = yaml.safe_load((bob_home / "config.yaml").read_text(encoding="utf-8"))
    assert set(bob_config["mcp_servers"]) == {"base-mcp"}
    assert str((alice_home / "skills").resolve()) not in bob_config["skills"]["external_dirs"]

    managed_installations.delete_owner_managed_resources(
        "alice",
        account_generation="alice-gen-1",
        db_path=db,
        _project_root=tmp_path / "managed-projects",
    )
    with pytest.raises(PermissionError, match="generation is deleted"):
        managed_installations.managed_account_runtime_home(
            "alice",
            "alice-gen-1",
            "default",
            db_path=db,
            base_profile_home=base_home,
        )
    assert not alice_home.exists()
    assert bob_home.is_dir()


def test_account_resources_are_shared_across_role_runtime_profiles(
    tmp_path, monkeypatch,
):
    import yaml

    profiles_root = tmp_path / "profiles"
    base_homes = {}
    for name in ("default", "dbb3-manager", "dbb3-worker", "pc-worker"):
        home = tmp_path / f"base-{name}"
        home.mkdir()
        (home / "config.yaml").write_text(
            yaml.safe_dump({"model": f"model-{name}"}, sort_keys=False),
            encoding="utf-8",
        )
        base_homes[name] = home

    def profile_dir(name):
        return base_homes.get(name, profiles_root / name)

    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", profile_dir)
    monkeypatch.setattr(managed_installations, "get_hermes_home", lambda: str(tmp_path))
    db = tmp_path / "managed-installations.db"
    resource_home = managed_installations._account_resource_home(
        "alice", "alice-gen-1", create=True,
    )
    (resource_home / "skills" / "shared-account-skill").mkdir(parents=True)
    (resource_home / "skills" / "shared-account-skill" / "SKILL.md").write_text(
        "# Shared account skill\n", encoding="utf-8",
    )
    (resource_home / "config.yaml").write_text(
        yaml.safe_dump({
            "mcp_servers": {"account-mcp": {"url": "https://account.invalid/mcp"}},
        }),
        encoding="utf-8",
    )
    managed_installations._record_account_mcp_server(resource_home, "account-mcp")

    runtimes = {}
    for role, base_home in base_homes.items():
        runtimes[role] = managed_installations.managed_account_runtime_home(
            "alice",
            "alice-gen-1",
            role,
            db_path=db,
            base_profile_home=base_home,
        )

    assert len(set(runtimes.values())) == len(base_homes)
    for role, runtime in runtimes.items():
        config = yaml.safe_load((runtime / "config.yaml").read_text(encoding="utf-8"))
        assert config["model"] == f"model-{role}"
        assert set(config["mcp_servers"]) == {"account-mcp"}
        assert str((resource_home / "skills").resolve()) in config["skills"]["external_dirs"]
        assert managed_installations.managed_account_runtime_profile(
            "alice",
            "alice-gen-1",
            role,
            db_path=db,
            base_profile_home=base_homes[role],
        ) == managed_installations._account_profile_name(
            "alice", "alice-gen-1", role,
        )


def test_remote_install_request_preserves_account_boundary(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        managed_installations,
        "_installation_route",
        lambda *_args, **_kwargs: {
            "url": "https://node.example/install",
            "token": INSTALL_TOKEN,
            "timeout": 8,
        },
    )

    def read(request, timeout):
        captured.update(json.loads(request.data.decode("utf-8")))
        return {"state": "accepted"}

    monkeypatch.setattr(managed_installations, "_read_json_response", read)
    managed_installations._dispatch_or_poll_remote({
        "id": "mi-account-boundary",
        "request_id": "request-1",
        "node_id": "dbb3",
        "target_state": "pending",
        "kind": "skill",
        "identifier": "official/example",
        "profile": "default",
        "project_name": "",
        "owner_id": "alice",
        "account_generation": "alice-gen-7",
    }, config_path=None)

    assert captured["owner_id"] == "alice"
    assert captured["account_generation"] == "alice-gen-7"


def test_receiver_can_poll_account_scoped_operation_by_main_operation_id(
    tmp_path, monkeypatch,
):
    token = tmp_path / "receiver-token"
    token.write_text(INSTALL_TOKEN, encoding="utf-8")
    config = _receiver_config(tmp_path, token)
    monkeypatch.setattr(managed_installations, "_start_receiver_thread", lambda *_args: None)
    payload = {
        "id": "mi-account-remote-poll",
        "request_id": "main-request",
        "node_id": "dbb3",
        "kind": "skill",
        "identifier": "official/example",
        "profile": "default",
        "project_name": "",
        "owner_id": "alice",
        "account_generation": "alice-gen-7",
    }

    accepted = accept_managed_installation(payload, INSTALL_TOKEN, config)
    polled = get_received_managed_installation(
        "mi-account-remote-poll", INSTALL_TOKEN, config
    )

    assert accepted["accepted"] is True
    assert polled["id"] == "mi-account-remote-poll"
    assert polled["node_id"] == "dbb3"


def test_managed_installation_tool_cannot_read_another_account_operation(
    tmp_path, monkeypatch,
):
    from tools.managed_installation_tool import managed_installation

    db = tmp_path / "managed-installations.db"
    alice = create_managed_installation(
        kind="skill", identifier="official/example", request_id="alice-tool-op",
        targets=["server"], owner_id="alice", account_generation="alice-gen-1",
        db_path=db,
    )
    monkeypatch.setattr(managed_installations, "managed_installations_db_path", lambda: db)
    monkeypatch.setenv("HERMES_TOOL_ARTIFACT_OWNER", "bob")
    monkeypatch.setenv("HERMES_ACCOUNT_GENERATION", "bob-gen-1")

    response = json.loads(managed_installation({
        "action": "status", "operation_id": alice["id"],
    }))

    assert response == {"error": "installation_not_found"}


def test_account_delete_removes_physical_boundary_and_blocks_old_generation(
    tmp_path, monkeypatch,
):
    db = tmp_path / "managed-installations.db"
    profiles_root = tmp_path / "profiles"
    projects_root = tmp_path / "managed-projects"
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda name: profiles_root / name,
    )
    monkeypatch.setattr(managed_installations, "get_hermes_home", lambda: str(tmp_path))
    alice_profile = managed_installations._account_profile_home(
        "alice", "alice-gen-1", "default", create=True,
    )
    bob_profile = managed_installations._account_profile_home(
        "bob", "bob-gen-1", "default", create=True,
    )
    alice_projects = managed_installations._account_project_root(
        projects_root, "alice", "alice-gen-1", create=True,
    )
    bob_projects = managed_installations._account_project_root(
        projects_root, "bob", "bob-gen-1", create=True,
    )
    (alice_projects / "demo").mkdir()
    (bob_projects / "demo").mkdir()
    create_managed_installation(
        kind="skill", identifier="official/example", request_id="alice-install",
        targets=["server"], owner_id="alice", account_generation="alice-gen-1",
        db_path=db,
    )
    bob = create_managed_installation(
        kind="skill", identifier="official/example", request_id="bob-install",
        targets=["server"], owner_id="bob", account_generation="bob-gen-1",
        db_path=db,
    )

    deleted = managed_installations.delete_owner_managed_resources(
        "alice", account_generation="alice-gen-1", db_path=db,
    )

    assert deleted["operations"] == 1
    assert not alice_profile.exists() and not alice_projects.exists()
    assert bob_profile.is_dir() and bob_projects.is_dir()
    assert get_managed_installation(bob["id"], db_path=db)["owner_id"] == "bob"
    with pytest.raises(ValueError, match="generation is deleted"):
        create_managed_installation(
            kind="skill", identifier="official/example", request_id="late-old-request",
            targets=["server"], owner_id="alice", account_generation="alice-gen-1",
            db_path=db,
        )


def test_empty_account_delete_fences_paused_old_install_and_allows_new_generation(
    tmp_path, monkeypatch,
):
    db = tmp_path / "managed-installations.db"
    profiles_root = tmp_path / "profiles"
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda name: profiles_root / name,
    )
    monkeypatch.setattr(managed_installations, "get_hermes_home", lambda: str(tmp_path))
    entered = threading.Event()
    release = threading.Event()
    real_resolve = managed_installations.resolve_installation_targets
    outcome = {}

    def paused_resolve(*args, **kwargs):
        targets = real_resolve(*args, **kwargs)
        if threading.current_thread().name == "late-managed-install":
            entered.set()
            assert release.wait(timeout=5)
        return targets

    def late_creator():
        try:
            create_managed_installation(
                kind="skill",
                identifier="official/example",
                request_id="late-empty-store-request",
                targets=["server"],
                owner_id="alice",
                account_generation="alice-gen-1",
                db_path=db,
            )
        except BaseException as exc:
            outcome["error"] = exc

    monkeypatch.setattr(
        managed_installations,
        "resolve_installation_targets",
        paused_resolve,
    )
    worker = threading.Thread(target=late_creator, name="late-managed-install")
    worker.start()
    assert entered.wait(timeout=5)

    assert managed_installations.delete_owner_managed_resources(
        "alice",
        account_generation="alice-gen-1",
        include_known_generations=True,
        db_path=db,
    ) == {"resources": 0, "events": 0, "operations": 0}
    release.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert isinstance(outcome.get("error"), ValueError)
    assert "generation is deleted" in str(outcome["error"])

    current = create_managed_installation(
        kind="skill",
        identifier="official/example",
        request_id="new-generation-request",
        targets=["server"],
        owner_id="alice",
        account_generation="alice-gen-2",
        db_path=db,
    )
    assert current["account_generation"] == "alice-gen-2"


def test_delete_interleaved_with_running_install_cleans_after_fence_release(
    tmp_path, monkeypatch,
):
    db = tmp_path / "managed-installations.db"
    profiles_root = tmp_path / "profiles"
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda name: profiles_root / name,
    )
    monkeypatch.setattr(managed_installations, "get_hermes_home", lambda: str(tmp_path))
    create_managed_installation(
        kind="skill", identifier="official/example", request_id="interleaved-install",
        targets=["server"], owner_id="alice", account_generation="alice-gen-1",
        db_path=db,
    )
    entered = threading.Event()
    release = threading.Event()

    def blocking_install(command, *, timeout):
        profile_home = profiles_root / command[4]
        destination = profile_home / "skills" / "development" / "example"
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text("# Example\n", encoding="utf-8")
        lock = profile_home / "skills" / ".hub" / "lock.json"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"installed": {"example": {
            "identifier": "official/example",
            "install_path": "development/example",
        }}}), encoding="utf-8")
        entered.set()
        assert release.wait(5)
        return subprocess.CompletedProcess(command, 0, stdout="installed\n", stderr="")

    worker = threading.Thread(
        target=dispatch_managed_installations_once,
        kwargs={"db_path": db, "executor": blocking_install},
    )
    worker.start()
    assert entered.wait(3)
    profile_home = next(profiles_root.glob("acct-*"))

    managed_installations.delete_owner_managed_resources(
        "alice", account_generation="alice-gen-1", db_path=db,
    )
    assert profile_home.exists()
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    with managed_installations.closing(managed_installations._connect(db)) as conn:
        conn.execute("UPDATE managed_owner_deletion_targets SET next_attempt_at=0")
    assert dispatch_managed_installations_once(db_path=db) is True

    assert not profile_home.exists()
    assert managed_installations._owner_deletion_state(
        db, "alice", "alice-gen-1"
    ) == "complete"


def test_receiver_delete_action_cleans_node_files_and_tombstones_generation(
    tmp_path, monkeypatch,
):
    token = tmp_path / "receiver-token"
    token.write_text(INSTALL_TOKEN, encoding="utf-8")
    config = _receiver_config(tmp_path, token)
    profiles_root = tmp_path / "profiles"
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda name: profiles_root / name,
    )
    profile_home = managed_installations._account_profile_home(
        "alice", "alice-gen-9", "default", create=True,
    )
    project_home = managed_installations._account_project_root(
        tmp_path / "projects", "alice", "alice-gen-9", create=True,
    )
    receiver_db = tmp_path / "node-installations.db"
    create_managed_installation(
        kind="project", identifier="https://github.com/example/demo.git",
        request_id="receiver-project", targets=["dbb3"], project_name="demo",
        owner_id="alice", account_generation="alice-gen-9", db_path=receiver_db,
    )

    response = accept_managed_installation({
        "action": "delete_owner",
        "node_id": "dbb3",
        "owner_id": "alice",
        "account_generation": "alice-gen-9",
    }, INSTALL_TOKEN, config)

    assert response["state"] == "complete"
    assert not profile_home.exists() and not project_home.exists()
    with pytest.raises(ValueError, match="generation is deleted"):
        create_managed_installation(
            kind="skill", identifier="official/example", request_id="late-receiver",
            targets=["dbb3"], owner_id="alice", account_generation="alice-gen-9",
            db_path=receiver_db,
        )


def test_remote_account_cleanup_retries_durably_then_completes(
    tmp_path, monkeypatch,
):
    db = tmp_path / "managed-installations.db"
    monkeypatch.setattr(managed_installations, "get_hermes_home", lambda: str(tmp_path))
    create_managed_installation(
        kind="project", identifier="https://github.com/example/demo.git",
        request_id="remote-delete", targets=["dbb3"], project_name="demo",
        owner_id="alice", account_generation="alice-gen-3", db_path=db,
    )
    managed_installations.delete_owner_managed_resources(
        "alice", account_generation="alice-gen-3", db_path=db,
    )
    monkeypatch.setattr(
        managed_installations,
        "_installation_route",
        lambda *_args, **_kwargs: {
            "url": "https://node.example/install",
            "token": INSTALL_TOKEN,
            "timeout": 8,
        },
    )
    responses = iter([URLError("offline"), {"state": "complete"}])

    def read(*_args, **_kwargs):
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(managed_installations, "_read_json_response", read)
    assert dispatch_managed_installations_once(db_path=db) is True
    assert managed_installations._owner_deletion_state(
        db, "alice", "alice-gen-3"
    ) == "pending"
    with managed_installations.closing(managed_installations._connect(db)) as conn:
        conn.execute("UPDATE managed_owner_deletion_targets SET next_attempt_at=0")
    assert dispatch_managed_installations_once(db_path=db) is True
    assert managed_installations._owner_deletion_state(
        db, "alice", "alice-gen-3"
    ) == "complete"


def test_project_ref_is_resolved_once_and_idempotent_replay_keeps_original_lock(
    tmp_path, monkeypatch,
):
    db = tmp_path / "source-lock.db"
    locks = iter((("a" * 40, "b" * 40), ("c" * 40, "d" * 40)))
    calls = []

    monkeypatch.setattr(
        managed_installations,
        "require_managed_installation_topology",
        lambda *_args, **_kwargs: None,
    )

    def resolve(identifier, source_ref):
        calls.append((identifier, source_ref))
        commit, tree = next(locks)
        return {
            "canonical_source": managed_installations._normalize_project_origin(identifier),
            "source_ref": source_ref,
            "resolved_commit": commit,
            "resolved_tree": tree,
            "policy_version": managed_installations.MANAGED_SOURCE_POLICY_VERSION,
        }

    monkeypatch.setattr(managed_installations, "_resolve_managed_project_source", resolve)
    first = create_managed_installation(
        kind="project",
        identifier="https://github.com/example/demo.git",
        source_ref="refs/heads/main",
        request_id="moving-ref",
        targets=["dbb3"],
        require_topology=True,
        db_path=db,
    )
    replay = create_managed_installation(
        kind="project",
        identifier="https://github.com/example/demo.git",
        source_ref="refs/heads/main",
        request_id="moving-ref",
        targets=["dbb3"],
        require_topology=True,
        db_path=db,
    )
    moved = create_managed_installation(
        kind="project",
        identifier="https://github.com/example/demo.git",
        source_ref="refs/heads/main",
        request_id="moving-ref-new-operation",
        targets=["dbb3"],
        require_topology=True,
        db_path=db,
    )

    assert first["source_lock"]["resolved_commit"] == "a" * 40
    assert replay["source_lock"] == first["source_lock"]
    assert moved["source_lock"]["resolved_commit"] == "c" * 40
    assert len(calls) == 2


def test_source_resolution_pins_git_connection_and_rejects_private_dns(
    monkeypatch,
):
    commands = []

    monkeypatch.setattr(
        managed_installations.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (managed_installations.socket.AF_INET, 1, 6, "", ("93.184.216.34", 443)),
        ],
    )

    def runner(command, *, timeout):
        commands.append(command)
        if "FETCH_HEAD^{commit}" in command:
            output = "a" * 40
        elif "FETCH_HEAD^{tree}" in command:
            output = "b" * 40
        else:
            output = "ok"
        return subprocess.CompletedProcess(command, 0, stdout=output + "\n", stderr="")

    locked = managed_installations._resolve_managed_project_source(
        "https://github.com/example/demo.git",
        "refs/tags/v1.0.0",
        runner=runner,
    )
    fetch = next(command for command in commands if "fetch" in command)
    assert "http.curloptResolve=+github.com:443:93.184.216.34" in fetch
    assert "http.followRedirects=false" in fetch
    assert "http.sslVerify=true" in fetch
    assert "credential.helper=" in fetch
    assert f"core.hooksPath={os.devnull}" in fetch
    assert locked["resolved_commit"] == "a" * 40
    assert locked["resolved_tree"] == "b" * 40

    monkeypatch.setattr(
        managed_installations.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (managed_installations.socket.AF_INET, 1, 6, "", ("127.0.0.1", 443)),
        ],
    )
    with pytest.raises(RuntimeError, match="non-public"):
        managed_installations._managed_source_curl_resolve(
            "https://github.com/example/demo.git"
        )


def test_pinned_project_uses_detached_commit_and_emits_verified_receipt(tmp_path):
    project_root = tmp_path / "projects"
    commit = "a" * 40
    tree = "b" * 40
    canonical = "https://github.com/example/demo"
    commands = []

    def runner(command, *, timeout):
        commands.append(command)
        if "init" in command:
            staging = Path(command[-1])
            (staging / ".git").mkdir(parents=True)
            (staging / "README.md").write_text("locked\n", encoding="utf-8")
        if "remote.origin.url" in command:
            output = canonical
        elif "--abbrev-ref" in command:
            output = "HEAD"
        elif "HEAD^{tree}" in command:
            output = tree
        elif "--verify" in command and "HEAD" in command:
            output = commit
        else:
            output = "ok"
        return subprocess.CompletedProcess(command, 0, stdout=output + "\n", stderr="")

    detail = managed_installations._execute_allowlisted_installation(
        {
            "id": "pinned-project",
            "node_id": "dbb3",
            "kind": "project",
            "identifier": "https://github.com/example/demo.git",
            "canonical_source": canonical,
            "source_ref": "refs/heads/main",
            "resolved_commit": commit,
            "resolved_tree": tree,
            "policy_version": managed_installations.MANAGED_SOURCE_POLICY_VERSION,
            "project_name": "demo",
            "owner_id": "server-admin",
            "_source_pins": ("+github.com:443:93.184.216.34",),
        },
        executor=runner,
        project_root=project_root,
    )

    assert not any("clone" in command for command in commands)
    assert any("fetch" in command and commit in command for command in commands)
    assert any("checkout" in command and "--detach" in command for command in commands)
    assert detail["resolved_commit"] == commit
    assert detail["tree_sha"] == tree
    assert detail["artifact_hash"] == detail["content_hash"]
    assert detail["receipt_schema"] == 1
    assert detail["node_id"] == "dbb3"
    assert detail["health"]["status"] == "healthy"
    marker = json.loads(
        (project_root / "demo" / ".git" / "hermes-managed-install.json").read_text(
            encoding="utf-8"
        )
    )
    assert marker["version"] == 2
    assert marker["head"] == commit
    assert marker["tree"] == tree


def test_source_lock_columns_are_immutable_and_partial_state_is_explicit(tmp_path):
    db = tmp_path / "aggregate.db"
    operation = create_managed_installation(
        kind="skill",
        identifier="official/example",
        request_id="partial-install",
        targets=["server", "dbb3"],
        db_path=db,
    )
    first = managed_installations._claim_target(db, now=time.time(), lease_seconds=30)
    assert first and first["node_id"] == "server"
    detail = managed_installations._finalize_installation_detail(first, {
        "installed": True,
        "kind": "skill",
        "proof_schema": 1,
        "proof_source": "local_filesystem",
        "resolved_version": "1.0.0",
        "content_hash": "f" * 64,
    })
    assert managed_installations._finish_target(
        db, first, state="completed", detail=detail
    )
    managed_installations._release_execution_fence(first)

    current = get_managed_installation(operation["id"], db_path=db)
    assert current["aggregate_state"] == "partial"
    with managed_installations.closing(managed_installations._connect(db)) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="source lock is immutable"):
            conn.execute(
                "UPDATE managed_installations SET policy_version='attacker-policy' WHERE id=?",
                (operation["id"],),
            )


def test_verified_skill_rollback_uninstalls_each_node_and_updates_catalog(
    tmp_path, monkeypatch,
):
    db = tmp_path / "rollback.db"
    installation = create_managed_installation(
        kind="skill",
        identifier="official/example",
        request_id="install-before-rollback",
        targets=["server"],
        db_path=db,
    )
    claimed = managed_installations._claim_target(db, now=time.time(), lease_seconds=30)
    assert claimed
    receipt = managed_installations._finalize_installation_detail(claimed, {
        "installed": True,
        "kind": "skill",
        "installed_name": "example",
        "proof_schema": 1,
        "proof_source": "local_filesystem",
        "resolved_version": "1.0.0",
        "content_hash": "f" * 64,
    })
    assert managed_installations._finish_target(
        db, claimed, state="completed", detail=receipt
    )
    managed_installations._release_execution_fence(claimed)
    assert get_managed_installation(
        installation["id"], db_path=db
    )["aggregate_state"] == "verified"

    monkeypatch.setattr(
        managed_installations,
        "require_managed_installation_topology",
        lambda *_args, **_kwargs: None,
    )
    rollback = managed_installations.rollback_managed_installation(
        installation["id"],
        request_id="rollback-example",
        db_path=db,
    )
    calls = []

    def executor(command, *, timeout):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="removed\n", stderr="")

    assert dispatch_managed_installations_once(db_path=db, executor=executor)
    current = get_managed_installation(rollback["id"], db_path=db)
    assert current["state"] == "rolled_back"
    assert current["aggregate_state"] == "rolled_back"
    assert calls[0][-4:] == ["skills", "uninstall", "example", "--yes"]
    resource = managed_installations.list_managed_resources(db_path=db)["resources"][0]
    assert resource["aggregate_state"] == "rolled_back"
    assert resource["enabled"] is False
    assert resource["loaded_nodes"] == []
    assert resource["rollback_available"] is False


def test_partial_rollback_fails_closed_without_claiming_every_node_was_removed(
    tmp_path, monkeypatch,
):
    db = tmp_path / "partial-rollback.db"
    installation = create_managed_installation(
        kind="skill",
        identifier="official/example",
        request_id="install-two-nodes",
        targets=["server", "dbb3"],
        db_path=db,
    )
    for node_id in ("server", "dbb3"):
        claimed = managed_installations._claim_target(
            db, now=time.time() + 10_000, lease_seconds=30
        )
        assert claimed and claimed["node_id"] == node_id
        receipt = managed_installations._finalize_installation_detail(claimed, {
            "installed": True,
            "kind": "skill",
            "installed_name": "example",
            "proof_schema": 1,
            "proof_source": "local_filesystem",
            "resolved_version": "1.0.0",
            "content_hash": "f" * 64,
        })
        assert managed_installations._finish_target(
            db, claimed, state="completed", detail=receipt
        )
        managed_installations._release_execution_fence(claimed)
    monkeypatch.setattr(
        managed_installations,
        "require_managed_installation_topology",
        lambda *_args, **_kwargs: None,
    )
    rollback = managed_installations.rollback_managed_installation(
        installation["id"], db_path=db
    )
    first = managed_installations._claim_target(db, now=time.time() + 20_000)
    assert first and first["node_id"] == "server"
    assert managed_installations._finish_target(
        db,
        first,
        state="completed",
        detail={"rollback_receipt_schema": 1, "removed": True},
    )
    managed_installations._release_execution_fence(first)
    second = managed_installations._claim_target(db, now=time.time() + 20_000)
    assert second and second["node_id"] == "dbb3"
    assert managed_installations._finish_target(
        db, second, state="failed", error="node offline"
    )
    managed_installations._release_execution_fence(second)

    current = get_managed_installation(rollback["id"], db_path=db)
    assert current["aggregate_state"] == "partial"
    resource = managed_installations.list_managed_resources(db_path=db)["resources"][0]
    assert resource["aggregate_state"] == "partial"
    assert resource["loaded_nodes"] == ["dbb3"]
    assert resource["enabled"] is False
