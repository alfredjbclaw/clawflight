"""Forwarding-address mailbox adapter over stdlib ``imaplib``.

The intended setup is a dedicated address (or a plus-address) that the family
forwards airline confirmations to. clawflight polls it read-only, extracts the
itinerary shape, and never stores the message body.

The IMAP connection is injected (``connection_factory``), so the whole adapter
is testable against a fake without a server, a credential, or a socket.
"""
from __future__ import annotations

import os
from typing import Callable, List, Optional, Sequence

from .mailbox import MailboxMessage, message_from_bytes


DEFAULT_PORT = 993
DEFAULT_FOLDER = "INBOX"
DEFAULT_SEARCH = "UNSEEN"


class ImapConfigError(RuntimeError):
    """Raised when IMAP settings are incomplete. Never contains the password."""


def _default_connection_factory(host: str, port: int, ssl: bool):  # pragma: no cover
    import imaplib

    if ssl:
        return imaplib.IMAP4_SSL(host, port)
    return imaplib.IMAP4(host, port)


class ImapAdapter:
    """Poll a mailbox over IMAP and return bounded messages.

    The password is read from ``password_env`` at call time and passed straight
    to ``login``. It is never stored on the instance, logged, or included in an
    exception message.
    """

    def __init__(
        self,
        host: str,
        username: str,
        *,
        password_env: str = "CLAWFLIGHT_IMAP_PASSWORD",
        port: int = DEFAULT_PORT,
        ssl: bool = True,
        folder: str = DEFAULT_FOLDER,
        search: str = DEFAULT_SEARCH,
        mark_seen: bool = False,
        connection_factory: Optional[Callable[[str, int, bool], object]] = None,
        environ: Optional[dict] = None,
    ) -> None:
        self.host = host
        self.username = username
        self.password_env = password_env
        self.port = port
        self.ssl = ssl
        self.folder = folder
        self.search = search
        self.mark_seen = mark_seen
        self._connection_factory = connection_factory or _default_connection_factory
        self._environ = environ if environ is not None else os.environ

    def fetch(self, limit: int = 50) -> List[MailboxMessage]:
        if limit <= 0:
            return []
        if not self.host or not self.username:
            raise ImapConfigError("imap adapter needs both host and username")
        password = self._environ.get(self.password_env)
        if not password:
            raise ImapConfigError(
                "environment variable {} is not set".format(self.password_env)
            )

        connection = self._connection_factory(self.host, self.port, self.ssl)
        try:
            connection.login(self.username, password)
            connection.select(self.folder, readonly=not self.mark_seen)
            status, payload = connection.search(None, self.search)
            if status != "OK":
                return []
            identifiers = _identifiers(payload)[-limit:]
            messages: List[MailboxMessage] = []
            for identifier in identifiers:
                status, data = connection.fetch(identifier, "(RFC822)")
                if status != "OK":
                    continue
                raw = _first_body(data)
                if raw is None:
                    continue
                message = message_from_bytes(
                    raw, "imap:{}:{}".format(self.folder, identifier.decode("ascii", "replace"))
                )
                if message is not None:
                    messages.append(message)
            return messages
        finally:
            _quietly(connection, "close")
            _quietly(connection, "logout")


def _identifiers(payload: Sequence[object]) -> List[bytes]:
    if not payload:
        return []
    first = payload[0]
    if isinstance(first, bytes):
        return [item for item in first.split() if item]
    if isinstance(first, str):
        return [item.encode("ascii", "replace") for item in first.split() if item]
    return []


def _first_body(data: Sequence[object]) -> Optional[bytes]:
    for item in data or ():
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
        if isinstance(item, bytes) and item.strip() not in (b")", b""):
            return item
    return None


def _quietly(connection: object, method_name: str) -> None:
    method = getattr(connection, method_name, None)
    if method is None:
        return
    try:
        method()
    except Exception:  # noqa: BLE001 - teardown must not mask a real error
        pass
