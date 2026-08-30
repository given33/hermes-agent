from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "coding-pi-server" / "sync_collab_web.py"


def _load_sync_module():
    spec = importlib.util.spec_from_file_location("coding_pi_sync_collab_web", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_collab_web_rewrite_is_subpath_scoped_and_removes_analytics(tmp_path):
    module = _load_sync_module()
    destination = tmp_path / "collab-web-dist"
    public = destination / "public"
    public.mkdir(parents=True)
    (destination / "index.html").write_text(
        '<!-- Analytics. room -->\n<script defer src="https://um.can.ac/script.js"></script>'
        '<link rel="manifest" href="./build.webmanifest">',
        encoding="utf-8",
    )
    manifest = {
        "start_url": "/",
        "scope": "/",
        "icons": [{"src": "/favicon.svg"}],
    }
    (destination / "build.webmanifest").write_text(json.dumps(manifest), encoding="utf-8")
    (public / "manifest.webmanifest").write_text(json.dumps(manifest), encoding="utf-8")

    module._rewrite_for_subpath(destination)

    assert "um.can.ac" not in (destination / "index.html").read_text(encoding="utf-8")
    root_manifest = json.loads((destination / "build.webmanifest").read_text(encoding="utf-8"))
    public_manifest = json.loads((public / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert (root_manifest["start_url"], root_manifest["scope"]) == ("./", "./")
    assert root_manifest["icons"][0]["src"] == "./public/favicon.svg"
    assert public_manifest["icons"][0]["src"] == "./favicon.svg"


def test_collab_web_cleanup_refuses_existing_custom_destination(tmp_path):
    module = _load_sync_module()
    custom = tmp_path / "existing"
    custom.mkdir()
    marker = custom / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(SystemExit, match="existing custom destination"):
        module._prepare_static_output(custom)

    assert marker.read_text(encoding="utf-8") == "keep"


def test_tracked_collab_web_assets_are_safe_for_collab_mount():
    output = REPO_ROOT / "coding-pi-server" / "collab-web-dist"
    assert "um.can.ac" not in (output / "index.html").read_text(encoding="utf-8")
    root_manifest = json.loads((output / "8rkmwnqq.webmanifest").read_text(encoding="utf-8"))
    public_manifest = json.loads((output / "public" / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert root_manifest["start_url"] == root_manifest["scope"] == "./"
    assert all(icon["src"].startswith("./public/") for icon in root_manifest["icons"])
    assert public_manifest["start_url"] == public_manifest["scope"] == "./"
    assert all(icon["src"].startswith("./") for icon in public_manifest["icons"])


def test_standalone_requirements_include_runtime_imports():
    requirements = (REPO_ROOT / "coding-pi-server" / "requirements.txt").read_text(
        encoding="utf-8"
    )
    assert "cryptography==50.0.0" in requirements
    assert "httpx==0.28.1" in requirements
    assert "websockets==15.0.1" in requirements
