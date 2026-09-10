"""Config loading, tolerant parsing, path resolution, and doctor validation."""
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from clawflight.config import (
    CONFIG_PATH_ENV,
    DEFAULT_HORIZON_DAYS,
    MAX_HORIZON_DAYS,
    STATE_DIR_ENV,
    Config,
    ConfigError,
    default_config_path,
    from_mapping,
    load_config,
    loads,
    strip_json_comments,
    validate,
    with_state_dir,
)


def _write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "clawflight.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


FULL = {
    "owner": "alex",
    "people": [
        {"key": "alex", "display": "Alex", "match_substrings": ["alex kestrel"]},
        {"key": "sam", "display": "Sam"},
    ],
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
        "path": "fixtures/inbox.mbox",
        "trusted_senders": ["air.example"],
    },
    "horizon_days": 5,
}


def test_a_missing_config_file_yields_working_defaults(tmp_path) -> None:
    # A fresh install must start, attribute nothing, and notify nobody.
    config = load_config(tmp_path / "nope.json")

    assert isinstance(config, Config)
    assert config.owner == ""
    assert len(config.people) == 0
    assert config.recipients.all_active() == []
    assert config.mailbox.adapter == "none"
    assert config.push.enabled is False
    assert config.horizon_days == DEFAULT_HORIZON_DAYS


def test_a_full_config_loads_every_section(tmp_path) -> None:
    config = load_config(_write(tmp_path, FULL))

    assert config.owner == "alex"
    assert [person.key for person in config.people] == ["alex", "sam"]
    assert config.recipients.get("alex").target == "-1009876543210:topic:42"
    assert config.mailbox.adapter == "mbox"
    assert config.mailbox.trusted_senders == ("air.example",)
    assert config.horizon_days == 5
    assert config.source_path is not None


def test_comments_and_trailing_commas_are_tolerated() -> None:
    text = """{
  // the person who gets unknown-person flights
  "owner": "alex",
  /* block comment
     spanning lines */
  "horizon_days": 4,
  "people": [
    {"key": "alex", "display": "Alex"},
  ],
}"""

    payload = loads(text)

    assert payload["owner"] == "alex"
    assert payload["horizon_days"] == 4
    assert len(payload["people"]) == 1


def test_a_double_slash_inside_a_string_is_not_a_comment() -> None:
    text = '{"push": {"webhook_url": "https://hooks.example.test/adb"}}'

    assert loads(text)["push"]["webhook_url"] == "https://hooks.example.test/adb"
    assert "https://" in strip_json_comments(text)


def test_invalid_json_raises_a_config_error(tmp_path) -> None:
    path = tmp_path / "clawflight.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(path)
    with pytest.raises(ConfigError):
        loads("[1, 2, 3]")


def test_unusable_section_values_fall_back_to_defaults(tmp_path) -> None:
    config = load_config(
        _write(
            tmp_path,
            {
                "owner": 17,
                "people": "not-a-list",
                "recipients": {"not": "a list"},
                "mailbox": "not-a-mapping",
                "push": 42,
                "horizon_days": "soon",
            },
        )
    )

    assert config.owner == ""
    assert len(config.people) == 0
    assert config.recipients.all_active() == []
    assert config.mailbox.adapter == "none"
    assert config.push.enabled is False
    assert config.horizon_days == DEFAULT_HORIZON_DAYS


def test_the_horizon_is_clamped_to_a_sane_range(tmp_path) -> None:
    assert load_config(_write(tmp_path, {"horizon_days": -5})).horizon_days == 0
    assert (
        load_config(_write(tmp_path, {"horizon_days": 9999})).horizon_days
        == MAX_HORIZON_DAYS
    )
    assert load_config(_write(tmp_path, {"horizon_days": True})).horizon_days == (
        DEFAULT_HORIZON_DAYS
    )


def test_an_unknown_mailbox_adapter_falls_back_to_none(tmp_path) -> None:
    config = load_config(_write(tmp_path, {"mailbox": {"adapter": "carrier-pigeon"}}))

    assert config.mailbox.adapter == "none"
    assert config.mailbox.enabled is False


def test_state_paths_all_resolve_under_the_state_directory(tmp_path) -> None:
    config = with_state_dir(load_config(_write(tmp_path, FULL)), tmp_path / "state")

    for path in (
        config.registry_path,
        config.monitor_path,
        config.outbox_path,
        config.follows_path,
        config.consent_path,
        config.subscriptions_path,
    ):
        assert path.parent == tmp_path / "state"


def test_the_state_directory_is_created_private(tmp_path) -> None:
    config = with_state_dir(Config(), tmp_path / "state")

    created = config.ensure_state_dir()

    assert created.is_dir()
    assert stat.S_IMODE(created.stat().st_mode) == 0o700


def test_environment_overrides_win_over_the_config_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "from-env"))
    path = _write(tmp_path, {"state_dir": str(tmp_path / "from-file")})

    assert load_config(path).state_dir == tmp_path / "from-env"

    monkeypatch.delenv(STATE_DIR_ENV)
    assert load_config(path).state_dir == tmp_path / "from-file"

    monkeypatch.setenv(CONFIG_PATH_ENV, str(path))
    assert default_config_path() == path


