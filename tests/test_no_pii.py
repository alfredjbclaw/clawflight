"""The publication gate: no third-party personal data anywhere in this repo.

clawflight is the sanitized public extraction of a private family tracker whose
fixtures contained real third-party data. These tests are the standing
guarantee that none of it ever arrives here — by a fresh paste, a helpful
"realistic" example, or a copied fixture.

The patterns live in ``pii_blocklist.py`` and the scanning in ``pii_scan.py``,
shared with the standalone pre-publication tool so the two can never diverge.
The maintainer's own identity is deliberately published; see
``MAINTAINER_CONTACTS`` and :func:`test_the_contact_route_is_published`.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pii_scan  # noqa: E402
from pii_blocklist import (  # noqa: E402
    MAINTAINER_CONTACTS,
    blocklist,
    without_maintainer,
)


REPO = Path(__file__).resolve().parent.parent


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


def _requires_git():
    try:
        pii_scan.publishable_blobs()
    except pii_scan.ScanError:
        pytest.skip("not a git repository with history")
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git
        pytest.skip("git is not available")


# --------------------------------------------------------------------------
# Content
# --------------------------------------------------------------------------


def test_no_third_party_personal_data_in_the_working_tree() -> None:
    findings, scanned = pii_scan.scan_worktree(strict=False)

    assert scanned > 0, "the scan read no files, so a clean result means nothing"
    assert not findings, "\n".join(findings)


def test_no_third_party_personal_data_in_publishable_history() -> None:
    _requires_git()

    findings, scanned = pii_scan.scan_history(strict=False)

    assert scanned > 0, "the scan read no blobs, so a clean result means nothing"
    assert not findings, "\n".join(findings)


def test_git_commit_metadata_carries_no_third_party_identity() -> None:
    """Author and committer identity is part of what a public repo publishes.

    Content scanners cannot see it: ``git grep`` searches blobs. The
    maintainer's own identity is allowed; a co-author's or family member's real
    mailbox arriving through a stray ``user.email`` is not.
    """
    _requires_git()

    findings = pii_scan.scan_commit_metadata(strict=False)

    assert not findings, (
        "\n".join(findings)
        + "\n\nCommit metadata is published with the code. Use the maintainer\n"
        "identity, or a GitHub noreply address:\n"
        "  git config user.email '<id>+<handle>@users.noreply.github.com'\n"
        "Existing commits need a history rewrite to change."
    )


# --------------------------------------------------------------------------
# Fixture hygiene
# --------------------------------------------------------------------------


def test_every_fixture_address_uses_a_reserved_domain() -> None:
    # RFC 2606 reserves example.com/.example/.test/.invalid/.localhost for
    # documentation. Anything else in a fixture could be a real mailbox.
    address = re.compile(r"[\w.+-]+@([\w.-]+\.[A-Za-z]{2,})")
    reserved = re.compile(
        r"(?:^|\.)(?:example\.com|example\.net|example\.org|example|test|invalid|localhost)$",
        re.IGNORECASE,
    )
    offenders = []

    for path in sorted((REPO / "fixtures").rglob("*")):
        if not path.is_file():
            continue
        for line_number, line in enumerate(_read(path).splitlines(), start=1):
            for domain in address.findall(line):
                if not reserved.search(domain):
                    offenders.append(
                        "{}:{}: {}".format(path.relative_to(REPO), line_number, domain)
                    )

    assert not offenders, "non-reserved domains in fixtures:\n" + "\n".join(offenders)


def test_confirmation_codes_in_fixtures_are_obviously_synthetic() -> None:
    # Real PNRs are six opaque alphanumerics. Ours are always FAKE-prefixed so a
    # reader can tell at a glance that nothing here is a live booking.
    # The label is matched case-insensitively; the code itself is not, so prose
    # like "confirmation codes are synthetic" cannot look like a booking.
    labelled = re.compile(
        r"(?i:confirmation\s*(?:code|number)?|record\s+locator|booking\s+reference|conf#)"
        r"\s*[:#]?\s*\**\s*([A-Z0-9]{5,8})\b"
    )
    offenders = []

    for path in sorted((REPO / "fixtures").rglob("*")):
        if not path.is_file():
            continue
        for line_number, line in enumerate(_read(path).splitlines(), start=1):
            for code in labelled.findall(line):
                if not code.upper().startswith("FAKE"):
                    offenders.append(
                        "{}:{}: {}".format(path.relative_to(REPO), line_number, code)
                    )

    assert not offenders, "non-synthetic confirmation codes:\n" + "\n".join(offenders)


def test_no_module_imports_the_private_package() -> None:
    # The private engine's package name. Prose in EXTRACTION-PLAN.md may name
    # the source project; shipped code may never import from it.
    offenders = [
        str(path.relative_to(REPO))
        for path in (REPO / "clawflight").rglob("*.py")
        if "flight_tracker" in _read(path)
    ]

    assert offenders == []


def test_the_stripped_pii_audit_is_not_reintroduced() -> None:
    # The private project's PII audit catalogued the data by quoting it. It was
    # deliberately removed from this repository's history and must stay gone.
    assert not (REPO / "PII-AUDIT.md").exists()


def test_no_credential_value_is_committed() -> None:
    # Secrets are referenced by environment-variable NAME only.
    assignment = re.compile(
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|token|bearer)\b\s*[:=]\s*"
        r"[\"']([^\"'\n]{8,})[\"']"
    )
    allowed = re.compile(
        r"(?i)_ENV$|^CLAWFLIGHT_|not-a-real-|expected-secret|gate-secret|^\$"
    )
    offenders = []

    for path in pii_scan.worktree_files():
        if path.suffix not in (".py", ".json", ".md", ".sh", ".toml", ".cfg"):
            continue
        for line_number, line in enumerate(_read(path).splitlines(), start=1):
            match = assignment.search(line)
            if match and not allowed.search(match.group(1)):
                offenders.append(
                    "{}:{}: {}".format(
                        path.relative_to(REPO), line_number, line.strip()[:120]
                    )
                )

    assert not offenders, "possible committed credential:\n" + "\n".join(offenders)


# --------------------------------------------------------------------------
# The maintainer allowance
# --------------------------------------------------------------------------


def test_the_contact_route_is_published() -> None:
    """A public project needs somewhere to send a bug report.

    The maintainer's name and address are deliberately published, so this is a
    presence check rather than an absence one: if a future scrub removes the
    only way to reach a human, that is a regression.
    """
    assert "Alfred J Berchtold" in _read(REPO / "LICENSE"), (
        "LICENSE must name a copyright holder"
    )
    assert any(
        contact in _read(REPO / "README.md") for contact in MAINTAINER_CONTACTS
    ), "README must carry a contact route"
    assert "alfred.j.berchtold@gmail.com" in _read(REPO / "pyproject.toml"), (
        "package metadata must carry a maintainer address"
    )


def test_the_maintainer_allowance_is_narrow() -> None:
    """Allowing one address must not allow the whole provider."""
    email = dict((rule.name, rule.pattern) for rule in blocklist())[
        "institutional-email"
    ]

    assert not email.search(without_maintainer("alfred.j.berchtold@gmail.com"))
    assert email.search(without_maintainer("someone.else@gmail.com"))
    assert email.search(without_maintainer("a.relative@icloud.com"))
    # Strict mode withdraws the allowance entirely.
    assert email.search(without_maintainer("alfred.j.berchtold@gmail.com", strict=True))


# --------------------------------------------------------------------------
# The scanner itself
# --------------------------------------------------------------------------


def test_every_rule_matches_something_it_is_meant_to_catch() -> None:
    """A rule that cannot match is a rule that is not protecting anything.

    This exists because a previous shell implementation used ``git grep -E``,
    whose POSIX ERE has no ``\\b``. Several patterns silently matched nothing,
    and every scan reported clean.
    """
    samples = {
        "private-domains": "see notes at team.ledecompany.com",
        "institutional-email": "reply to a.person@cornell.edu please",
        "loyalty-number": "SkyMiles #1234567 on file",
        "eticket-number": "e-ticket number 0062345678901",
        "payment-card": "Amex ending in 1009",
        "us-phone-number": "call 415 555 0142",
        "home-coordinates": "home_address = '...'",
        "maintainer-identity": "maintained by Alfred J Berchtold",
    }
    unmatched = []
    for rule in blocklist(strict=True):
        sample = samples.get(rule.name)
        if sample is None or not rule.pattern.search(sample):
            unmatched.append("{}: did not match {!r}".format(rule.name, sample))

    assert not unmatched, "\n".join(unmatched)


def _run_scan(*arguments):
    return subprocess.run(
        [sys.executable, str(REPO / "tests" / "pii_scan.py"), *arguments],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def test_the_scanner_detects_a_planted_pattern() -> None:
    """A scanner that cannot fail is a scanner that cannot pass."""
    planted = REPO / "planted_for_scan_test.txt"
    assert not planted.exists()
    planted.write_text("contact: someone.else@gmail.com\n", encoding="utf-8")
    try:
        result = _run_scan("--worktree")
    finally:
        planted.unlink()

    assert result.returncode == 1, (
        "the scanner did not report a planted third-party address\n"
        + result.stdout
        + result.stderr
    )
    assert "someone.else@gmail.com" in result.stdout


def test_the_scanner_reports_a_clean_repository() -> None:
    worktree = _run_scan("--worktree")
    history = _run_scan()

    assert worktree.returncode == 0, worktree.stdout + worktree.stderr
    assert history.returncode == 0, history.stdout + history.stderr
    assert "clean:" in worktree.stdout and "clean:" in history.stdout
    # And it says how much it read, so a no-op cannot look like a pass.
    assert "scanned 0 " not in worktree.stdout
    assert "scanned 0 " not in history.stdout


def test_the_scanner_does_not_flag_the_maintainer() -> None:
    # LICENSE, README and pyproject carry the maintainer's identity on purpose.
    result = _run_scan("--worktree")

    assert result.returncode == 0
    assert "alfred.j.berchtold@gmail.com" not in result.stdout


def test_strict_mode_flags_the_maintainer_identity() -> None:
    # The setting for a fork that wants no personal identity at all.
    result = _run_scan("--worktree", "--strict")

    assert result.returncode == 1
    assert "maintainer-identity" in result.stdout


def test_the_shell_shim_still_works() -> None:
    # docs/, the Makefile and muscle memory all name history_scan.sh.
    result = subprocess.run(
        [str(REPO / "tests" / "history_scan.sh"), "--worktree"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "clean:" in result.stdout
