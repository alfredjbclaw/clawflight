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


def test_the_bundle_ships_the_licence(bundle) -> None:
    # Without it the registry listed the skill as MIT-0, which grants more than
    # this project does: MIT keeps the attribution requirement.
    licence = (bundle / "LICENSE").read_text(encoding="utf-8")

    assert "MIT License" in licence
    assert "Alfred J Berchtold" in licence


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


# -- listing metadata -------------------------------------------------------


def _frontmatter(bundle: Path) -> str:
    return (bundle / "SKILL.md").read_text(encoding="utf-8").split("---")[1]


def _description(bundle: Path) -> str:
    block = re.search(
        r"description: >-\n((?:[ \t]{2,}.*\n)+)", _frontmatter(bundle)
    )
    assert block is not None, "description must be a folded block scalar"
    return " ".join(line.strip() for line in block.group(1).splitlines())


def test_the_description_stays_short_enough_for_a_system_prompt(bundle) -> None:
    # It is injected into the agent's prompt every session, so length is a real
    # running cost, not just a listing detail.
    assert len(_description(bundle)) <= 260


def test_the_description_carries_its_trigger_words(bundle) -> None:
    # This is what decides whether the agent reaches for the skill at all.
    description = _description(bundle).lower()

    for trigger in ("track", "follow", "mute", "flight", "alert"):
        assert trigger in description, trigger


def test_the_skill_version_matches_the_package(bundle) -> None:
    import clawflight

    version = re.search(r"(?m)^version:\s*(\S+)\s*$", _frontmatter(bundle))

    assert version is not None
    assert version.group(1) == clawflight.__version__


def test_the_skill_declares_a_homepage(bundle) -> None:
    # Shown as "Website" in the Skills UI and the only route back to the source.
    assert "homepage: https://github.com/alfredjbclaw/clawflight" in _frontmatter(bundle)


def test_topics_stay_within_the_registry_limit() -> None:
    # ClawHub rejects a publish carrying more than five topics. Discovered the
    # hard way: a twelve-topic attempt failed after the upload.
    assert len(build_skill_bundle.TOPICS) <= build_skill_bundle.MAX_TOPICS
    assert len(set(build_skill_bundle.TOPICS)) == len(build_skill_bundle.TOPICS)


def test_the_publish_command_pins_the_owner() -> None:
    # Another publisher owns a skill called "clawflight". Resolving the bare
    # slug finds theirs, so --owner is load-bearing, not decoration.
    command = build_skill_bundle.publish_command(Path("/tmp/bundle"), "0.1.0", "abc123")

    assert "--owner alfredjbclaw" in command
    assert "--slug clawflight" in command
    assert "--source-commit abc123" in command
