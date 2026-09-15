"""The loopback push receiver: authentication, bounds, and retry semantics."""
import io
import json
from types import SimpleNamespace

import pytest

from clawflight.webhook_receiver import (
    MAX_BODY_BYTES,
    REQUEST_TIMEOUT_SECONDS,
    SECRET_ENV,
    make_handler,
    make_server_from_env,
    push_handler,
)
from clawflight.models import FlightEvent, FlightLeg, FlightRecord, FlightUpdate, PersonRef
from clawflight.recipients import FollowStore, RecipientConfig


class _FakeSocket:
    def __init__(self, request: bytes) -> None:
        self._input = io.BytesIO(request)
        self.output = io.BytesIO()

    def settimeout(self, timeout: float) -> None:
        del timeout

    def makefile(self, mode: str, buffering: int = -1):
        del buffering
        return self._input if "r" in mode else self.output

    def sendall(self, data: bytes) -> None:
        self.output.write(data)


def _request(handler_type, request: bytes) -> bytes:
    socket = _FakeSocket(request)
    handler_type(socket, ("127.0.0.1", 5000), None)
    return socket.output.getvalue()


def _post(path: str, payload: dict) -> bytes:
    body = json.dumps(payload).encode()
    return (
        "POST {} HTTP/1.1\r\nContent-Length: {}\r\n\r\n".format(path, len(body)).encode()
        + body
    )


def test_a_valid_post_is_normalized_and_acknowledged() -> None:
    received = []

    response = _request(
        make_handler("expected-secret", received.append),
        _post("/hook/expected-secret", {"flight": {"number": "AA4912", "status": "Scheduled"}}),
    )

    assert b" 200 " in response
    assert [update.flight_number for update in received] == ["AA4912"]


def test_a_failing_callback_asks_the_vendor_to_retry() -> None:
    received = []

    def on_update(update):
        received.append(update)
        raise RuntimeError("subscriber is temporarily unavailable")

    response = _request(
        make_handler("expected-secret", on_update),
        _post("/hook/expected-secret", {"flight": {"number": "AA4912", "status": "Scheduled"}}),
    )

    assert b" 500 " in response
    assert [update.flight_number for update in received] == ["AA4912"]


def test_every_update_is_attempted_before_reporting_failure() -> None:
    received = []

    def on_update(update):
        received.append(update.flight_number)
        if update.flight_number == "AA4912":
            raise RuntimeError("first update failed")

    response = _request(
        make_handler("expected-secret", on_update),
        _post("/hook/expected-secret", {"flights": [{"number": "AA4912"}, {"number": "DL767"}]}),
    )

    assert b" 500 " in response
    assert received == ["AA4912", "DL767"]


def test_wrong_secret_malformed_and_non_object_bodies_are_rejected() -> None:
    handler = make_handler("expected-secret", lambda update: None)

    wrong_path = _request(handler, _post("/hook/wrong", {}))
    malformed = _request(
        handler, b"POST /hook/expected-secret HTTP/1.1\r\nContent-Length: 1\r\n\r\n{"
    )
    non_object = _request(handler, _post("/hook/expected-secret", []))

    assert b" 404 " in wrong_path
    assert b" 400 " in malformed
    assert b" 400 " in non_object


def test_body_length_is_bounded_and_health_is_exposed() -> None:
    handler = make_handler("expected-secret", lambda update: None)

    health = _request(handler, b"GET /healthz HTTP/1.1\r\nHost: local\r\n\r\n")
    unknown_get = _request(handler, b"GET /secret HTTP/1.1\r\nHost: local\r\n\r\n")
    oversized = _request(
        handler,
        "POST /hook/expected-secret HTTP/1.1\r\nContent-Length: {}\r\n\r\n".format(
            MAX_BODY_BYTES + 1
        ).encode(),
    )
    negative = _request(
        handler, b"POST /hook/expected-secret HTTP/1.1\r\nContent-Length: -1\r\n\r\n{}"
    )
    non_numeric = _request(
        handler, b"POST /hook/expected-secret HTTP/1.1\r\nContent-Length: abc\r\n\r\n{}"
    )

    assert b" 200 " in health
    assert b" 404 " in unknown_get
    assert b" 413 " in oversized
    assert b" 400 " in negative
    assert b" 400 " in non_numeric


def test_a_stalled_vendor_connection_cannot_pin_a_thread() -> None:
    handler = make_handler("expected-secret", lambda update: None)

    assert handler.timeout == REQUEST_TIMEOUT_SECONDS


