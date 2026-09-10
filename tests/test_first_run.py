"""The out-of-the-box experience, asserted end to end.

This is the test that would have caught the real problem: every unit passed
while a new user could not get a single flight into the system without
hand-editing JSON and standing up an IMAP mailbox.

It drives the *bundled* CLI as a subprocess — the same artifact ClawHub ships —
from an empty directory, and ends by asserting that a message actually reaches
a recipient's channel.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import build_skill_bundle  # noqa: E402


DEPARTURE = "2026-09-12T16:55:00-04:00"
# Inside the six-hour watch window that opens at 14:55Z.
IN_WINDOW = datetime(2026, 9, 12, 15, 30, tzinfo=timezone.utc).timestamp()


@pytest.fixture(scope="module")
def launcher(tmp_path_factory) -> Path:
    return build_skill_bundle.build(tmp_path_factory.mktemp("first-run")) / "clawflight"


@pytest.fixture
def home(tmp_path) -> Path:
    (tmp_path / "bin").mkdir()
    (tmp_path / "sends").mkdir()
    # A stand-in for the real openclaw CLI. One file per invocation, because
    # alert text is multi-line and counting log lines counts lines, not sends.
    stub = tmp_path / "bin" / "openclaw"
    stub.write_text(
        '#!/usr/bin/env bash\n'
        'out=$(mktemp "{}/sends/send-XXXXXX")\n'
        'printf \'%s\' "$*" > "$out"\n'
        'exit 0\n'.format(tmp_path),
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return tmp_path


def _run(launcher: Path, home: Path, *arguments, expect: int = 0):
    import os

    environment = dict(os.environ)
    environment["PATH"] = "{}:{}".format(home / "bin", environment.get("PATH", ""))
    result = subprocess.run(
        [
            sys.executable, str(launcher),
            "--config", str(home / "clawflight.json"),
            "--state-dir", str(home / "state"),
            *arguments,
        ],
        cwd=str(home),
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == expect, (
        "clawflight {}\nexit {}\n{}{}".format(
            " ".join(arguments), result.returncode, result.stdout, result.stderr
        )
    )
    return result.stdout


def _sent(home: Path):
    """Every message the stub was asked to send, one entry per invocation."""
    return [
        path.read_text(encoding="utf-8")
        for path in sorted((home / "sends").iterdir())
    ]


def test_a_brand_new_install_reports_what_it_needs(launcher, home) -> None:
    # doctor exits 1 because an unconfigured install genuinely is not ready.
    output = _run(launcher, home, "doctor", expect=1)

    assert "No people configured" in output
    assert "No active recipients" in output


def test_a_person_can_set_the_whole_thing_up_and_get_an_alert(launcher, home) -> None:
    _run(launcher, home, "person", "add", "alex", "--name", "Alex", "--match", "alex kestrel")
    _run(launcher, home, "config", "set", "owner", "alex")
    _run(
        launcher, home, "recipient", "add", "alex", "--name", "Alex",
        "--channel", "telegram", "--to", "-1001234567890", "--follow-all",
    )

    # Nothing ingested, no mailbox, no API key — just a flight typed in.
    added = _run(
        launcher, home, "flight", "add", "DL767", "--date", "2026-09-12",
        "--from", "JFK", "--to", "LAX", "--depart", "16:55", "--arrive", "20:20",
        "--person", "alex", "--conf", "ABC123", "--seat", "22E",
    )
    assert "added DL767-2026-09-12" in added
    assert "traveler: Alex" in added

    # Configuration is now healthy enough to run.
    _run(launcher, home, "doctor", expect=0)

    report = json.loads(
        _run(launcher, home, "--json", "tick", "--offline", "--now", str(IN_WINDOW))
    )

    assert report["idle"] is False
    assert report["errors"] == []
    assert "tracking_started" in {event["kind"] for event in report["events"]}

    # The point of the whole product: somebody was actually told.
    delivered = _sent(home)
    assert len(delivered) == 1
    assert "--channel telegram" in delivered[0]
    assert "--target -1001234567890" in delivered[0]
    assert "Travel Day" in delivered[0]
    assert "DL767 JFK -> LAX" in delivered[0]
    assert "ABC123" in delivered[0] and "Seat 22E" in delivered[0]


def test_the_same_alert_is_never_sent_twice(launcher, home) -> None:
    _run(launcher, home, "person", "add", "alex", "--name", "Alex")
    _run(launcher, home, "config", "set", "owner", "alex")
    _run(
        launcher, home, "recipient", "add", "alex", "--name", "Alex",
        "--channel", "telegram", "--to", "-100", "--follow-all",
    )
    _run(
        launcher, home, "flight", "add", "DL767", "--date", "2026-09-12",
        "--from", "JFK", "--to", "LAX", "--depart", "16:55", "--person", "alex",
    )

    _run(launcher, home, "tick", "--offline", "--now", str(IN_WINDOW))
    first = len(_sent(home))
    _run(launcher, home, "tick", "--offline", "--now", str(IN_WINDOW + 120))

    assert first == 1
    assert len(_sent(home)) == first


def test_a_flight_with_no_departure_time_warns_instead_of_going_quiet(
    launcher, home
) -> None:
    # Without a departure instant the watch window can never open, so the
    # flight would sit there forever producing nothing.
    output = _run(launcher, home, "flight", "add", "DL767", "--date", "2026-09-12")

    assert "never be watched" in output
    assert "--depart" in output


def test_muting_stops_delivery_for_that_person(launcher, home) -> None:
    _run(launcher, home, "person", "add", "alex", "--name", "Alex")
    _run(launcher, home, "config", "set", "owner", "alex")
    _run(
        launcher, home, "recipient", "add", "alex", "--name", "Alex",
        "--channel", "telegram", "--to", "-100", "--follow-all",
    )
    _run(
        launcher, home, "flight", "add", "DL767", "--date", "2026-09-12",
        "--from", "JFK", "--to", "LAX", "--depart", "16:55", "--person", "alex",
    )
    _run(launcher, home, "mute", "DL767-2026-09-12")

    _run(launcher, home, "tick", "--offline", "--now", str(IN_WINDOW))

    assert _sent(home) == []


def test_removing_a_flight_stops_it_being_tracked(launcher, home) -> None:
    _run(
        launcher, home, "flight", "add", "DL767", "--date", "2026-09-12",
        "--from", "JFK", "--to", "LAX", "--depart", "16:55",
    )
    _run(launcher, home, "flight", "remove", "DL767-2026-09-12")

    status = json.loads(_run(launcher, home, "--json", "status"))

    assert status["flights"] == []


def test_settings_round_trip_through_the_config_file(launcher, home) -> None:
    _run(launcher, home, "config", "set", "horizon_days", "7")
    _run(launcher, home, "config", "set", "mailbox.adapter", "imap")

    shown = json.loads(_run(launcher, home, "--json", "config", "show"))

    assert shown["horizon_days"] == 7
    assert shown["mailbox"]["adapter"] == "imap"
    assert json.loads((home / "clawflight.json").read_text())["horizon_days"] == 7


def test_a_credential_is_refused_at_the_command_line(launcher, home) -> None:
    output = _run(launcher, home, "config", "set", "mailbox.password", "hunter2", expect=2)

    assert "environment variable" in output
    assert "password_env" in output
    config = home / "clawflight.json"
    if config.exists():
        assert "hunter2" not in config.read_text(errors="ignore")


def test_errors_are_explained_rather_than_traced(launcher, home) -> None:
    for arguments, expected in (
        (("flight", "add", "Delta 767", "--date", "2026-09-12"), "DL767"),
        (("flight", "add", "DL767", "--date", "yesterday"), "YYYY-MM-DD"),
        (("person", "remove", "nobody"), "no person"),
        (("config", "set", "nonsense", "1"), "Settable"),
    ):
        output = _run(launcher, home, *arguments, expect=2)
        assert expected in output
        assert "Traceback" not in output
