"""Build the pinned official oh-my-pi collab-web SPA for the standalone service.

The build is intentionally performed from the verified Pi checkout. No file
under the checkout is patched; ``dist`` is generated integration output and is
safe to remove and rebuild whenever the private Pi source ref changes.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = Path.home() / "AppData" / "Local" / "hermes" / "coding-pi" / "source-private"
DEFAULT_DEST = SERVICE_ROOT / "coding-pi-server" / "collab-web-dist"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(DEFAULT_SOURCE), help="Verified oh-my-pi checkout")
    parser.add_argument("--destination", default=str(DEFAULT_DEST), help="Generated static SPA directory")
    parser.add_argument("--bun", default="bun", help="Bun executable")
    args = parser.parse_args()
    source = Path(args.source).expanduser().resolve()
    destination = Path(args.destination).expanduser().resolve()
    package = source / "packages" / "collab-web"
    if not (package / "package.json").is_file():
        raise SystemExit(f"collab-web package was not found: {package}")
    subprocess.run([args.bun, "run", "build"], cwd=str(package), check=True)
    built = package / "dist"
    if not (built / "index.html").is_file():
        raise SystemExit(f"collab-web build did not produce {built / 'index.html'}")
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(built, destination)
    print(f"Prepared official collab-web at {destination}")


if __name__ == "__main__":
    main()
