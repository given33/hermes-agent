import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import web_server

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    previous_auth_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = False
    test_client = TestClient(web_server.app)
    test_client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    try:
        yield test_client
    finally:
        if previous_auth_required is None:
            try:
                delattr(web_server.app.state, "auth_required")
            except AttributeError:
                pass
        else:
            web_server.app.state.auth_required = previous_auth_required


def test_fs_list_sorts_and_hides_noise(client, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "b.txt").write_text("b")
    (root / "a_dir").mkdir()
    (root / "a.txt").write_text("a")
    (root / "node_modules").mkdir()
    (root / ".git").mkdir()

    response = client.get("/api/fs/list", params={"path": str(root)})

    assert response.status_code == 200
    entries = response.json()["entries"]
    assert [entry["name"] for entry in entries] == ["a_dir", "a.txt", "b.txt"]
    assert entries[0] == {"name": "a_dir", "path": str(root / "a_dir"), "isDirectory": True}
    assert all(entry["name"] not in {".git", "node_modules"} for entry in entries)


def test_fs_read_data_url_rejects_over_cap(client, tmp_path, monkeypatch):
    monkeypatch.setattr(web_server, "_FS_DATA_URL_MAX_BYTES", 3)
    target = tmp_path / "image.png"
    target.write_bytes(b"1234")

    response = client.get("/api/fs/read-data-url", params={"path": str(target)})

    assert response.status_code == 413


def test_fs_download_streams_file_without_data_url_cap(client, tmp_path, monkeypatch):
    monkeypatch.setattr(web_server, "_FS_DATA_URL_MAX_BYTES", 3)
    target = tmp_path / "report with spaces.pdf"
    target.write_bytes(b"123456")

    response = client.get("/api/fs/download", params={"path": str(target)})

    assert response.status_code == 200
    assert response.content == b"123456"
    assert response.headers["content-type"].startswith("application/pdf")
    assert "report%20with%20spaces.pdf" in response.headers["content-disposition"]


def test_fs_download_rejects_sensitive_files(client, tmp_path):
    target = tmp_path / ".env"
    target.write_text("SECRET=1")

    response = client.get("/api/fs/download", params={"path": str(target)})

    assert response.status_code == 403


def test_fs_remote_requests_are_confined_to_approved_roots(
    client, tmp_path, monkeypatch
):
    approved = tmp_path / "approved"
    outside = tmp_path / "outside"
    approved.mkdir()
    outside.mkdir()
    allowed_file = approved / "notes.txt"
    outside_file = outside / "secret.txt"
    allowed_file.write_text("before", encoding="utf-8")
    outside_file.write_text("secret", encoding="utf-8")

    monkeypatch.setenv(web_server._FS_ALLOWED_ROOTS_ENV, str(approved))
    monkeypatch.setattr(web_server, "_local_dashboard_request", lambda _request: False)

    allowed_read = client.get(
        "/api/fs/read-text", params={"path": str(allowed_file)}
    )
    allowed_write = client.post(
        "/api/fs/write-text",
        json={"path": str(allowed_file), "content": "after"},
    )
    denied_read = client.get(
        "/api/fs/read-text", params={"path": str(outside_file)}
    )
    denied_write = client.post(
        "/api/fs/write-text",
        json={"path": str(outside / "new.txt"), "content": "no"},
    )

    assert allowed_read.status_code == 200
    assert allowed_write.status_code == 200
    assert allowed_file.read_text(encoding="utf-8") == "after"
    assert denied_read.status_code == 403
    assert denied_write.status_code == 403
    assert not (outside / "new.txt").exists()


def test_fs_sensitive_paths_are_hidden_and_rejected(client, tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "visible.txt").write_text("ok", encoding="utf-8")
    secret = root / ".env.local"
    secret.write_text("TOKEN=secret", encoding="utf-8")
    token_dir = root / "mcp-tokens"
    token_dir.mkdir()
    token_file = token_dir / "service.json"
    token_file.write_text("{}", encoding="utf-8")

    monkeypatch.setenv(web_server._FS_ALLOWED_ROOTS_ENV, str(root))
    monkeypatch.setattr(web_server, "_local_dashboard_request", lambda _request: False)

    listing = client.get("/api/fs/list", params={"path": str(root)})
    read_secret = client.get("/api/fs/read-text", params={"path": str(secret)})
    write_secret = client.post(
        "/api/fs/write-text",
        json={"path": str(secret), "content": "TOKEN=replaced"},
    )
    read_token = client.get(
        "/api/fs/read-data-url", params={"path": str(token_file)}
    )

    assert listing.status_code == 200
    assert [entry["name"] for entry in listing.json()["entries"]] == ["visible.txt"]
    assert read_secret.status_code == 403
    assert write_secret.status_code == 403
    assert read_token.status_code == 403
    assert secret.read_text(encoding="utf-8") == "TOKEN=secret"


def test_dashboard_backup_rejects_client_output_path(client, tmp_path, monkeypatch):
    spawned = []

    def _spawn(args, name):
        spawned.append((args, name))
        return SimpleNamespace(pid=1234)

    archive = tmp_path / "managed" / "backup.zip"
    monkeypatch.setattr(web_server, "_spawn_hermes_action", _spawn)
    monkeypatch.setattr(web_server, "_new_dashboard_backup_path", lambda: archive)

    rejected = client.post(
        "/api/ops/backup", json={"output": str(tmp_path / "attacker.zip")}
    )
    accepted = client.post("/api/ops/backup", json={})

    assert rejected.status_code == 400
    assert accepted.status_code == 200
    assert accepted.json()["archive"] == str(archive)
    assert spawned == [(["backup", "-o", str(archive)], "backup")]
    assert archive.parent.is_dir()


def test_fs_endpoints_require_auth(tmp_path):
    client = TestClient(web_server.app)
    target = tmp_path / "secret.txt"
    target.write_text("secret")

    list_response = client.get("/api/fs/list", params={"path": str(tmp_path)})
    read_response = client.get("/api/fs/read-text", params={"path": str(target)})
    default_response = client.get("/api/fs/default-cwd")

    assert list_response.status_code == 401
    assert read_response.status_code == 401
    assert default_response.status_code == 401
