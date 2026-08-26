#!/usr/bin/env python3
"""Upload built artifacts only after validating the selected platforms are complete.

The script regenerates the expected recipes per platform in an isolated temporary copy of
the repository, compares them with the local output artifacts, and refuses to upload when
any generated recipe is still missing a package artifact.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

from build_gap_report import CONDA_SUFFIX, TARBZ2_SUFFIX, gap_report_for_platform, recipe_directories

DEFAULT_OWNER = "robostack-kilted"
DEFAULT_BATCH_SIZE = 200


def default_api_key() -> str | None:
    return os.environ.get("ANACONDA_API_KEY") or os.environ.get("ANACONDA_API_TOKEN")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate that the selected platforms have complete local build outputs "
            "before uploading any artifacts to Anaconda.org."
        )
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root used to regenerate recipes (default: current directory).",
    )
    parser.add_argument(
        "--output-dir",
        default="output",
        help="Directory containing built artifacts grouped by platform (default: output).",
    )
    parser.add_argument(
        "--platform",
        action="append",
        default=[],
        help=(
            "Platform to validate and upload (repeatable). "
            "Defaults to the platform folders that currently contain package artifacts."
        ),
    )
    parser.add_argument(
        "--owner",
        default=DEFAULT_OWNER,
        help=f"Anaconda owner to upload to (default: {DEFAULT_OWNER}).",
    )
    parser.add_argument(
        "--api-key",
        default=default_api_key(),
        help="Anaconda API key. Defaults to ANACONDA_API_KEY or ANACONDA_API_TOKEN if set.",
    )
    parser.add_argument(
        "--channel",
        default=None,
        help="Optional Anaconda label/channel to upload to, for example rc.",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="Optional alternate Anaconda server URL.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"How many package files to upload per command invocation (default: {DEFAULT_BATCH_SIZE}).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Pass --force to rattler-build upload.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate readiness and print the upload plan without uploading.",
    )
    parser.add_argument(
        "--skip-validate",
        action="store_true",
        help="Skip completeness validation and upload whatever is present in output/. Use sparingly.",
    )
    return parser.parse_args()


def chunked(values: list[Path], chunk_size: int) -> Iterable[list[Path]]:
    for index in range(0, len(values), chunk_size):
        yield values[index : index + chunk_size]


def artifact_files_for_platform(output_dir: Path, platform: str) -> list[Path]:
    platform_dir = output_dir / platform
    if not platform_dir.exists():
        return []
    return [
        path
        for path in sorted(platform_dir.iterdir())
        if path.is_file()
        and (path.name.endswith(CONDA_SUFFIX) or path.name.endswith(TARBZ2_SUFFIX))
    ]


def discover_platforms(output_dir: Path) -> list[str]:
    if not output_dir.exists():
        return []
    platforms: list[str] = []
    for entry in sorted(output_dir.iterdir()):
        if not entry.is_dir():
            continue
        artifact_files = artifact_files_for_platform(output_dir, entry.name)
        if artifact_files:
            platforms.append(entry.name)
    return platforms


def create_repo_copy(repo_root: Path, temp_root: Path, platform: str) -> Path:
    copy_root = temp_root / platform
    shutil.copytree(
        repo_root,
        copy_root,
        ignore=shutil.ignore_patterns(
            ".git",
            ".pixi",
            "output",
            "recipes",
            "recipes_only_patch",
            "__pycache__",
            "*.pyc",
        ),
    )
    return copy_root


def generate_recipes(repo_copy: Path, platform: str) -> Path:
    recipes_dir = repo_copy / "recipes"
    subprocess.run(
        ["vinca", "--platform", platform, "-m", "-n"],
        cwd=repo_copy,
        check=True,
    )
    if not recipes_dir.exists():
        raise RuntimeError(f"vinca did not generate recipes for {platform}")
    return recipes_dir


def validate_platforms(repo_root: Path, output_dir: Path, platforms: list[str]) -> int:
    exit_code = 0
    with tempfile.TemporaryDirectory(prefix="ros-kilted-release-") as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        for index, platform in enumerate(platforms):
            repo_copy = create_repo_copy(repo_root, temp_dir, platform)
            recipes_dir = generate_recipes(repo_copy, platform)
            recipes = recipe_directories(recipes_dir)
            _, extra, missing = gap_report_for_platform(output_dir, recipes_dir, platform)

            print(f"Platform: {platform}")
            print(f"Generated recipes: {len(recipes)}")
            print(f"Built artifacts without matching recipe: {len(extra)}")
            if extra:
                for package in sorted(extra)[:20]:
                    print(f"  - {package}")
                if len(extra) > 20:
                    print(f"  ... {len(extra) - 20} more")
            print(f"Missing built artifacts: {len(missing)}")
            if missing:
                for package in sorted(missing)[:20]:
                    print(f"  - {package}")
                if len(missing) > 20:
                    print(f"  ... {len(missing) - 20} more")
                exit_code = 1

            if index != len(platforms) - 1:
                print("\n" + "-" * 72 + "\n")

    return exit_code


def upload_files(args: argparse.Namespace, files: list[Path]) -> int:
    if not args.api_key:
        print("No Anaconda API key configured. Set ANACONDA_API_KEY or ANACONDA_API_TOKEN.")
        return 1

    for batch_index, batch in enumerate(chunked(files, args.batch_size), start=1):
        cmd = [
            "rattler-build",
            "upload",
            "anaconda",
            "--owner",
            args.owner,
            "--api-key",
            args.api_key,
        ]
        if args.channel:
            cmd.extend(["--channel", args.channel])
        if args.url:
            cmd.extend(["--url", args.url])
        if args.force:
            cmd.append("--force")
        cmd.extend(str(path) for path in batch)
        print(
            f"Uploading batch {batch_index} with {len(batch)} artifacts to owner {args.owner}"
            + (f" channel {args.channel}" if args.channel else "")
        )
        subprocess.run(cmd, check=True)
    return 0


def main() -> int:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()

    if not output_dir.exists():
        print(f"Output directory does not exist: {output_dir}")
        return 1

    platforms = args.platform or discover_platforms(output_dir)
    if not platforms:
        print(
            "No platform output folders with built package artifacts were found under "
            f"{output_dir}."
        )
        return 1

    if not args.skip_validate:
        validation_exit_code = validate_platforms(repo_root, output_dir, platforms)
        if validation_exit_code != 0:
            print("Release readiness check failed. Refusing to upload partial outputs.")
            return validation_exit_code

    files_to_upload: list[Path] = []
    for platform in platforms:
        files = artifact_files_for_platform(output_dir, platform)
        print(f"Platform {platform} artifacts queued for upload: {len(files)}")
        files_to_upload.extend(files)

    if not files_to_upload:
        print("No package artifacts found to upload.")
        return 1

    if args.dry_run:
        print("Dry run only. Validation passed; no upload was performed.")
        return 0

    return upload_files(args, files_to_upload)


if __name__ == "__main__":
    sys.exit(main())
