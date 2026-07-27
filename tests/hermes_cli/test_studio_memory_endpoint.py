"""Contract tests for the iOS/Hermes Studio editable-memory endpoint."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server


@pytest.fixture
def studio_memory_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "profiles" / "reviewer").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))

    previous = {
        name: (hasattr(web_server.app.state, name), getattr(web_server.app.state, name, None))
        for name in ("bound_host", "bound_port", "auth_required")
    }
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = 9119
    web_server.app.state.auth_required = False
    client = TestClient(web_server.app, base_url="http://127.0.0.1:9119")
    client.headers["X-Hermes-Session-Token"] = web_server._SESSION_TOKEN
    try:
        yield client, home
    finally:
        client.close()
        for name, (existed, value) in previous.items():
            if existed:
                setattr(web_server.app.state, name, value)
            elif hasattr(web_server.app.state, name):
                delattr(web_server.app.state, name)


def test_get_returns_empty_documents_for_new_profile(studio_memory_client):
    client, _ = studio_memory_client

    response = client.get("/api/hermes/memory", params={"profile": "reviewer"})

    assert response.status_code == 200
    assert response.json() == {
        "memory": "",
        "memory_mtime": None,
        "soul": "",
        "soul_mtime": None,
        "user": "",
        "user_mtime": None,
    }


@pytest.mark.parametrize(
    ("section", "relative_path"),
    [
        ("memory", Path("memories") / "MEMORY.md"),
        ("soul", Path("SOUL.md")),
        ("user", Path("memories") / "USER.md"),
    ],
)
def test_put_atomically_updates_each_section_and_returns_snapshot(
    studio_memory_client,
    section: str,
    relative_path: Path,
):
    client, home = studio_memory_client
    content = f"# {section}\n\nprofile-specific content\n"

    response = client.put(
        "/api/hermes/memory",
        params={"profile": "reviewer"},
        json={"section": section, "content": content},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload[section] == content
    assert isinstance(payload[f"{section}_mtime"], (int, float))
    target = home / "profiles" / "reviewer" / relative_path
    assert target.read_text(encoding="utf-8") == content
    assert not list(target.parent.glob(f".{target.stem}_*.tmp"))


def test_profiles_are_isolated(studio_memory_client):
    client, home = studio_memory_client

    response = client.put(
        "/api/hermes/memory",
        params={"profile": "reviewer"},
        json={"section": "memory", "content": "reviewer only"},
    )

    assert response.status_code == 200
    default_payload = client.get(
        "/api/hermes/memory",
        params={"profile": "default"},
    ).json()
    assert default_payload["memory"] == ""
    assert not (home / "memories" / "MEMORY.md").exists()
    assert (
        home / "profiles" / "reviewer" / "memories" / "MEMORY.md"
    ).read_text(encoding="utf-8") == "reviewer only"


@pytest.mark.parametrize(
    ("profile", "status_code"),
    [("../outside", 400), ("Reviewer", 400), ("missing", 404)],
)
def test_profile_validation_is_fail_closed(
    studio_memory_client,
    profile: str,
    status_code: int,
):
    client, _ = studio_memory_client

    response = client.get("/api/hermes/memory", params={"profile": profile})

    assert response.status_code == status_code


def test_invalid_section_is_rejected_without_creating_files(studio_memory_client):
    client, home = studio_memory_client

    response = client.put(
        "/api/hermes/memory",
        params={"profile": "reviewer"},
        json={"section": "system", "content": "not allowed"},
    )

    assert response.status_code == 400
    assert not (home / "profiles" / "reviewer" / "memories").exists()


def test_atomic_write_failure_preserves_previous_document(
    studio_memory_client,
    monkeypatch: pytest.MonkeyPatch,
):
    client, home = studio_memory_client
    target = home / "profiles" / "reviewer" / "memories" / "MEMORY.md"
    target.parent.mkdir(parents=True)
    target.write_text("previous", encoding="utf-8")

    def fail_replace(_source, _target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("utils.atomic_replace", fail_replace)
    response = client.put(
        "/api/hermes/memory",
        params={"profile": "reviewer"},
        json={"section": "memory", "content": "new"},
    )

    assert response.status_code == 500
    assert target.read_text(encoding="utf-8") == "previous"
    assert not list(target.parent.glob(".MEMORY_*.tmp"))


def test_legacy_profile_soul_route_uses_the_same_atomic_write_boundary(
    studio_memory_client,
    monkeypatch: pytest.MonkeyPatch,
):
    client, home = studio_memory_client
    target = home / "profiles" / "reviewer" / "SOUL.md"
    target.write_text("previous soul", encoding="utf-8")

    def fail_replace(_source, _target):
        raise OSError("simulated soul replace failure")

    monkeypatch.setattr("utils.atomic_replace", fail_replace)
    response = client.put(
        "/api/profiles/reviewer/soul",
        json={"content": "new soul"},
    )

    assert response.status_code == 500
    assert target.read_text(encoding="utf-8") == "previous soul"
    assert not list(target.parent.glob(".SOUL_*.tmp"))


def test_endpoint_remains_session_authenticated(studio_memory_client):
    client, _ = studio_memory_client
    del client.headers["X-Hermes-Session-Token"]

    response = client.get("/api/hermes/memory", params={"profile": "default"})

    assert response.status_code == 401
