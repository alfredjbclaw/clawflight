"""End-to-end gate: one flight event fans out to several recipients.

The hermetic descendant of the private project's fan-out simulation. Each
recipient gets its own delivery id, its own channel, and its own independent
acknowledgement and retry state — one broken channel never blocks another.
"""
from __future__ import annotations

from datetime import datetime, timezone

from clawflight.adapters.channel_openclaw import poster_router
from clawflight.models import FlightEvent, FlightLeg
from clawflight.notify import DeliveryOutbox, compose_post
from clawflight.parse import ParsedFlight
from clawflight.recipients import FollowStore, RecipientConfig, resolve_recipients
from clawflight.registry import Registry


NOW = datetime(2026, 7, 12, 15, 0, tzinfo=timezone.utc).timestamp()

RECIPIENTS = RecipientConfig.from_entries(
    [
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
        {
            "key": "robin",
            "name": "Robin",
            "channel": {"channel": "signal", "to": "+15550000001"},
        },
    ]
)


class _RecordingRunner:
    """One runner for every channel; records argv instead of spawning."""

    def __init__(self, failing_channels=()) -> None:
        self.calls = []
        self.failing_channels = set(failing_channels)

    def __call__(self, argv, timeout):
        argv = list(argv)
        channel = argv[argv.index("--channel") + 1]
        self.calls.append((channel, argv[argv.index("--target") + 1], argv[-1]))
        return 1 if channel in self.failing_channels else 0


def _sam_flight(tmp_path, people) -> tuple:
    registry = Registry(str(tmp_path / "registry.json"), people)
    registry.merge(
        [
            ParsedFlight(
                leg=FlightLeg(
                    carrier="UA",
                    number=512,
                    date="2026-07-12",
                    origin="SFO",
                    dest="SEA",
                    sched_dep_iso="2026-07-12T08:00:00-07:00",
                    sched_arr_iso="2026-07-12T11:15:00-07:00",
                    conf_code="FAKE06",
                    seat="14A",
                ),
                hints={"source_id": "cal-0010", "passenger_name": "SAMUEL T KESTREL"},
            )
        ]
    )
    record = registry.get("UA512-2026-07-12")
    event = FlightEvent(
        record.flight_id, "delay", "UA512 is delayed by at least 45 minutes.", True, NOW
    )
    return record, event


def test_one_event_reaches_every_subscribed_recipient_once(tmp_path, people) -> None:
    record, event = _sam_flight(tmp_path, people)
    store = FollowStore(str(tmp_path / "follows.json"))
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))
    runner = _RecordingRunner()

    # Alex follows everything; Sam auto-follows their own flight; Robin does not.
    subscribed = [r.key for r in resolve_recipients(record, RECIPIENTS, store)]
    assert subscribed == ["alex", "sam"]

    entries = outbox.enqueue_for(
        event, record, subscribed, text=compose_post(event, record)
    )
    result = outbox.deliver_pending(
        None, NOW + 60, poster_for=poster_router(RECIPIENTS, runner=runner)
    )

    # Distinct delivery ids, distinct channels, one message each.
    assert len({entry.delivery_id for entry in entries}) == 2
    assert sorted(channel for channel, _target, _text in runner.calls) == [
        "telegram",
        "whatsapp",
    ]
    assert len(result["delivered"]) == 2
    assert result["failed"] == []
    assert all("UA512 is delayed" in text for _c, _t, text in runner.calls)


def test_an_explicit_follow_adds_a_recipient_and_a_mute_removes_one(
    tmp_path, people
) -> None:
    record, event = _sam_flight(tmp_path, people)
    store = FollowStore(str(tmp_path / "follows.json"))
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))
    runner = _RecordingRunner()

    store.follow("robin", record.flight_id)
    store.mute("alex", record.flight_id)

    subscribed = [r.key for r in resolve_recipients(record, RECIPIENTS, store)]
    outbox.enqueue_for(event, record, subscribed, text=compose_post(event, record))
    outbox.deliver_pending(
        None, NOW + 60, poster_for=poster_router(RECIPIENTS, runner=runner)
    )

    assert subscribed == ["sam", "robin"]
    assert sorted(channel for channel, _t, _x in runner.calls) == ["signal", "whatsapp"]


def test_one_failing_channel_never_blocks_another(tmp_path, people) -> None:
    record, event = _sam_flight(tmp_path, people)
    store = FollowStore(str(tmp_path / "follows.json"))
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))
    failing = _RecordingRunner(failing_channels={"whatsapp"})

    entries = outbox.enqueue_for(
        event, record, ["alex", "sam"], text=compose_post(event, record)
    )
    first = outbox.deliver_pending(
        None, NOW, poster_for=poster_router(RECIPIENTS, runner=failing)
    )

    assert first["delivered"] == [entries[0].delivery_id]
    assert first["failed"] == [entries[1].delivery_id]
    assert {entry.recipient: entry.state for entry in outbox.entries()} == {
        "alex": "acknowledged",
        "sam": "failed",
    }

    # The healthy recipient is not retried; the broken one waits out its backoff.
    healthy = _RecordingRunner()
    too_soon = outbox.deliver_pending(
        None, NOW + 1, poster_for=poster_router(RECIPIENTS, runner=healthy)
    )
    assert too_soon["skipped"] == [entries[1].delivery_id]
    assert healthy.calls == []

    recovered = _RecordingRunner()
    retried = outbox.deliver_pending(
        None, NOW + 31, poster_for=poster_router(RECIPIENTS, runner=recovered)
    )
    assert retried["delivered"] == [entries[1].delivery_id]
    assert [channel for channel, _t, _x in recovered.calls] == ["whatsapp"]


def test_a_recipient_with_no_channel_fails_visibly(tmp_path, people) -> None:
    record, event = _sam_flight(tmp_path, people)
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))
    misconfigured = RecipientConfig.from_entries(
        [
            {
                "key": "alex",
                "name": "Alex",
                "channel": {"channel": "telegram", "to": "-100"},
            },
            {"key": "sam", "name": "Sam"},
        ]
    )
    runner = _RecordingRunner()

    entries = outbox.enqueue_for(
        event, record, ["alex", "sam"], text=compose_post(event, record)
    )
    result = outbox.deliver_pending(
        None, NOW, poster_for=poster_router(misconfigured, runner=runner)
    )

    assert result["delivered"] == [entries[0].delivery_id]
    assert result["failed"] == [entries[1].delivery_id]
    # The failure is recorded, not silently dropped, so doctor can report it.
    by_recipient = {entry.recipient: entry for entry in outbox.entries()}
    assert by_recipient["sam"].last_error == "no poster for recipient"


def test_the_same_text_is_delivered_to_every_recipient(tmp_path, people) -> None:
    # Fan-out must not re-render per recipient: the audit trail is one message.
    record, event = _sam_flight(tmp_path, people)
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))
    runner = _RecordingRunner()
    text = compose_post(event, record)

    outbox.enqueue_for(event, record, ["alex", "sam", "robin"], text=text)
    outbox.deliver_pending(
        None, NOW, poster_for=poster_router(RECIPIENTS, runner=runner)
    )

    assert len(runner.calls) == 3
    assert {sent for _c, _t, sent in runner.calls} == {text}
    assert "Traveler: Sam" in text
    assert "Confirmation: FAKE06" in text
