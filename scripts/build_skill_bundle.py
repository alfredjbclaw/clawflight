#!/usr/bin/env python3
"""Assemble the self-contained ClawHub skill bundle.

The published skill has to work the moment it is installed. A skill that first
tells you to go and pip-install something is a skill most people abandon at
step one, and a gated skill that silently does not load is worse — it just
looks broken.

The engine is pure standard library with no dependencies, so the bundle can
simply carry it:

    dist/skill/clawflight/
      SKILL.md          agent instructions (copied from skill/)
      LICENSE           the project licence, so the listing states it correctly
      clawflight        launcher: puts engine/ on sys.path, calls the CLI
      engine/clawflight/…   the package, verbatim

Nothing is duplicated in git — this is a build artifact. ``make skill-bundle``
produces it and ``--verify`` proves the result actually runs.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SOURCE_SKILL = REPO / "skill" / "SKILL.md"
PACKAGE = REPO / "clawflight"
DEFAULT_OUTPUT = REPO / "dist" / "skill"

#: The skill's folder name becomes its ClawHub slug unless --slug overrides it.
SLUG = "clawflight"

#: ClawHub owner to publish under. Passing this explicitly is not optional:
#: another publisher already owns a skill called "clawflight" (an unrelated
#: Starlink WiFi finder), and resolving the bare slug picks up theirs.
OWNER = "alfredjbclaw"

#: Discovery topics. **The registry rejects more than five.**
#:
#: Chosen to add terms the description does not already contain — searching
#: "flight" already matches the name and description, so these buy breadth
#: (travel, aviation) and browse categories (notifications, family) instead.
TOPICS = ("flight-tracking", "travel", "aviation", "notifications", "family")
MAX_TOPICS = 5

LAUNCHER = '''#!/usr/bin/env python3
"""Run clawflight from inside the skill bundle.

No install step: the engine ships alongside this file and has no dependencies,
so putting its directory on sys.path is the whole bootstrap.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "engine"))

from clawflight.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
'''

EXCLUDE_DIRS = {"__pycache__", ".pytest_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}


def _copy_package(destination: Path) -> int:
    copied = 0
    for source in sorted(PACKAGE.rglob("*")):
        if any(part in EXCLUDE_DIRS for part in source.parts):
            continue
        if source.suffix in EXCLUDE_SUFFIXES:
            continue
        target = destination / source.relative_to(PACKAGE.parent)
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied += 1
    return copied


def build(output_root: Path = DEFAULT_OUTPUT, slug: str = SLUG) -> Path:
    """Build the bundle and return its directory."""
    if not SOURCE_SKILL.is_file():
        raise SystemExit("missing {}".format(SOURCE_SKILL))

    bundle = output_root / slug
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)

    shutil.copy2(SOURCE_SKILL, bundle / "SKILL.md")

    # Ship the licence with the code. Without it ClawHub listed the skill as
    # MIT-0 ("no attribution required"), which is not the licence this project
    # grants — MIT keeps the notice requirement.
    licence = REPO / "LICENSE"
    if not licence.is_file():
        raise SystemExit("missing LICENSE; the bundle would be published unlicensed")
    shutil.copy2(licence, bundle / "LICENSE")

    launcher = bundle / "clawflight"
    launcher.write_text(LAUNCHER, encoding="utf-8")
    launcher.chmod(0o755)

    copied = _copy_package(bundle / "engine")
    if copied == 0:
        raise SystemExit("copied no engine files; the bundle would be empty")

    # The packaged airport table is data, not code, and is the one file most
    # likely to be lost by a careless copy rule.
    if not (bundle / "engine" / "clawflight" / "data" / "airports.csv").is_file():
        raise SystemExit("bundle is missing the packaged airport table")

    return bundle


def verify(bundle: Path) -> None:
    """Run the bundled launcher the way an installed skill would.

    ``cwd`` is deliberately elsewhere and the repository is kept off sys.path,
    so a bundle that only works from a source checkout fails here.
    """
    import os
    import tempfile

    launcher = bundle / "clawflight"
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)

    with tempfile.TemporaryDirectory() as scratch:
        for arguments, expectation in (
            (["--version"], None),
            (["--state-dir", scratch, "--json", "status"], '{"flights": []}'),
        ):
            result = subprocess.run(
                [sys.executable, str(launcher), *arguments],
                cwd=scratch,
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if result.returncode != 0:
                raise SystemExit(
                    "bundled launcher failed for {}:\n{}{}".format(
                        arguments, result.stdout, result.stderr
                    )
                )
            if expectation and expectation not in result.stdout:
                raise SystemExit(
                    "bundled launcher output for {} did not contain {!r}:\n{}".format(
                        arguments, expectation, result.stdout
                    )
                )

        # doctor exits 1 on an unconfigured install; that is correct, not a crash.
        doctor = subprocess.run(
            [sys.executable, str(launcher), "--state-dir", scratch, "doctor"],
            cwd=scratch,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if doctor.returncode != 1:
            raise SystemExit(
                "expected doctor to exit 1 on an unconfigured install, got {}".format(
                    doctor.returncode
                )
            )


def publish_command(bundle: Path, version: str, commit: str) -> str:
    """The exact ClawHub publish invocation, so it is not reinvented by memory."""
    return " ".join(
        [
            "clawhub skill publish {}".format(bundle),
            "--owner {}".format(OWNER),
            "--slug {}".format(SLUG),
            "--version {}".format(version),
            "--topics {}".format(",".join(TOPICS)),
            "--source-repo {}/{}".format(OWNER, SLUG),
            "--source-commit {}".format(commit),
            "--source-ref refs/heads/main",
            "--source-path skill",
        ]
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--slug", default=SLUG)
    parser.add_argument(
        "--verify", action="store_true", help="run the bundled CLI after building"
    )
    parser.add_argument(
        "--print-publish",
        action="store_true",
        help="print the ClawHub publish command for this bundle",
    )
    args = parser.parse_args(argv)

    bundle = build(Path(args.output), args.slug)
    files = sorted(path for path in bundle.rglob("*") if path.is_file())
    size = sum(path.stat().st_size for path in files)
    print("built {} ({} files, {:.0f} KB)".format(bundle, len(files), size / 1024))

    if args.verify:
        verify(bundle)
        print("verified: the bundled CLI runs with no install and no PYTHONPATH")

    if args.print_publish:
        import re

        frontmatter = (bundle / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
        version = re.search(r"(?m)^version:\s*(\S+)\s*$", frontmatter)
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        print()
        print(publish_command(bundle, version.group(1) if version else "0.0.0", commit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
