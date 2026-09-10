"""The loopback push receiver: authentication, bounds, and retry semantics."""
import io
import json

import pytest

from clawflight.webhook_receiver import (
    MAX_BODY_BYTES,
    REQUEST_TIMEOUT_SECONDS,
    SECRET_ENV,
    make_handler,
    make_server_from_env,
)


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
