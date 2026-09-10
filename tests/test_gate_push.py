"""End-to-end gate: a push webhook reaches an acknowledged delivery.

This is the hermetic descendant of the private project's push smoke harness.
It exercises receiver -> normalize -> monitor diff -> registry match -> outbox
-> poster acknowledgement in one pass, with no network, no key, and no server.
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timezone

from clawflight.aerodatabox import normalize_notification
from clawflight.models import FlightLeg
from clawflight.monitor import Monitor
from clawflight.notify import DeliveryOutbox, FakePoster, compose_post
from clawflight.parse import ParsedFlight
from clawflight.registry import Registry
from clawflight.webhook_receiver import make_handler


NOW = datetime(2026, 7, 11, 17, 0, tzinfo=timezone.utc).timestamp()

DELAY_NOTIFICATION = {
    "flight": {
        "number": "AA4912",
        "status": "Delayed",
        "departure": {
            "scheduledTime": {"local": "2026-07-11T12:51:00-06:00"},
            "revisedTime": {"local": "2026-07-11T13:51:00-06:00"},
            "terminal": "B",
            "gate": "B12",
        },
        "arrival": {
            "scheduledTime": {"local": "2026-07-11T16:10:00-05:00"},
            "revisedTime": {"local": "2026-07-11T17:10:00-05:00"},
        },
    }
}


class _FakeSocket:
    def __init__(self, request: bytes) -> None:
        self._input = io.BytesIO(request)
        self.output = io.BytesIO()

    def settimeout(self, timeout):
        del timeout

    def makefile(self, mode, buffering=-1):
        del buffering
        return self._input if "r" in mode else self.output

    def sendall(self, data):
        self.output.write(data)


def _deliver_webhook(handler_type, payload: dict) -> bytes:
    body = json.dumps(payload).encode()
    request = (
        b"POST /hook/gate-secret HTTP/1.1\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    )
    socket = _FakeSocket(request)
    handler_type(socket, ("127.0.0.1", 5000), None)
    return socket.output.getvalue()


def _seed_registry(path, people, conf_code="FAKE01") -> Registry:
    registry = Registry(str(path), people)
    registry.merge(
        [
            ParsedFlight(
                leg=FlightLeg(
                    carrier="AA",
                    number=4912,
                    date="2026-07-11",
                    origin="ASE",
                    dest="DFW",
                    sched_dep_iso="2026-07-11T12:51:00-06:00",
                    sched_arr_iso="2026-07-11T16:10:00-05:00",
                    conf_code=conf_code,
                    seat="10C",
                ),
                hints={
                    "source_id": "cal-0001",
                    "passenger_name": "ALEXANDRA MORGAN KESTREL",
                },
            )
        ]
    )
    return registry


def test_a_push_notification_reaches_an_acknowledged_delivery(tmp_path, people) -> None:
    registry = _seed_registry(tmp_path / "registry.json", people)
    monitor = Monitor(str(tmp_path / "monitor.json"))
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))
    poster = FakePoster()
    received = []

    response = _deliver_webhook(
        make_handler("gate-secret", received.append), DELAY_NOTIFICATION
    )

    assert b" 200 " in response
    assert [update.flight_number for update in received] == ["AA4912"]

    update = received[0]
    bookings = registry.matching_bookings(update)
    assert [booking.flight_id for booking in bookings] == ["AA4912-2026-07-11"]

    events = monitor.ingest_push_for_bookings(update, bookings, NOW)
    kinds = [event.kind for event in events]
    assert "delay" in kinds and "schedule_change" in kinds

    for event in events:
        outbox.enqueue(event, bookings[0], text=compose_post(event, bookings[0]))
    result = outbox.deliver_pending(poster, NOW + 60)

    assert result["failed"] == []
    assert len(result["delivered"]) == len(outbox.entries())
    assert all(entry.state == "acknowledged" for entry in outbox.entries())
    # The delivered text carries the traveler, the route and the new times.
    joined = "\n".join(poster.calls)
    assert "Traveler: Alex" in joined
    assert "Route: ASE -> DFW" in joined
    assert "New departure 13:51 (ASE local) / 15:51 ET" in joined


def test_replaying_the_same_notification_delivers_nothing_new(tmp_path, people) -> None:
    registry = _seed_registry(tmp_path / "registry.json", people)
    monitor = Monitor(str(tmp_path / "monitor.json"))
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))
    poster = FakePoster()
    update = normalize_notification(DELAY_NOTIFICATION)[0]
    bookings = registry.matching_bookings(update)

    for _ in range(3):
        for event in monitor.ingest_push_for_bookings(update, bookings, NOW):
            outbox.enqueue(event, bookings[0], text=compose_post(event, bookings[0]))
    outbox.deliver_pending(poster, NOW + 60)

    # A vendor that retries a webhook must never produce a second alert.
    delivered = len(poster.calls)
    outbox.deliver_pending(poster, NOW + 120)
    assert len(poster.calls) == delivered


def test_the_gate_survives_a_restart_between_receipt_and_delivery(
    tmp_path, people
) -> None:
    registry = _seed_registry(tmp_path / "registry.json", people)
    update = normalize_notification(DELAY_NOTIFICATION)[0]
    bookings = registry.matching_bookings(update)

    # Process one: receive and persist, then "crash" before delivering.
    first_monitor = Monitor(str(tmp_path / "monitor.json"))
    first_outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))
    for event in first_monitor.ingest_push_for_bookings(update, bookings, NOW):
        first_outbox.enqueue(event, bookings[0], text=compose_post(event, bookings[0]))
    pending_before = len(first_outbox.pending())
    assert pending_before > 0

    # Process two: a fresh start finds the work and completes it.
    resumed = DeliveryOutbox(str(tmp_path / "outbox.json"))
    poster = FakePoster()
    result = resumed.deliver_pending(poster, NOW + 3600)

    assert len(result["delivered"]) == pending_before
    assert len(poster.calls) == pending_before

    # And the monitor's diff state survived too: no duplicate events.
    resumed_monitor = Monitor(str(tmp_path / "monitor.json"))
    assert resumed_monitor.ingest_push_for_bookings(update, bookings, NOW + 3600) == []


def test_a_cancellation_reaches_the_registry_and_the_chat(tmp_path, people) -> None:
    registry = _seed_registry(tmp_path / "registry.json", people)
    monitor = Monitor(str(tmp_path / "monitor.json"))
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))
    poster = FakePoster()
    update = normalize_notification(
        {
            "flight": {
                "number": "AA4912",
                "status": "Cancelled",
                "departure": {"scheduledTime": {"local": "2026-07-11T12:51:00-06:00"}},
            }
        }
    )[0]

    bookings = registry.matching_bookings(update)
    events = monitor.ingest_push_for_bookings(update, bookings, NOW)
    for event in events:
        outbox.enqueue(event, bookings[0], text=compose_post(event, bookings[0]))
    outbox.deliver_pending(poster, NOW + 60)
    changed = registry.set_status_for_update(update, "cancelled")

    assert [event.kind for event in events] == ["cancelled"]
    assert changed == ["AA4912-2026-07-11"]
    assert registry.get("AA4912-2026-07-11").status == "cancelled"
    assert any("cancelled" in call.casefold() for call in poster.calls)


def test_push_state_stays_off_until_the_upgrade_is_configured() -> None:
    from clawflight.config import Config

    # The default configuration is polling-only: no key, no webhook, no tunnel.
    default = Config()

    assert default.push.enabled is False
    assert default.push.webhook_url == ""
    # Secrets are named, never held.
    assert default.push.rapidapi_key_env == "CLAWFLIGHT_RAPIDAPI_KEY"
    assert default.push.webhook_secret_env == "CLAWFLIGHT_WEBHOOK_SECRET"
