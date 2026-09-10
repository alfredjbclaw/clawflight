"""The ``clawflight`` command line, driven entirely offline."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from clawflight import __version__
from clawflight.cli import SWEEP_CRON, TICK_CRON, build_parser, cron_recipes, main
from clawflight.config import load_config
from clawflight.registry import Registry


FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
NOW = datetime(2026, 7, 11, 15, 0, tzinfo=timezone.utc).timestamp()


def _config_payload(tmp_path: Path, **overrides) -> dict:
    payload = {
        "owner": "alex",
        "people": json.loads((FIXTURES / "people.json").read_text())["people"],
        "recipients": [
            {
                "key": "alex",
                "name": "Alex",
                "follow_all": True,
                "channel": {"channel": "telegram", "to": "-1009876543210:topic:42"},
            }
        ],
        "mailbox": {
            "adapter": "mbox",
            "path": str(FIXTURES / "inbox.mbox"),
            "trusted_senders": ["air.example"],
        },
        "state_dir": str(tmp_path / "state"),
    }
    payload.update(overrides)
    return payload


def _write_config(tmp_path: Path, **overrides) -> Path:
    path = tmp_path / "clawflight.json"
    path.write_text(json.dumps(_config_payload(tmp_path, **overrides)), encoding="utf-8")
    return path


def _run(capsys, *argv) -> tuple:
    code = main(list(argv))
    return code, capsys.readouterr().out


def _json_run(capsys, *argv) -> tuple:
    code, out = _run(capsys, *argv)
    return code, json.loads(out)


# -- parser -----------------------------------------------------------------


def test_every_documented_verb_is_available() -> None:
    parser = build_parser()
    subparsers = [
        action for action in parser._actions if hasattr(action, "choices") and action.choices
    ]
    verbs = set(subparsers[0].choices)

    assert {
        "setup", "tick", "sweep", "status", "follow", "unfollow", "mute", "unmute", "doctor"
    } <= verbs


def test_no_command_prints_help_and_exits_non_zero(capsys) -> None:
    code, out = _run(capsys)

    assert code == 2
    assert "clawflight" in out


def test_the_version_flag_reports_the_package_version(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_an_unreadable_config_is_reported_not_raised(tmp_path, capsys) -> None:
    path = tmp_path / "clawflight.json"
    path.write_text("{not json", encoding="utf-8")

    code, out = _run(capsys, "--config", str(path), "doctor")

    assert code == 2
    assert "config error" in out


# -- setup ------------------------------------------------------------------


def test_setup_prints_both_cron_recipes_without_running_them(tmp_path, capsys) -> None:
    path = _write_config(tmp_path)

    code, out = _run(capsys, "--config", str(path), "setup")

    assert code == 0
    assert "openclaw cron create" in out
    assert TICK_CRON in out and SWEEP_CRON in out
    assert "clawflight-tick" in out and "clawflight-sweep" in out
    assert (tmp_path / "state").is_dir()


def test_the_cron_recipes_carry_the_config_path() -> None:
    config = load_config(None)

    generic = cron_recipes(config)
    assert all(recipe.startswith("openclaw cron create") for recipe in generic)
    assert all("--session isolated --delivery none" in recipe for recipe in generic)


def test_setup_refuses_to_apply_while_the_config_has_errors(tmp_path, capsys) -> None:
    # Given: a config with no recipients, which cannot deliver anything.
    path = _write_config(tmp_path, recipients=[])

    code, out = _run(capsys, "--config", str(path), "setup", "--apply")

    assert code == 1
    assert "Refusing to apply" in out
    assert "No active recipients" in out


def test_setup_reports_machine_readable_findings(tmp_path, capsys) -> None:
    path = _write_config(tmp_path)

    code, payload = _json_run(capsys, "--config", str(path), "--json", "setup")

    assert code == 0
    assert len(payload["cron"]) == 2
    assert payload["applied"] is False
    assert payload["state_dir"] == str(tmp_path / "state")


# -- tick -------------------------------------------------------------------


def test_tick_exits_immediately_when_nothing_is_in_its_watch_window(
    tmp_path, capsys
) -> None:
    # This is what makes a two-minute cron free while nobody is flying.
    path = _write_config(tmp_path)

    code, payload = _json_run(
        capsys, "--config", str(path), "--json", "tick", "--now", str(NOW)
    )

    assert code == 0
    assert payload == {"checked": 0, "idle": True, "events": []}


def test_tick_assesses_a_flight_inside_the_watch_window(tmp_path, capsys) -> None:
    from clawflight.parse import parse_calendar_events

    path = _write_config(tmp_path)
    config = load_config(path)
    config.ensure_state_dir()
    registry = Registry(str(config.registry_path), config.people)
    registry.merge(
        parse_calendar_events(
            (FIXTURES / "calendar_events.txt").read_text(encoding="utf-8"), 2026
        )
    )

    code, payload = _json_run(
        capsys, "--config", str(path), "--json", "tick", "--dry-run", "--now", str(NOW)
    )

    assert code == 0
    assert payload["idle"] is False
    assert payload["checked"] >= 1
    assert "tracking_started" in {event["kind"] for event in payload["events"]}
    assert payload["errors"] == []


def test_tick_dry_run_never_delivers(tmp_path, capsys) -> None:
    from clawflight.parse import parse_calendar_events

    path = _write_config(tmp_path)
    config = load_config(path)
    config.ensure_state_dir()
    Registry(str(config.registry_path), config.people).merge(
        parse_calendar_events(
            (FIXTURES / "calendar_events.txt").read_text(encoding="utf-8"), 2026
        )
    )

    _json_run(capsys, "--config", str(path), "--json", "tick", "--dry-run", "--now", str(NOW))

    # A dry run leaves no outbox behind for a later real run to drain.
    assert not config.outbox_path.exists()


# -- sweep ------------------------------------------------------------------


def test_sweep_ingests_the_mbox_fixture_into_the_registry(tmp_path, capsys) -> None:
    path = _write_config(tmp_path)

    code, payload = _json_run(
        capsys, "--config", str(path), "--json", "sweep", "--dry-run", "--now", str(NOW)
    )

    assert code == 0
    assert payload["ingested"]["candidates"] == 3
    assert payload["ingested"]["skipped"] == 2
    assert "DL248-2026-08-19" in payload["ingested"]["created"]

    config = load_config(path)
    record = Registry(str(config.registry_path), config.people).get("DL248-2026-08-19")
    assert record is not None
    assert record.person.key == "alex"
    assert record.leg.conf_code == "FAKE20"


def test_sweep_is_idempotent_over_the_same_mailbox(tmp_path, capsys) -> None:
    path = _write_config(tmp_path)

    _json_run(capsys, "--config", str(path), "--json", "sweep", "--dry-run", "--now", str(NOW))
    _, second = _json_run(
        capsys, "--config", str(path), "--json", "sweep", "--dry-run", "--now", str(NOW)
    )

    assert second["ingested"]["created"] == []
    assert second["ingested"]["updated"] == []


def test_a_mailbox_override_selects_a_different_source(tmp_path, capsys) -> None:
    path = _write_config(tmp_path, mailbox={"adapter": "none", "trusted_senders": ["air.example"]})

    _, without = _json_run(
        capsys, "--config", str(path), "--json", "sweep", "--dry-run", "--now", str(NOW)
    )
    _, overridden = _json_run(
        capsys,
        "--config", str(path), "--json", "sweep", "--dry-run",
        "--mailbox", "mbox:{}".format(FIXTURES / "inbox.mbox"),
        "--now", str(NOW),
    )

    assert without["ingested"]["candidates"] == 0
    assert overridden["ingested"]["candidates"] == 3


def test_an_unsupported_mailbox_override_is_reported(tmp_path) -> None:
    from clawflight.config import ConfigError

    path = _write_config(tmp_path)

    with pytest.raises(ConfigError):
        main(
            [
                "--config", str(path), "sweep", "--dry-run",
                "--mailbox", "carrier-pigeon:coop", "--now", str(NOW),
            ]
        )


def test_sweep_promotes_landed_flights_and_prunes_state(tmp_path, capsys) -> None:
    from clawflight.monitor import Monitor

    path = _write_config(tmp_path)
    config = load_config(path)
    config.ensure_state_dir()
    Registry(str(config.registry_path), config.people).merge_email_candidates([])
    monitor_state = {
        "DL999-2026-01-01": {
            "phase": "landed",
            "landed_epoch": NOW - 7200,
            "flight_number": "DL999",
            "last_push_epoch": NOW - 7200,
        }
    }
    config.monitor_path.write_text(json.dumps(monitor_state), encoding="utf-8")

    code, payload = _json_run(
        capsys, "--config", str(path), "--json", "sweep", "--dry-run",
        "--now", str(NOW + 3 * 86400),
    )

    assert code == 0
    assert payload["promoted"] == ["DL999-2026-01-01"]
    # The orphaned state is then pruned because the registry never knew it.
    assert payload["pruned"]["monitor"] == ["DL999-2026-01-01"]
    assert Monitor(str(config.monitor_path)).state_snapshot() == {}


# -- status, follow, mute ---------------------------------------------------


def test_status_reports_nothing_before_anything_is_tracked(tmp_path, capsys) -> None:
    path = _write_config(tmp_path)

    code, out = _run(capsys, "--config", str(path), "status")

    assert code == 0
    assert "No flights tracked yet." in out


def test_status_lists_tracked_flights_in_both_formats(tmp_path, capsys) -> None:
    path = _write_config(tmp_path)
    _json_run(capsys, "--config", str(path), "--json", "sweep", "--dry-run", "--now", str(NOW))

    _, text = _run(capsys, "--config", str(path), "status")
    _, payload = _json_run(capsys, "--config", str(path), "--json", "status")

    assert "DL248-2026-08-19" in text
    assert "Alex" in text
    flight_ids = {row["flight_id"] for row in payload["flights"]}
    assert "DL248-2026-08-19" in flight_ids
    assert all("phase" in row for row in payload["flights"])


def test_follow_and_mute_round_trip_through_the_store(tmp_path, capsys) -> None:
    path = _write_config(tmp_path)

    _run(capsys, "--config", str(path), "follow", "DL248-2026-08-19", "--recipient", "sam")
    _, followed = _json_run(
        capsys, "--config", str(path), "--json", "mute", "DL248-2026-08-19", "--recipient", "sam"
    )

    assert followed["followed"] == ["DL248-2026-08-19"]
    assert followed["muted"] == ["DL248-2026-08-19"]

    _run(capsys, "--config", str(path), "unfollow", "DL248-2026-08-19", "--recipient", "sam")
    _, cleared = _json_run(
        capsys, "--config", str(path), "--json", "unmute", "DL248-2026-08-19", "--recipient", "sam"
    )

    assert cleared["followed"] == []
    assert cleared["muted"] == []


def test_follow_defaults_to_the_configured_owner(tmp_path, capsys) -> None:
    path = _write_config(tmp_path)

    _, payload = _json_run(
        capsys, "--config", str(path), "--json", "follow", "DL248-2026-08-19"
    )

    assert payload["recipient"] == "alex"


def test_follow_without_a_recipient_or_owner_is_an_error(tmp_path, capsys) -> None:
    path = _write_config(tmp_path, owner="")

    code, out = _run(capsys, "--config", str(path), "follow", "DL248-2026-08-19")

    assert code == 2
    assert "no recipient" in out


# -- doctor -----------------------------------------------------------------


def test_doctor_reports_a_complete_config_as_healthy(tmp_path, capsys) -> None:
    path = _write_config(tmp_path)

    code, payload = _json_run(capsys, "--config", str(path), "--json", "doctor")

    assert code == 0
    assert payload["ok"] is True
    assert [f for f in payload["config_findings"] if f["severity"] == "error"] == []
    assert payload["audit_summary"]["critical"] == 0
    assert payload["outbound_cli"]["binary"] == "openclaw"


def test_doctor_exits_non_zero_and_explains_an_incomplete_config(tmp_path, capsys) -> None:
    path = _write_config(tmp_path, recipients=[], owner="")

    code, out = _run(capsys, "--config", str(path), "doctor")

    assert code == 1
    assert "No active recipients" in out
    assert "clawflight audit report" in out


def test_doctor_runs_on_a_completely_empty_configuration(tmp_path, capsys) -> None:
    # A fresh install must be able to ask "what do I still need?"
    code, out = _run(
        capsys, "--config", str(tmp_path / "nope.json"), "--state-dir", str(tmp_path), "doctor"
    )

    assert code == 1
    assert "No people configured" in out
    assert "Polling-only mode" in out


def test_the_state_dir_flag_overrides_the_config_file(tmp_path, capsys) -> None:
    path = _write_config(tmp_path)
    override = tmp_path / "elsewhere"

    _, payload = _json_run(
        capsys, "--config", str(path), "--state-dir", str(override), "--json", "doctor"
    )

    assert payload["state_dir"] == str(override)
