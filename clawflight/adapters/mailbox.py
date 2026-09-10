"""The mailbox adapter interface and the shared message -> candidate pipeline.

Bring your own adapter: implement :meth:`MailboxAdapter.fetch` and clawflight
can ingest from anywhere — a corporate mail API, a calendar CLI, a webhook
drop directory. Two adapters ship: ``mailbox_mbox`` (offline file drop) and
``mailbox_imap`` (a forwarding address).
"""
from __future__ import annotations

import email
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Iterable, List, Optional, Tuple, Union

try:
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover - Python < 3.8
    from typing_extensions import Protocol, runtime_checkable  # type: ignore

from ..email_ingest import (
    EmailItineraryCandidate,
    TrustedSenderPolicy,
    ingest_email,
)
from ..parse import ParsedFlight, parse_airline_email


MAX_BODY_CHARS = 256 * 1024


@dataclass(frozen=True)
class MailboxMessage:
    """One message, reduced to exactly what ingestion needs.

    ``source_id`` must be stable for the life of the message (a Message-ID, an
    IMAP UIDVALIDITY/UID pair, a file path). It is the idempotency key.
    """

    source_id: str
    sender: str
    subject: str
    body: str
    received_at: datetime

    def bounded(self) -> "MailboxMessage":
        if len(self.body) <= MAX_BODY_CHARS:
            return self
        return MailboxMessage(
            source_id=self.source_id,
            sender=self.sender,
            subject=self.subject,
            body=self.body[:MAX_BODY_CHARS],
            received_at=self.received_at,
        )


@runtime_checkable
class MailboxAdapter(Protocol):
    """Anything that can hand clawflight a bounded list of messages."""

    def fetch(self, limit: int = 50) -> List[MailboxMessage]:
        """Return up to *limit* candidate messages, newest last."""
        ...


def messages_to_candidates(
    messages: Iterable[MailboxMessage],
    trusted_sources: Union[TrustedSenderPolicy, Iterable[str]],
    *,
    source_kind: str = "email",
) -> Tuple[List[EmailItineraryCandidate], List[str]]:
    """Run the labelled-field ingestion over messages from any adapter.

    Returns ``(candidates, skipped_source_ids)``. A message is skipped when its
    sender is untrusted or its body carries no complete labelled itinerary; both
    are normal, not errors.
    """
    policy = (
        trusted_sources
        if isinstance(trusted_sources, TrustedSenderPolicy)
        else TrustedSenderPolicy.from_sources(trusted_sources)
    )
    candidates: List[EmailItineraryCandidate] = []
    skipped: List[str] = []
    for message in messages:
        bounded = message.bounded()
        try:
            produced = ingest_email(
                source_id=bounded.source_id,
                sender=bounded.sender,
                observed_at=bounded.received_at,
                body=bounded.body,
                trusted_sources=policy,
                source_kind=source_kind,
            )
        except ValueError:
            skipped.append(bounded.source_id)
            continue
        if produced:
            candidates.extend(produced)
        else:
            skipped.append(bounded.source_id)
    return candidates, skipped


def messages_to_parsed_flights(
    messages: Iterable[MailboxMessage],
    trusted_sources: Union[TrustedSenderPolicy, Iterable[str]],
    default_year: int,
) -> List[ParsedFlight]:
    """Run the airline-layout parsers over trusted messages.

    This is the complement of :func:`messages_to_candidates`: it handles airline
    receipts that use a visual layout rather than labelled ``Key: value`` lines.
    """
    policy = (
        trusted_sources
        if isinstance(trusted_sources, TrustedSenderPolicy)
        else TrustedSenderPolicy.from_sources(trusted_sources)
    )
    parsed: List[ParsedFlight] = []
    for message in messages:
        bounded = message.bounded()
        if not policy.trusts(bounded.sender):
            continue
        for flight in parse_airline_email(bounded.body, default_year):
            hints = dict(flight.hints)
            hints.setdefault("source_id", "mail:{}".format(bounded.source_id))
            parsed.append(ParsedFlight(leg=flight.leg, hints=hints))
    return parsed


# --------------------------------------------------------------------------
# Shared RFC 822 helpers used by the mbox and IMAP adapters
# --------------------------------------------------------------------------


def message_from_bytes(raw: bytes, fallback_source_id: str) -> Optional[MailboxMessage]:
    try:
        parsed = email.message_from_bytes(raw)
    except Exception:  # noqa: BLE001 - malformed mail must never crash a sweep
        return None
    return message_from_email(parsed, fallback_source_id)


def message_from_email(
    parsed: Message, fallback_source_id: str
) -> Optional[MailboxMessage]:
    sender = parsed.get("From") or ""
    if not sender:
        return None
    source_id = _clean_message_id(parsed.get("Message-ID")) or fallback_source_id
    return MailboxMessage(
        source_id=source_id,
        sender=sender,
        subject=parsed.get("Subject") or "",
        body=extract_text_body(parsed),
        received_at=_received_at(parsed),
    )


def extract_text_body(parsed: Message) -> str:
    """Return the first ``text/plain`` part, falling back to a stripped HTML part."""
    if parsed.is_multipart():
        for part in parsed.walk():
            if part.get_content_type() == "text/plain":
                return _decoded(part)
        for part in parsed.walk():
            if part.get_content_type() == "text/html":
                return _strip_html(_decoded(part))
        return ""
    if parsed.get_content_type() == "text/html":
        return _strip_html(_decoded(parsed))
    return _decoded(parsed)


def _decoded(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        payload = part.get_payload()
        return payload if isinstance(payload, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")


def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = _TAG_RE.sub(" ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
    )
    lines = [_WS_RE.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _clean_message_id(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().strip("<>").strip()
    return cleaned or None


def _received_at(parsed: Message) -> datetime:
    raw = parsed.get("Date")
    if raw:
        try:
            value = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            value = None
        if value is not None:
            return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)
