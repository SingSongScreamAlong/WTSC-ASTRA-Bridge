#!/usr/bin/env python3
"""Install/link the ASTRA Bridge add-on into the newest local Blender install on macOS."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def version_key(path: Path):
    parts = []
    for part in path.name.split("."):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def main() -> None:
    if sys.platform != "darwin":
        raise SystemExit("This helper is for macOS. See SETUP.md for manual installation.")

    parser = argparse.ArgumentParser()
    parser.add_argument("--blender-version", help="Specific Blender version folder, e.g. 4.5")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[1]
    source = repo / "blender_addon" / "astra_bridge"
    if not source.exists():
        raise SystemExit(f"Add-on source not found: {source}")

    base = Path.home() / "Library" / "Application Support" / "Blender"
    if args.blender_version:
        version_dir = base / args.blender_version
    else:
        candidates = [p for p in base.iterdir() if p.is_dir()] if base.exists() else []
        if not candidates:
            raise SystemExit(
                "No Blender user configuration folders were found. "
                "Launch Blender once, close it, then rerun this script."
            )
        version_dir = sorted(candidates, key=version_key)[-1]

    addons = version_dir / "scripts" / "addons"
    addons.mkdir(parents=True, exist_ok=True)
    target = addons / "astra_bridge"

    if target.is_symlink():
        current = target.resolve()
        if current == source.resolve():
            print(f"ASTRA Bridge is already linked for Blender {version_dir.name}:")
            print(target)
            return
        target.unlink()
    elif target.exists():
        raise SystemExit(
            f"Existing non-symlink add-on found at {target}. "
            "Remove/rename it manually before continuing."
        )

    os.symlink(source, target, target_is_directory=True)
    print(f"Installed ASTRA Bridge for Blender {version_dir.name}")
    print(f"  source: {source}")
    print(f"  link:   {target}")
    print()
    print("Next:")
    print("1. Open Blender.")
    print("2. Preferences > Add-ons: enable 'ASTRA Bridge'.")
    print("3. In ASTRA Bridge preferences, set Bridge Repo to:")
    print(f"   {repo}")
    print("4. Save Preferences.")
    print("5. Run ./run_bridge.command from the repo.")


if __name__ == "__main__":
    main()
