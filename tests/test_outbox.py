"""The durable delivery outbox: persistence, dedup, retry, backoff, fan-out."""
from __future__ import annotations

import json
import stat
from pathlib import Path

from clawflight.models import FlightEvent
from clawflight.notify import DEFAULT_RECIPIENT, DeliveryOutbox, FakePoster

from conftest import make_record


def _event(flight_id: str = "AA4912-2026-07-11", kind: str = "takeoff") -> FlightEvent:
    return FlightEvent(flight_id, kind, "AA4912 is airborne.", True, 1_000_000.0)


class _AlwaysFails:
    def post(self, text: str) -> bool:
        raise RuntimeError("channel unavailable")


class _Rejects:
    def post(self, text: str) -> bool:
        return False


def test_enqueue_persists_before_any_delivery_attempt(tmp_path: Path) -> None:
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))

    entry = outbox.enqueue(_event(), make_record())

    assert entry.state == "pending"
    assert entry.recipient == DEFAULT_RECIPIENT
    data = json.loads((tmp_path / "outbox.json").read_text())
    assert entry.delivery_id in data["deliveries"]
    assert stat.S_IMODE((tmp_path / "outbox.json").stat().st_mode) == 0o600


def test_enqueue_deduplicates_the_same_event(tmp_path: Path) -> None:
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))

    first = outbox.enqueue(_event(), make_record())
    second = outbox.enqueue(_event(), make_record())

    assert first.delivery_id == second.delivery_id
    assert len(outbox.entries()) == 1


def test_delivery_id_is_per_recipient(tmp_path: Path) -> None:
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))

    alex = outbox.enqueue(_event(), make_record(), recipient="alex")
    sam = outbox.enqueue(_event(), make_record(), recipient="sam")

    assert alex.delivery_id != sam.delivery_id
    assert len(alex.delivery_id) == len(sam.delivery_id) == 24


def test_enqueue_for_fans_out_and_deduplicates_recipients(tmp_path: Path) -> None:
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))

    entries = outbox.enqueue_for(_event(), make_record(), ["alex", "sam", "alex"])

    assert [entry.recipient for entry in entries] == ["alex", "sam"]
    assert len(outbox.entries()) == 2


def test_deliver_pending_calls_the_poster_and_acknowledges(tmp_path: Path) -> None:
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))
    outbox.enqueue(_event(), make_record())
    poster = FakePoster()

    result = outbox.deliver_pending(poster, 1_000_100.0)

    assert len(result["delivered"]) == 1
    assert result["failed"] == []
    assert "AA4912" in poster.calls[0]


def test_a_raising_or_rejecting_poster_marks_the_entry_failed(tmp_path: Path) -> None:
    raising = DeliveryOutbox(str(tmp_path / "raise.json"))
    raising.enqueue(_event(), make_record())
    rejecting = DeliveryOutbox(str(tmp_path / "reject.json"))
    rejecting.enqueue(_event(), make_record())

    raised = raising.deliver_pending(_AlwaysFails(), 1_000_100.0)
    rejected = rejecting.deliver_pending(_Rejects(), 1_000_100.0)

    assert len(raised["failed"]) == 1
    assert "channel unavailable" in (raising.entries()[0].last_error or "")
    assert len(rejected["failed"]) == 1
    assert rejecting.entries()[0].last_error == "poster did not acknowledge"


def test_pending_entries_survive_a_restart_and_resume(tmp_path: Path) -> None:
    path = str(tmp_path / "outbox.json")
    DeliveryOutbox(path).enqueue(_event(), make_record())

    resumed = DeliveryOutbox(path)
    poster = FakePoster()
    result = resumed.deliver_pending(poster, 2_000_000.0)

    assert len(resumed.pending()) == 0
    assert len(result["delivered"]) == 1
    assert len(poster.calls) == 1


def test_acknowledged_entries_are_never_redelivered(tmp_path: Path) -> None:
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))
    outbox.enqueue(_event(), make_record())
    poster = FakePoster()
    outbox.deliver_pending(poster, 1_000_100.0)

    again = outbox.deliver_pending(poster, 1_000_200.0)

    assert again["delivered"] == []
    assert len(poster.calls) == 1


def test_reenqueueing_a_delivered_event_does_not_requeue_it(tmp_path: Path) -> None:
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))
    record = make_record()
    event = _event(kind="delay")
    first = outbox.enqueue(event, record)
    outbox.deliver_pending(FakePoster(), 2_000.0)

    again = outbox.enqueue(event, record)

    assert again.delivery_id == first.delivery_id
    assert outbox.pending() == []


def test_events_for_different_flights_are_separate_deliveries(tmp_path: Path) -> None:
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))
    outbox.enqueue(_event("AA4912-2026-07-11", "takeoff"), make_record())
    outbox.enqueue(
        _event("AA1203-2026-07-11", "delay"),
        make_record(flight_id="AA1203-2026-07-11"),
    )
    poster = FakePoster()

    result = outbox.deliver_pending(poster, 1_000_100.0)

    assert len(result["delivered"]) == 2
    assert len(poster.calls) == 2


