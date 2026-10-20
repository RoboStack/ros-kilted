#!/usr/bin/env python3
"""Delete build artifacts that contain no files although their recipe has a build script.

A cancelled Windows job can leave such packages behind: cmd.exe turns the runner's
Ctrl+C into a "Terminate batch job (Y/N)?" prompt, the build script exits 0, and
rattler-build packages the empty prefix as a successful build. Once saved to the CI
cache, every downstream package then fails to find the missing headers/CMake config.
Metapackages and conda-forge dummies (``script: ''``) are legitimately empty and kept.

Usage: prune_empty_packages.py <output/platform dir> [--dry-run]
"""
from __future__ import annotations

import io
import json
import sys
import tarfile
import zipfile
from pathlib import Path

import yaml
import zstandard


def _info(path: Path) -> tuple[int, object]:
    with zipfile.ZipFile(path) as z:
        name = next(n for n in z.namelist() if n.startswith("info-") and n.endswith(".tar.zst"))
        raw = zstandard.ZstdDecompressor().decompress(z.read(name), max_output_size=512 * 1024 * 1024)
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        files = {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}
    paths = json.loads(files["info/paths.json"]).get("paths", [])
    recipe = yaml.safe_load(files.get("info/recipe/recipe.yaml", b"") or b"{}") or {}
    return len(paths), (recipe.get("build") or {}).get("script", "")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    folder = Path(args[0]) if args else None
    if folder is None or not folder.is_dir():
        print(f"prune_empty_packages: no such directory: {folder}")
        return 0
    removed = 0
    for art in sorted(folder.glob("*.conda")):
        try:
            n_paths, script = _info(art)
        except Exception as exc:  # unreadable artifact: leave it to rattler-build
            print(f"skip {art.name}: {exc}")
            continue
        if n_paths == 0 and script not in ("", None, []):
            print(f"{'would remove' if dry else 'removing'} empty package {art.name} (recipe has a build script but installed nothing)")
            if not dry:
                art.unlink()
            removed += 1
    print(f"prune_empty_packages: {removed} empty package(s) {'found' if dry else 'removed'} in {folder}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
