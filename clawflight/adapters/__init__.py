"""Pluggable edges: where messages come from and where alerts go.

The engine core never talks to a mailbox or a chat network directly. It talks
to these two small interfaces:

* :class:`~clawflight.adapters.mailbox.MailboxAdapter` — yields
  :class:`~clawflight.adapters.mailbox.MailboxMessage` values.
* :class:`~clawflight.notify.Poster` — one ``post(text) -> bool`` method.

Anything satisfying them works, which is how personal integrations stay out of
this repository. See ``docs/adapters.md``.
"""

from .mailbox import MailboxAdapter, MailboxMessage, messages_to_candidates

__all__ = ["MailboxAdapter", "MailboxMessage", "messages_to_candidates"]