def test_backoff_skips_a_retry_until_its_window_elapses(tmp_path: Path) -> None:
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))
    outbox.enqueue(_event(), make_record())

    # First attempt fails at T=1000; backoff after one failure is 30s.
    assert len(outbox.deliver_pending(_AlwaysFails(), 1_000.0)["failed"]) == 1
    too_soon = outbox.deliver_pending(FakePoster(), 1_001.0)
    poster = FakePoster()
    after_window = outbox.deliver_pending(poster, 1_031.0)

    assert too_soon["delivered"] == [] and len(too_soon["skipped"]) == 1
    assert len(after_window["delivered"]) == 1
    assert len(poster.calls) == 1


def test_backoff_doubles_with_each_successive_failure(tmp_path: Path) -> None:
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))
    outbox.enqueue(_event(), make_record())

    outbox.deliver_pending(_AlwaysFails(), 0.0)  # backoff now 30s
    outbox.deliver_pending(_AlwaysFails(), 31.0)  # backoff now 60s

    assert len(outbox.deliver_pending(FakePoster(), 80.0)["skipped"]) == 1
    assert len(outbox.deliver_pending(FakePoster(), 92.0)["delivered"]) == 1


def test_recipients_have_independent_acknowledgement_and_backoff(tmp_path: Path) -> None:
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))
    entries = outbox.enqueue_for(_event(), make_record(), ["alex", "sam"])
    alex_poster = FakePoster()

    result = outbox.deliver_pending(
        FakePoster(),
        1_000.0,
        poster_for=lambda key: alex_poster if key == "alex" else _AlwaysFails(),
    )

    assert result == {
        "delivered": [entries[0].delivery_id],
        "failed": [entries[1].delivery_id],
        "skipped": [],
    }
    assert {entry.recipient: entry.state for entry in outbox.entries()} == {
        "alex": "acknowledged",
        "sam": "failed",
    }

    sam_poster = FakePoster()
    early = outbox.deliver_pending(FakePoster(), 1_001.0, poster_for=lambda key: sam_poster)
    retried = outbox.deliver_pending(FakePoster(), 1_030.0, poster_for=lambda key: sam_poster)

    assert early["skipped"] == [entries[1].delivery_id]
    assert retried["delivered"] == [entries[1].delivery_id]
    assert len(sam_poster.calls) == 1


def test_a_recipient_without_a_poster_is_marked_failed(tmp_path: Path) -> None:
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))
    entries = outbox.enqueue_for(_event(), make_record(), ["alex", "sam"])
    alex_poster = FakePoster()

    result = outbox.deliver_pending(
        FakePoster(),
        1_000.0,
        poster_for=lambda key: alex_poster if key == "alex" else None,
    )

    assert result["delivered"] == [entries[0].delivery_id]
    assert result["failed"] == [entries[1].delivery_id]
    by_recipient = {entry.recipient: entry for entry in outbox.entries()}
    assert by_recipient["sam"].last_error == "no poster for recipient"
    assert len(alex_poster.calls) == 1


def test_prune_removes_only_old_acknowledged_entries(tmp_path: Path) -> None:
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))
    record = make_record()
    old = outbox.enqueue(_event(kind="takeoff"), record)
    recent = outbox.enqueue(_event(kind="landing"), record)
    pending = outbox.enqueue(_event(kind="delay"), record)
    failed = outbox.enqueue(_event(kind="gate_change"), record)
    now = 2_000_000.0
    outbox.acknowledge(old.delivery_id, now - 15 * 86400)
    outbox.acknowledge(recent.delivery_id, now - 13 * 86400)
    outbox.fail(failed.delivery_id, "down", now - 30 * 86400)

    removed = outbox.prune(now)

    assert removed == [old.delivery_id]
    assert {entry.delivery_id: entry.state for entry in outbox.entries()} == {
        recent.delivery_id: "acknowledged",
        pending.delivery_id: "pending",
        failed.delivery_id: "failed",
    }


def test_acknowledge_and_fail_ignore_unknown_delivery_ids(tmp_path: Path) -> None:
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))

    outbox.acknowledge("nope", 1.0)
    outbox.fail("nope", "down", 1.0)

    assert outbox.entries() == []


def test_loader_skips_malformed_rows_and_defaults_optional_fields(tmp_path: Path) -> None:
    path = tmp_path / "outbox.json"
    path.write_text(
        json.dumps(
            {
                "deliveries": {
                    "valid": {
                        "delivery_id": "valid",
                        "event_key": "event",
                        "flight_id": "AA1-2026-07-11",
                        "text": "message",
                        "state": "pending",
                        "attempts": 0,
                        "created_at_epoch": 10.0,
                        "updated_at_epoch": 11.0,
                        "future_field": "ignored",
                    },
                    "missing-text": {
                        "delivery_id": "missing-text",
                        "event_key": "event-2",
                        "flight_id": "AA2-2026-07-11",
                        "state": "pending",
                    },
                    "not-a-mapping": "nope",
                }
            }
        )
    )

    entries = DeliveryOutbox(str(path)).entries()

    assert len(entries) == 1
    assert entries[0].delivery_id == "valid"
    assert entries[0].recipient == DEFAULT_RECIPIENT
    assert entries[0].last_error is None


def test_a_corrupt_outbox_file_starts_empty_rather_than_raising(tmp_path: Path) -> None:
    path = tmp_path / "outbox.json"

    path.write_text("{not json")
    assert DeliveryOutbox(str(path)).entries() == []

    path.write_bytes(b"\xff\xfe")
    assert DeliveryOutbox(str(path)).entries() == []

    path.write_text('{"deliveries": "not-a-dict"}')
    assert DeliveryOutbox(str(path)).entries() == []
