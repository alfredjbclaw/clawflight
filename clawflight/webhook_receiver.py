"""Loopback-only webhook receiver for the optional push upgrade.

The server binds 127.0.0.1 only. Exposing it to the internet is the user's
choice of ingress (see ``docs/push-upgrade.md``); this module never opens a
public listener itself. The secret lives in the request path and is compared in
constant time.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Type

from .aerodatabox import normalize_notification
from .models import FlightUpdate
from .notify import FakePoster
from .recipients import resolve_recipients


MAX_BODY_BYTES = 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 30
SECRET_ENV = "CLAWFLIGHT_WEBHOOK_SECRET"
PATH_PREFIX_ENV = "CLAWFLIGHT_WEBHOOK_PATH_PREFIX"

_LOGGER = logging.getLogger(__name__)


def push_handler(
    config, monitor, outbox, registry, store, poster_for
) -> Callable[[FlightUpdate], None]:
    """Build the isolated update-to-delivery callback used by ``serve``.

    The HTTP layer owns request authentication and retry responses. This
    callback owns the same durable monitor/outbox path as a poll tick. It is
    independent of the server so tests can drive it without a socket.
    """

    def on_update(update: FlightUpdate) -> None:
        try:
            records = [
                record
                for record in registry.matching_bookings(update)
                if record.status not in ("done", "cancelled")
            ]
            if not records:
                return
            now_epoch = time.time()
            events = monitor.ingest_push(update, now_epoch)
            for record in records:
                recipients = [
                    recipient.key
                    for recipient in resolve_recipients(
                        record, config.recipients, store
                    )
                ]
                for event in events:
                    outbox.enqueue_for(event, record, recipients)
            outbox.deliver_pending(FakePoster(), now_epoch, poster_for=poster_for)
        except Exception:  # noqa: BLE001 - one update must not stop the receiver
            _LOGGER.exception("clawflight push update failed")

    return on_update


def make_server(
    port: int,
    secret: str,
    on_update: Callable[[FlightUpdate], None],
    path_prefix: str = "",
) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(
        ("127.0.0.1", port), make_handler(secret, on_update, path_prefix)
    )


def make_server_from_env(
    port: int, on_update: Callable[[FlightUpdate], None]
) -> ThreadingHTTPServer:
    secret = os.environ.get(SECRET_ENV)
    if not secret:
        raise ValueError("{} is required".format(SECRET_ENV))
    return make_server(port, secret, on_update, os.environ.get(PATH_PREFIX_ENV, ""))


def make_handler(
    secret: str,
    on_update: Callable[[FlightUpdate], None],
    path_prefix: str = "",
) -> Type[BaseHTTPRequestHandler]:
    if not secret:
        raise ValueError("webhook secret is required")
    # path_prefix (e.g. "/adb") supports prefix-preserving ingress routing; bare
    # paths stay accepted so local probes keep working.
    expected_path = path_prefix + "/hook/" + secret
    health_paths = {"/healthz", path_prefix + "/healthz"}

    class WebhookHandler(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT_SECONDS  # bound a stalled vendor connection

        def do_GET(self) -> None:
            if self.path in health_paths:
                self.send_response(200)
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            if not hmac.compare_digest(self.path, expected_path):
                self.send_response(404)
                self.end_headers()
                return
            content_length = self.headers.get("Content-Length")
            try:
                length = int(content_length or "0")
            except ValueError:
                self.send_response(400)
                self.end_headers()
                return
            if length < 0:
                self.send_response(400)
                self.end_headers()
                self.close_connection = True
                return
            if length > MAX_BODY_BYTES:
                self.send_response(413)
                self.end_headers()
                self.close_connection = True
                return
            try:
                decoded = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_response(400)
                self.end_headers()
                return
            if not isinstance(decoded, dict):
                self.send_response(400)
                self.end_headers()
                return
            callback_failed = False
            for update in normalize_notification(decoded):
                try:
                    on_update(update)
                except Exception:  # noqa: BLE001 - one bad update must not stop the rest
                    callback_failed = True
                    _LOGGER.exception("clawflight webhook callback failed")
            self.send_response(500 if callback_failed else 200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return WebhookHandler
