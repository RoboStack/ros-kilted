#!/usr/bin/env python3
"""Validate that the latest published ROS packages use a consistent mutex generation.

This checks the current published channel state, not the local recipes. It is meant to
catch partial migrations where some latest packages still depend on an older
``ros2-distro-mutex`` generation while the repository has already moved ``vinca.yaml``
to a newer one.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CHANNEL_BASE = "https://prefix.dev/robostack-kilted"
DEFAULT_PLATFORMS = ["linux-64", "linux-aarch64", "osx-64", "osx-arm64", "win-64"]
USER_AGENT = "Mozilla/5.0 (compatible; RoboStack mutex consistency checker)"
PACKAGE_PREFIX = "ros-kilted-"
MUTEX_NAME = "ros2-distro-mutex"


@dataclass(frozen=True)
class LatestPackage:
    name: str
    filename: str
    version: str
    build_number: int
    depends: tuple[str, ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check that the latest published ROS packages in a channel all depend on "
            "the mutex generation declared in vinca.yaml."
        )
    )
    parser.add_argument(
        "--channel-base",
        default=DEFAULT_CHANNEL_BASE,
        help=(
            "Channel base URL, without platform suffix "
            f"(default: {DEFAULT_CHANNEL_BASE})"
        ),
    )
    parser.add_argument(
        "--platform",
        action="append",
        default=[],
        help=(
            "Platform subdir to inspect (repeatable). "
            "Defaults to the standard RoboStack platforms."
        ),
    )
    parser.add_argument(
        "--vinca-file",
        default="vinca.yaml",
        help="Path to the vinca.yaml file that declares mutex_package.version.",
    )
    parser.add_argument(
        "--expected-mutex-version",
        default=None,
        help="Override the expected mutex version instead of reading it from vinca.yaml.",
    )
    parser.add_argument(
        "--package-prefix",
        default=PACKAGE_PREFIX,
        help=f"Only inspect packages with this prefix (default: {PACKAGE_PREFIX}).",
    )
    parser.add_argument(
        "--ignore-package",
        action="append",
        default=[],
        help="Package name to ignore (repeatable).",
    )
    parser.add_argument(
        "--max-report",
        type=int,
        default=20,
        help="Maximum number of offending packages to print per category (default: 20).",
    )
    return parser.parse_args()


def read_expected_mutex_version(vinca_file: Path) -> str:
    in_mutex_block = False
    for raw_line in vinca_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line.startswith(" ") and stripped == "mutex_package:":
            in_mutex_block = True
            continue
        if in_mutex_block and not line.startswith(" "):
            break
        if in_mutex_block:
            match = re.match(r'\s*version:\s*["\']?([^"\']+)["\']?\s*$', line)
            if match:
                return match.group(1)
    raise ValueError(f"Could not find mutex_package.version in {vinca_file}")


def normalize_mutex_generation(version_or_spec: str) -> str | None:
    match = re.search(r"(\d+)\.(\d+)", version_or_spec)
    if not match:
        return None
    return f"{match.group(1)}.{match.group(2)}"


def version_key(version: str) -> tuple[tuple[int, int | str], ...]:
    key: list[tuple[int, int | str]] = []
    for part in re.split(r"[^0-9A-Za-z]+", version):
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part))
    return tuple(key)


def fetch_repodata(channel_base: str, platform: str) -> dict:
    url = f"{channel_base.rstrip('/')}/{platform}/repodata.json"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.load(response)


def latest_packages(repodata: dict, package_prefix: str, ignored_packages: set[str]) -> dict[str, LatestPackage]:
    latest: dict[str, tuple[tuple[tuple[int, int | str], ...], int, LatestPackage]] = {}
    for package_set in ("packages.conda", "packages"):
        for filename, metadata in repodata.get(package_set, {}).items():
            name = metadata.get("name")
            if not name or not name.startswith(package_prefix) or name in ignored_packages:
                continue
            candidate = LatestPackage(
                name=name,
                filename=filename,
                version=str(metadata.get("version", "")),
                build_number=int(metadata.get("build_number", 0)),
                depends=tuple(metadata.get("depends", [])),
            )
            ordering = (version_key(candidate.version), candidate.build_number, candidate)
            current = latest.get(name)
            if current is None or ordering[:2] > current[:2]:
                latest[name] = ordering
    return {name: item[2] for name, item in latest.items()}


def mutex_generations(depends: tuple[str, ...]) -> set[str]:
    generations: set[str] = set()
    for dependency in depends:
        if not dependency.startswith(MUTEX_NAME):
            continue
        generation = normalize_mutex_generation(dependency)
        if generation:
            generations.add(generation)
    return generations


def print_package_list(title: str, packages: list[LatestPackage], max_report: int) -> None:
    print(f"{title}: {len(packages)}")
    for package in packages[:max_report]:
        mutex_deps = [dep for dep in package.depends if dep.startswith(MUTEX_NAME)]
        print(
            "  - "
            f"{package.name} {package.version} build {package.build_number} "
            f"({package.filename}) -> {mutex_deps or ['<missing>']}"
        )
    remaining = len(packages) - max_report
    if remaining > 0:
        print(f"  ... {remaining} more")


def main() -> int:
    args = parse_args()
    expected_version = args.expected_mutex_version
    if expected_version is None:
        expected_version = read_expected_mutex_version(Path(args.vinca_file))
    expected_generation = normalize_mutex_generation(expected_version)
    if expected_generation is None:
        raise ValueError(f"Could not parse expected mutex generation from {expected_version!r}")

    platforms = args.platform or DEFAULT_PLATFORMS
    ignored_packages = set(args.ignore_package)
    exit_code = 0

    for index, platform in enumerate(platforms):
        try:
            repodata = fetch_repodata(args.channel_base, platform)
        except urllib.error.URLError as exc:
            print(f"Platform: {platform}")
            print(f"Failed to fetch repodata: {exc}")
            exit_code = 1
            if index != len(platforms) - 1:
                print("\n" + "-" * 72 + "\n")
            continue

        latest = latest_packages(repodata, args.package_prefix, ignored_packages)
        generation_counts: Counter[str] = Counter()
        missing_mutex: list[LatestPackage] = []
        unexpected_mutex: list[LatestPackage] = []

        for package in latest.values():
            generations = mutex_generations(package.depends)
            if not generations:
                missing_mutex.append(package)
                continue
            generation_counts.update(generations)
            if generations != {expected_generation}:
                unexpected_mutex.append(package)

        print(f"Platform: {platform}")
        print(f"Expected mutex generation: {expected_generation}")
        print(f"Latest packages checked: {len(latest)}")
        if generation_counts:
            counts = ", ".join(
                f"{generation}={count}" for generation, count in sorted(generation_counts.items())
            )
            print(f"Observed mutex generations: {counts}")
        else:
            print("Observed mutex generations: none")

        print_package_list("Packages on an unexpected mutex generation", sorted(unexpected_mutex, key=lambda item: item.name), args.max_report)
        print_package_list("Packages missing a mutex dependency", sorted(missing_mutex, key=lambda item: item.name), args.max_report)

        if unexpected_mutex or missing_mutex:
            exit_code = 1

        if index != len(platforms) - 1:
            print("\n" + "-" * 72 + "\n")

    if exit_code == 0:
        print("Channel mutex consistency check passed.")
    else:
        print(
            "Channel mutex consistency check failed. "
            "This usually means the published channel is in a partial migration state."
        )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())