def test_tilde_paths_are_expanded(tmp_path) -> None:
    config = from_mapping({"state_dir": "~/clawflight-state"})

    assert "~" not in str(config.state_dir)
    assert config.state_dir.is_absolute()


def test_config_round_trips_to_a_serialisable_mapping(tmp_path) -> None:
    config = load_config(_write(tmp_path, FULL))

    payload = config.to_dict()

    assert json.loads(json.dumps(payload))["owner"] == "alex"
    rebuilt = from_mapping(payload)
    assert [person.key for person in rebuilt.people] == ["alex", "sam"]
    assert rebuilt.recipients.get("alex").target == "-1009876543210:topic:42"


# -- validation -------------------------------------------------------------


def _messages(config: Config, severity: str) -> list:
    return [f.message for f in validate(config) if f.severity == severity]


def test_a_default_config_reports_the_work_left_to_do() -> None:
    findings = validate(Config())

    warnings = [f.message for f in findings if f.severity == "warning"]
    errors = [f.message for f in findings if f.severity == "error"]
    assert any("No people configured" in message for message in warnings)
    assert any("No owner configured" in message for message in warnings)
    assert any("No active recipients" in message for message in errors)
    assert any("No mailbox adapter" in message for message in warnings)


def test_a_complete_config_validates_without_errors(tmp_path) -> None:
    payload = dict(FULL)
    payload["mailbox"] = {
        "adapter": "mbox",
        "path": str(tmp_path / "inbox.mbox"),
        "trusted_senders": ["air.example"],
    }

    findings = validate(load_config(_write(tmp_path, payload)))

    assert [f for f in findings if f.severity == "error"] == []
    assert any("Polling-only mode" in f.message for f in findings)


def test_an_owner_who_is_not_a_configured_person_is_an_error(tmp_path) -> None:
    payload = dict(FULL, owner="nobody")

    assert any("owner 'nobody'" in message for message in _messages(
        load_config(_write(tmp_path, payload)), "error"
    ))


def test_a_recipient_without_a_channel_target_is_an_error(tmp_path) -> None:
    payload = dict(FULL, recipients=[{"key": "alex", "name": "Alex"}])

    assert any("no channel target" in message for message in _messages(
        load_config(_write(tmp_path, payload)), "error"
    ))


def test_a_recipient_outside_the_people_table_is_only_informational(tmp_path) -> None:
    payload = dict(
        FULL,
        recipients=FULL["recipients"]
        + [
            {
                "key": "neighbour",
                "name": "Neighbour",
                "channel": {"channel": "sms", "to": "+15550000002"},
            }
        ],
    )

    findings = validate(load_config(_write(tmp_path, payload)))

    assert any(
        "neighbour" in f.message and f.severity == "info" for f in findings
    )


def test_mailbox_adapters_report_their_own_missing_settings(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CLAWFLIGHT_IMAP_PASSWORD", raising=False)
    imap = dict(FULL, mailbox={"adapter": "imap", "trusted_senders": ["air.example"]})
    mbox = dict(FULL, mailbox={"adapter": "mbox", "trusted_senders": ["air.example"]})
    untrusted = dict(FULL, mailbox={"adapter": "mbox", "path": "inbox.mbox"})

    imap_errors = _messages(load_config(_write(tmp_path, imap)), "error")
    mbox_errors = _messages(load_config(_write(tmp_path, mbox)), "error")
    untrusted_errors = _messages(load_config(_write(tmp_path, untrusted)), "error")

    assert any("host and username" in message for message in imap_errors)
    assert any("CLAWFLIGHT_IMAP_PASSWORD" in message for message in imap_errors)
    assert any("mbox mailbox needs a path" in message for message in mbox_errors)
    assert any("trusted_senders is empty" in message for message in untrusted_errors)


def test_enabling_push_requires_its_key_secret_and_url(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CLAWFLIGHT_RAPIDAPI_KEY", raising=False)
    monkeypatch.delenv("CLAWFLIGHT_WEBHOOK_SECRET", raising=False)
    payload = dict(FULL, push={"enabled": True})

    errors = _messages(load_config(_write(tmp_path, payload)), "error")

    assert any("CLAWFLIGHT_RAPIDAPI_KEY" in message for message in errors)
    assert any("CLAWFLIGHT_WEBHOOK_SECRET" in message for message in errors)
    assert any("webhook_url is empty" in message for message in errors)


def test_push_secrets_are_referenced_by_environment_name_only(tmp_path) -> None:
    # The config file must never be able to hold a credential value.
    payload = dict(
        FULL,
        push={
            "enabled": True,
            "rapidapi_key_env": "MY_KEY_VAR",
            "webhook_secret_env": "MY_SECRET_VAR",
            "webhook_url": "https://hooks.example.test/adb",
        },
    )

    config = load_config(_write(tmp_path, payload))

    assert config.push.rapidapi_key_env == "MY_KEY_VAR"
    assert config.push.webhook_secret_env == "MY_SECRET_VAR"
    assert not hasattr(config.push, "rapidapi_key")
    assert not hasattr(config.push, "webhook_secret")
