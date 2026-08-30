"""Build the pinned official oh-my-pi collab-web SPA for the standalone service.

The build is intentionally performed from the verified Pi checkout. No file
under the checkout is patched; ``dist`` is generated integration output and is
safe to remove and rebuild whenever the private Pi source ref changes.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = Path.home() / "AppData" / "Local" / "hermes" / "coding-pi" / "source-private"
DEFAULT_DEST = SERVICE_ROOT / "coding-pi-server" / "collab-web-dist"


def _prepare_static_output(destination: Path) -> None:
    """Remove only the repository's known generated output directory."""
    if not destination.exists() and not destination.is_symlink():
        return
    if destination.is_symlink() or not destination.is_dir():
        raise SystemExit("Refusing to replace a symlink or non-directory destination")
    if destination.resolve() != DEFAULT_DEST.resolve():
        raise SystemExit(
            "Refusing to remove an existing custom destination; choose a new "
            "path or the repository's collab-web-dist output."
        )
    shutil.rmtree(destination)


def _rewrite_manifest_for_subpath(manifest: Path, destination: Path) -> None:
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    data["start_url"] = "./"
    data["scope"] = "./"
    icon_prefix = "./public/" if manifest.parent == destination else "./"
    icons = data.get("icons")
    if isinstance(icons, list):
        for icon in icons:
            if not isinstance(icon, dict) or not isinstance(icon.get("src"), str):
                continue
            source = icon["src"]
            if source.startswith("/"):
                icon["src"] = icon_prefix + source.lstrip("/")
    manifest.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _rewrite_for_subpath(destination: Path) -> None:
    """Confine PWA resources to /collab and remove the upstream beacon."""
    index = destination / "index.html"
    if index.is_file():
        html = index.read_text(encoding="utf-8")
        html = re.sub(
            r'\s*<!-- Analytics\..*?<script[^>]+src="https://um\.can\.ac/script\.js"[^>]*></script>',
            "",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        index.write_text(html, encoding="utf-8")
    manifests = list(destination.glob("*.webmanifest"))
    manifests.append(destination / "public" / "manifest.webmanifest")
    for manifest in manifests:
        if manifest.is_file():
            _rewrite_manifest_for_subpath(manifest, destination)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(DEFAULT_SOURCE), help="Verified oh-my-pi checkout")
    parser.add_argument("--destination", default=str(DEFAULT_DEST), help="Generated static SPA directory")
    parser.add_argument("--bun", default="bun", help="Bun executable")
    args = parser.parse_args()
    source = Path(args.source).expanduser().resolve()
    destination = Path(args.destination).expanduser().absolute()
    package = source / "packages" / "collab-web"
    if not (package / "package.json").is_file():
        raise SystemExit(f"collab-web package was not found: {package}")
    subprocess.run([args.bun, "run", "build"], cwd=str(package), check=True)
    built = package / "dist"
    if not (built / "index.html").is_file():
        raise SystemExit(f"collab-web build did not produce {built / 'index.html'}")
    _prepare_static_output(destination)
    shutil.copytree(built, destination)
    _rewrite_for_subpath(destination)
    print(f"Prepared official collab-web at {destination}")


if __name__ == "__main__":
    main()
