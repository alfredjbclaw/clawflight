"""Structural checks for commands advertised to a pip-installed reader."""
from pathlib import Path


README = Path(__file__).resolve().parent.parent / "README.md"


def test_quickstart_shell_blocks_only_use_installed_commands() -> None:
    lines = README.read_text(encoding="utf-8").splitlines()
    start = lines.index("## Quickstart") + 1
    end = next(
        index for index in range(start, len(lines))
        if lines[index].startswith("## ")
    )
    section = lines[start:end]
    blocks = []
    current = None
    for line in section:
        if line == "```sh":
            current = []
        elif line == "```" and current is not None:
            blocks.append(current)
            current = None
        elif current is not None and line.strip():
            current.append(line.strip())

    assert blocks
    for block in blocks:
        for command in block:
            words = command.split()
            assert words and (
                words[0] == "clawflight"
                or (len(words) > 1 and words[:2] == ["pip", "install"])
            )
