"""The ClawHub skill bundle must work the moment it is installed.

A skill that first tells you to pip-install something is a skill most people
abandon at step one, and a gated skill that does not load just looks broken.
The engine has no dependencies, so the bundle carries it outright.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import build_skill_bundle  # noqa: E402


@pytest.fixture(scope="module")
def bundle(tmp_path_factory) -> Path:
    return build_skill_bundle.build(tmp_path_factory.mktemp("skill-dist"))


def test_the_bundle_carries_skill_launcher_and_engine(bundle) -> None:
    assert (bundle / "SKILL.md").is_file()
    assert (bundle / "clawflight").is_file()
    assert (bundle / "engine" / "clawflight" / "cli.py").is_file()
    # Data, not code, and the file most likely to be lost by a careless copy.
    assert (bundle / "engine" / "clawflight" / "data" / "airports.csv").is_file()


def test_the_launcher_is_executable(bundle) -> None:
    assert bundle.joinpath("clawflight").stat().st_mode & 0o111


def test_the_bundle_excludes_build_droppings(bundle) -> None:
    assert not list(bundle.rglob("__pycache__"))
    assert not list(bundle.rglob("*.pyc"))


def test_the_bundle_name_matches_the_skill_frontmatter(bundle) -> None:
    # The folder name becomes the ClawHub slug unless --slug overrides it, so a
    # mismatch here publishes under a name nobody expects.
    frontmatter = (bundle / "SKILL.md").read_text(encoding="utf-8")
    name = re.search(r"(?m)^name:\s*(\S+)\s*$", frontmatter)

    assert name is not None
    assert name.group(1) == bundle.name == build_skill_bundle.SLUG


def test_the_skill_only_requires_python(bundle) -> None:
    # Gating on a `clawflight` binary would hide the skill from everyone who
    # installed it from ClawHub, since the bundle needs no install.
    frontmatter = (bundle / "SKILL.md").read_text(encoding="utf-8").split("---")[1]

    assert "bins: [python3]" in frontmatter
    assert "clawflight]" not in frontmatter


def test_skill_instructions_reference_the_bundled_launcher(bundle) -> None:
    body = (bundle / "SKILL.md").read_text(encoding="utf-8")

    assert "{baseDir}/clawflight" in body
    # No instruction should tell the agent to run a bare `clawflight ...`,
    # which only exists for people who also pip-installed the package.
    stray = re.findall(r"(?m)^\s*[`|]?\s*clawflight (?:status|follow|mute|doctor|setup|sweep)", body)
    assert stray == []


def test_the_bundled_cli_runs_with_no_install(bundle) -> None:
    build_skill_bundle.verify(bundle)


def test_the_bundled_cli_emits_its_own_path_in_cron_recipes(bundle, tmp_path) -> None:
    """A cron job saying bare `clawflight` would fail for a skill install."""
    result = subprocess.run(
        [sys.executable, str(bundle / "clawflight"), "--state-dir", str(tmp_path), "setup"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert str(bundle / "clawflight") in result.stdout
    assert "openclaw cron create" in result.stdout


def test_rebuilding_replaces_rather_than_accumulates(tmp_path) -> None:
    output = tmp_path / "dist"
    first = build_skill_bundle.build(output)
    stale = first / "engine" / "clawflight" / "leftover.py"
    stale.write_text("# removed upstream\n", encoding="utf-8")

    second = build_skill_bundle.build(output)

    assert not (second / "engine" / "clawflight" / "leftover.py").exists()
