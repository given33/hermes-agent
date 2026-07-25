import asyncio
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
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
        ["git", "-C", str(destination), "remote", "get-url", "origin"],
        ["git", "-C", str(destination), "rev-parse", "--verify", "HEAD"],
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

    def runner(command, *, timeout):
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
