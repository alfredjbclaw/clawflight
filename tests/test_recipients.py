"""Recipient configuration and per-flight subscription routing."""
from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace

from clawflight.recipients import (
    FollowStore,
    RecipientConfig,
    resolve_recipients,
)


def _flight(flight_id: str, person_key: str) -> SimpleNamespace:
    return SimpleNamespace(
        flight_id=flight_id, person=SimpleNamespace(key=person_key)
    )


def test_the_default_configuration_is_empty() -> None:
    # A fresh install must notify nobody until the user names recipients.
    config = RecipientConfig()

    assert config.all_active() == []
    assert config.for_person("alex") == []
    assert len(config) == 0


def test_a_missing_or_malformed_file_stays_empty(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text("not json")
    (tmp_path / "wrong-shape.json").write_text('{"recipients": "nope"}')

    assert RecipientConfig("/nonexistent/path.json").all_active() == []
    assert RecipientConfig(str(tmp_path / "bad.json")).all_active() == []
    assert RecipientConfig(str(tmp_path / "wrong-shape.json")).all_active() == []


def test_recipients_load_with_a_channel_agnostic_target(tmp_path: Path) -> None:
    path = tmp_path / "recipients.json"
    path.write_text(
        json.dumps(
            {
                "recipients": [
                    {
                        "key": "alex",
                        "name": "Alex",
                        "follow_all": True,
                        "channel": {"channel": "telegram", "to": "-1009876543210:topic:42"},
                    },
                    {
                        "key": "sam",
                        "name": "Sam",
                        "channel": {"channel": "whatsapp", "to": "+15550000000"},
                    },
                ]
            }
        )
    )

    config = RecipientConfig(str(path))

    alex = config.for_person("alex")[0]
    sam = config.for_person("sam")[0]
    assert alex.channel_id == "telegram"
    assert alex.target == "-1009876543210:topic:42"
    assert alex.follow_all is True and alex.auto_follow_own is True
    assert sam.channel_id == "whatsapp"
    assert sam.follow_all is False
    assert alex.deliverable and sam.deliverable
    assert config.for_person("nobody") == []


def test_nested_defaults_are_accepted_as_well_as_flat_flags() -> None:
    config = RecipientConfig.from_entries(
        [
            {
                "key": "robin",
                "name": "Robin",
                "channel": {"channel": "signal", "to": "+15550000001"},
                "defaults": {"auto_follow_own": False, "follow_all": True},
            }
        ]
    )

    robin = config.for_person("robin")[0]
    assert robin.auto_follow_own is False
    assert robin.follow_all is True


def test_a_recipient_without_a_channel_is_not_deliverable() -> None:
    config = RecipientConfig.from_entries(
        [
            {"key": "alex", "name": "Alex"},
            {"key": "sam", "name": "Sam", "channel": {"channel": "telegram"}},
        ]
    )

    assert config.get("alex").deliverable is False
    assert config.get("sam").deliverable is False


def test_invalid_recipient_rows_are_skipped() -> None:
    config = RecipientConfig.from_entries(
        [{"key": "alex"}, {"name": "no key"}, {"key": "  "}, "nope", None]
    )

    assert config.keys() == []


def test_inactive_recipients_are_never_returned() -> None:
    config = RecipientConfig.from_entries(
        [{"key": "alex", "name": "Alex", "active": False}]
    )

    assert config.for_person("alex") == []
    assert config.all_active() == []
    assert config.get("alex") is not None


def test_follow_store_round_trips_and_tolerates_corruption(tmp_path: Path) -> None:
    path = tmp_path / "follows.json"
    store = FollowStore(str(path))

    assert store.followed("sam") == []
    assert store.muted("sam") == []

    store.follow("sam", "AA1-2026-07-27")
    store.follow("sam", "AA1-2026-07-27")  # idempotent
    store.mute("sam", "AA2-2026-07-28")
    store.mute("sam", "AA2-2026-07-28")
    assert FollowStore(str(path)).followed("sam") == ["AA1-2026-07-27"]
    assert FollowStore(str(path)).muted("sam") == ["AA2-2026-07-28"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    store.unfollow("sam", "AA1-2026-07-27")
    store.unfollow("sam", "AA1-2026-07-27")
    store.unmute("sam", "AA2-2026-07-28")
    store.unmute("sam", "AA2-2026-07-28")
    assert store.followed("sam") == [] and store.muted("sam") == []

    path.write_text("{broken")
    assert store.followed("sam") == []
    path.write_bytes(b"\xff\xfe")
    assert store.muted("sam") == []

    # A corrupt file is replaced, not appended to.
    store.follow("sam", "AA3-2026-07-29")
    assert store.followed("sam") == ["AA3-2026-07-29"]


def test_follow_store_drops_non_string_entries(tmp_path: Path) -> None:
    path = tmp_path / "follows.json"
    path.write_text(
        json.dumps(
            {
                "recipients": {
                    "sam": {"followed": ["AA1-2026-07-27", 17, None], "muted": "nope"},
                    17: {"followed": []},
                }
            }
        )
    )

    store = FollowStore(str(path))

    assert store.followed("sam") == ["AA1-2026-07-27"]
    assert store.muted("sam") == []


def test_subscription_matrix_covers_follow_all_own_explicit_and_mute(
    tmp_path: Path,
) -> None:
    config = RecipientConfig.from_entries(
        [
            {
                "key": "alex",
                "name": "Alex",
                "follow_all": True,
                "channel": {"channel": "telegram", "to": "-100:topic:1"},
            },
            {
                "key": "sam",
                "name": "Sam",
                "channel": {"channel": "whatsapp", "to": "+15550000000"},
            },
            {
                "key": "inactive",
                "name": "Inactive",
                "active": False,
                "follow_all": True,
                "channel": {"channel": "telegram", "to": "-100:topic:2"},
            },
        ]
    )
    store = FollowStore(str(tmp_path / "follows.json"))
    sam_flight = _flight("DL10-2026-07-27", "sam")
    alex_flight = _flight("UA20-2026-07-28", "alex")

    # follow_all sees everything; auto_follow_own sees only one's own flights.
    assert [r.key for r in resolve_recipients(sam_flight, config, store)] == ["alex", "sam"]
    assert [r.key for r in resolve_recipients(alex_flight, config, store)] == ["alex"]

    # An explicit follow adds a flight that is not one's own.
    store.follow("sam", alex_flight.flight_id)
    assert [r.key for r in resolve_recipients(alex_flight, config, store)] == ["alex", "sam"]

    # A mute wins over follow_all.
    store.mute("alex", alex_flight.flight_id)
    assert [r.key for r in resolve_recipients(alex_flight, config, store)] == ["sam"]