def test_an_ingress_path_prefix_is_honoured() -> None:
    # Given: a handler behind prefix-preserving ingress routing.
    handler = make_handler("expected-secret", lambda update: None, path_prefix="/adb")
    payload = {"flight": {"number": "DL767", "status": "Scheduled"}}

    prefixed = _request(handler, _post("/adb/hook/expected-secret", payload))
    bare = _request(handler, _post("/hook/expected-secret", payload))
    prefixed_health = _request(handler, b"GET /adb/healthz HTTP/1.1\r\nHost: local\r\n\r\n")
    bare_health = _request(handler, b"GET /healthz HTTP/1.1\r\nHost: local\r\n\r\n")

    assert b" 200 " in prefixed
    assert b" 404 " in bare
    assert b" 200 " in prefixed_health
    assert b" 200 " in bare_health


def test_a_receiver_can_never_be_built_without_a_secret(monkeypatch) -> None:
    monkeypatch.delenv(SECRET_ENV, raising=False)

    with pytest.raises(ValueError):
        make_handler("", lambda update: None)
    with pytest.raises(ValueError):
        make_server_from_env(0, lambda update: None)


def _synthetic_update() -> FlightUpdate:
    return FlightUpdate(
        "AA4912", "Scheduled", "2026-07-11T12:51:00-06:00", None,
        None, None, None, None, None, None, service_date="2026-07-11",
    )


def _synthetic_record() -> FlightRecord:
    return FlightRecord(
        "AA4912-2026-07-11",
        FlightLeg("AA", 4912, "2026-07-11", "DEN", "ORD", None, None, "FAKE01", None),
        PersonRef("alex", "Alex Kestrel"), (), None, "scheduled", (),
    )


class _RecordingMonitor:
    def __init__(self):
        self.updates = []

    def ingest_push(self, update, now_epoch):
        self.updates.append((update, now_epoch))
        return [FlightEvent("AA4912-2026-07-11", "gate_change", "AA4912 now departs from B4.", True, now_epoch)]


class _RecordingOutbox:
    def __init__(self, fail_delivery=False):
        self.enqueued = []
        self.drains = 0
        self.fail_delivery = fail_delivery

    def enqueue_for(self, event, record, recipients):
        self.enqueued.append((event, record, list(recipients)))

    def deliver_pending(self, poster, now_epoch, *, poster_for=None):
        self.drains += 1
        if self.fail_delivery:
            raise RuntimeError("synthetic delivery failure")


def _handler_parts(tmp_path, *, known=True, fail_delivery=False):
    record = _synthetic_record()
    registry = SimpleNamespace(
        matching_bookings=lambda update: [record] if known else []
    )
    recipients = RecipientConfig.from_entries([
        {"key": "alex", "name": "Alex", "follow_all": True,
         "channel": {"channel": "telegram", "to": "synthetic-room"}}
    ])
    config = SimpleNamespace(recipients=recipients)
    monitor = _RecordingMonitor()
    outbox = _RecordingOutbox(fail_delivery)
    handler = push_handler(
        config, monitor, outbox, registry,
        FollowStore(str(tmp_path / "follows.json")), lambda key: object(),
    )
    return handler, monitor, outbox


def test_push_handler_ingests_routes_enqueues_and_drains_without_a_server(tmp_path) -> None:
    handler, monitor, outbox = _handler_parts(tmp_path)

    handler(_synthetic_update())

    assert [item[0] for item in monitor.updates] == [_synthetic_update()]
    assert len(outbox.enqueued) == 1
    assert outbox.enqueued[0][2] == ["alex"]
    assert outbox.drains == 1


def test_push_handler_ignores_an_unknown_flight(tmp_path) -> None:
    handler, monitor, outbox = _handler_parts(tmp_path, known=False)

    handler(_synthetic_update())

    assert monitor.updates == []
    assert outbox.enqueued == []
    assert outbox.drains == 0


@pytest.mark.parametrize("status", ["done", "cancelled"])
def test_push_handler_ignores_a_terminal_flight(tmp_path, status) -> None:
    record = _synthetic_record()
    record = FlightRecord(
        record.flight_id, record.leg, record.person, record.sources,
        record.backup_group, status, record.notes,
    )
    registry = SimpleNamespace(matching_bookings=lambda update: [record])
    recipients = RecipientConfig.from_entries([
        {"key": "alex", "name": "Alex", "follow_all": True,
         "channel": {"channel": "telegram", "to": "synthetic-room"}}
    ])
    monitor = _RecordingMonitor()
    outbox = _RecordingOutbox()
    handler = push_handler(
        SimpleNamespace(recipients=recipients), monitor, outbox, registry,
        FollowStore(str(tmp_path / "follows.json")), lambda key: object(),
    )

    handler(_synthetic_update())

    assert monitor.updates == []
    assert outbox.enqueued == []
    assert outbox.drains == 0


def test_push_handler_isolates_delivery_failure_and_accepts_the_next_update(tmp_path) -> None:
    handler, monitor, outbox = _handler_parts(tmp_path, fail_delivery=True)

    handler(_synthetic_update())
    handler(_synthetic_update())

    assert len(monitor.updates) == 2
    assert outbox.drains == 2
