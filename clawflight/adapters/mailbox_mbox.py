"""File-drop mailbox adapter: read an mbox or a directory of .eml files.

This is the offline path. It needs no credentials and no network, which makes
it the adapter the test suite and ``clawflight sweep --dry-run`` use. It is also
a perfectly good production path for anyone whose mail client can export to a
watched folder.
"""
from __future__ import annotations

import mailbox
import os
from pathlib import Path
from typing import List

from .mailbox import MailboxMessage, message_from_bytes, message_from_email


class MboxAdapter:
    """Read messages from an mbox file or a directory of RFC 822 files."""

    def __init__(self, path: object) -> None:
        self.path = Path(os.fspath(path)).expanduser()

    def fetch(self, limit: int = 50) -> List[MailboxMessage]:
        if limit <= 0:
            return []
        if self.path.is_dir():
            return self._fetch_directory(limit)
        return self._fetch_mbox(limit)

    def _fetch_mbox(self, limit: int) -> List[MailboxMessage]:
        try:
            box = mailbox.mbox(str(self.path), create=False)
        except (OSError, mailbox.Error):
            return []
        messages: List[MailboxMessage] = []
        try:
            for index, parsed in enumerate(box):
                message = message_from_email(
                    parsed, "{}#{}".format(self.path.name, index)
                )
                if message is not None:
                    messages.append(message)
                if len(messages) >= limit:
                    break
        finally:
            box.close()
        return messages

    def _fetch_directory(self, limit: int) -> List[MailboxMessage]:
        messages: List[MailboxMessage] = []
        for entry in sorted(self.path.iterdir()):
            if len(messages) >= limit:
                break
            if not entry.is_file() or entry.suffix.lower() not in (".eml", ".msg", ".txt"):
                continue
            try:
                raw = entry.read_bytes()
            except OSError:
                continue
            message = message_from_bytes(raw, entry.name)
            if message is not None:
                messages.append(message)
        return messages
