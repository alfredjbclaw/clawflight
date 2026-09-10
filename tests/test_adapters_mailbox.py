"""The mailbox adapters and the shared message -> candidate pipeline."""
from __future__ import annotations

import email.utils
from datetime import datetime, timezone

import pytest

from clawflight.adapters.mailbox import (
    MailboxAdapter,
    MailboxMessage,
    extract_text_body,
    message_from_bytes,
    messages_to_candidates,
    messages_to_parsed_flights,
)
from clawflight.adapters.mailbox_imap import ImapAdapter, ImapConfigError
from clawflight.adapters.mailbox_mbox import MboxAdapter


TRUSTED = {"air.example"}


def _message(**overrides) -> MailboxMessage:
    defaults = dict(
        source_id="trip-1001@air.example",
        sender="Air Example <confirmations@air.example>",
        subject="Your itinerary is confirmed",
        body=(
            "Confirmation Code: FAKE20\n"
            "Traveler: ALEXANDRA MORGAN KESTREL\n"
            "\nFlight: DL 248\nDate: 2026-08-19\nFrom: JFK\nTo: LAX\n"
        ),
        received_at=datetime(2026, 7, 11, 9, 14, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return MailboxMessage(**defaults)


# -- the mbox adapter -------------------------------------------------------


def test_the_mbox_adapter_reads_every_message_in_the_fixture(fixtures) -> None:
    messages = MboxAdapter(fixtures / "inbox.mbox").fetch()

    assert [message.source_id for message in messages] == [
        "trip-1001@air.example",
        "trip-1002@air.example",
        "promo-77@marketing.example",
        "spoof-9@air.example.evil.test",
    ]
    assert messages[0].sender.endswith("<confirmations@air.example>")
    assert "Confirmation Code: FAKE20" in messages[0].body
    assert messages[0].received_at.tzinfo is not None


def test_the_mbox_adapter_honours_its_limit_and_missing_files(fixtures, tmp_path) -> None:
    assert len(MboxAdapter(fixtures / "inbox.mbox").fetch(limit=2)) == 2
    assert MboxAdapter(fixtures / "inbox.mbox").fetch(limit=0) == []
    assert MboxAdapter(tmp_path / "nope.mbox").fetch() == []


def test_the_mbox_adapter_can_read_a_drop_directory(tmp_path) -> None:
    # Given: a watched folder rather than an mbox file.
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "a.eml").write_text(
        "Message-ID: <a@air.example>\nFrom: Air <confirmations@air.example>\n"
        "Subject: One\nDate: Sat, 11 Jul 2026 09:14:02 +0000\n\nbody one\n"
    )
    (drop / "b.eml").write_text(
        "Message-ID: <b@air.example>\nFrom: Air <confirmations@air.example>\n"
        "Subject: Two\n\nbody two\n"
    )
    (drop / "ignored.pdf").write_bytes(b"not mail")

    messages = MboxAdapter(drop).fetch()

    assert [message.source_id for message in messages] == ["a@air.example", "b@air.example"]
    # A message with no Date header still gets a usable timestamp.
    assert messages[1].received_at.tzinfo is not None


def test_a_message_without_a_sender_is_skipped() -> None:
    assert message_from_bytes(b"Subject: no sender\n\nbody", "fallback") is None


def test_a_message_without_a_message_id_uses_the_supplied_fallback() -> None:
    message = message_from_bytes(
        b"From: Air <confirmations@air.example>\n\nbody", "fallback-id"
    )

    assert message is not None and message.source_id == "fallback-id"


def test_html_only_bodies_are_reduced_to_text() -> None:
    raw = (
        b"From: Air <confirmations@air.example>\n"
        b"Content-Type: text/html; charset=utf-8\n\n"
        b"<html><style>p{color:red}</style><body><p>Flight: DL 248</p>"
        b"<br><div>From: JFK</div></body></html>"
    )

    message = message_from_bytes(raw, "fallback")

    assert message is not None
    assert "Flight: DL 248" in message.body
    assert "<p>" not in message.body
    assert "color:red" not in message.body


def test_a_multipart_message_prefers_its_plain_text_part() -> None:
    import email.message

    outer = email.message.EmailMessage()
    outer["From"] = "Air <confirmations@air.example>"
    outer.set_content("plain part")
    outer.add_alternative("<p>html part</p>", subtype="html")

    assert "plain part" in extract_text_body(outer)


def test_bodies_are_bounded_before_ingestion() -> None:
    huge = _message(body="x" * (300 * 1024))

    assert len(huge.bounded().body) == 256 * 1024
    assert huge.bounded().source_id == huge.source_id
    # A message already within bounds is returned unchanged.
    small = _message()
    assert small.bounded() is small


# -- the shared pipeline ----------------------------------------------------


def test_only_trusted_labelled_messages_become_candidates(fixtures) -> None:
    messages = MboxAdapter(fixtures / "inbox.mbox").fetch()

    candidates, skipped = messages_to_candidates(messages, TRUSTED)

    # The two trusted bookings produce three legs; marketing and the
    # lookalike-subdomain spoof are skipped.
    assert [(item.carrier, item.number) for item in candidates] == [
        ("DL", 248),
        ("UA", 410),
        ("UA", 882),
    ]
    assert {item.confirmation_code for item in candidates} == {"FAKE20", "FAKE21"}
    assert skipped == ["promo-77@marketing.example", "spoof-9@air.example.evil.test"]


def test_candidate_provenance_points_back_at_the_message(fixtures) -> None:
    messages = MboxAdapter(fixtures / "inbox.mbox").fetch()

    candidates, _ = messages_to_candidates(messages, TRUSTED)

    assert candidates[0].evidence.source_id == "trip-1001@air.example"
    assert candidates[0].evidence.sender_identity == "confirmations@air.example"
    assert candidates[0].evidence.source_kind == "email"


def test_a_message_with_invalid_provenance_is_skipped_not_raised() -> None:
    candidates, skipped = messages_to_candidates([_message(source_id="  ")], TRUSTED)

    assert candidates == []
    assert skipped == ["  "]


def test_airline_layout_messages_take_the_parser_path(fixtures) -> None:
    # Given: a receipt in the visual airline layout rather than labelled fields.
    body = (fixtures / "email_delta_receipt.txt").read_text(encoding="utf-8")
    trusted = _message(source_id="receipt-1", body=body)
    untrusted = _message(
        source_id="receipt-2", sender="spoof@air.example.evil.test", body=body
    )

    parsed = messages_to_parsed_flights([trusted, untrusted], TRUSTED, 2026)

    assert [(item.leg.carrier, item.leg.number) for item in parsed] == [
        ("DL", 667),
        ("DL", 1226),
    ]
    assert all(item.hints["source_id"] == "mail:receipt-1" for item in parsed)


def test_an_explicit_policy_object_is_accepted_by_both_pipelines(fixtures) -> None:
    from clawflight.email_ingest import TrustedSenderPolicy

    policy = TrustedSenderPolicy(base_domains={"air.example"})
    messages = MboxAdapter(fixtures / "inbox.mbox").fetch()

    candidates, _ = messages_to_candidates(messages, policy)
    parsed = messages_to_parsed_flights(messages, policy, 2026)

    assert len(candidates) == 3
    assert parsed == []


# -- the IMAP adapter -------------------------------------------------------


class _FakeIMAP:
    """A stdlib-shaped IMAP double. No socket, no credential, no server."""

    def __init__(self, messages, *, search_status="OK") -> None:
        self.messages = messages
        self.search_status = search_status
        self.calls = []
        self.closed = False
        self.logged_out = False

    def login(self, username, password):
        self.calls.append(("login", username, password))
        return ("OK", [b"logged in"])

    def select(self, folder, readonly=True):
        self.calls.append(("select", folder, readonly))
        return ("OK", [b"1"])

    def search(self, charset, criteria):
        self.calls.append(("search", charset, criteria))
        identifiers = b" ".join(str(index + 1).encode() for index in range(len(self.messages)))
        return (self.search_status, [identifiers])

    def fetch(self, identifier, parts):
        self.calls.append(("fetch", identifier, parts))
        index = int(identifier) - 1
        return ("OK", [(b"1 (RFC822 {})", self.messages[index]), b")"])

    def close(self):
        self.closed = True

    def logout(self):
        self.logged_out = True


RAW = (
    b"Message-ID: <trip-2001@air.example>\n"
    b"From: Air Example <confirmations@air.example>\n"
    b"Subject: Your itinerary is confirmed\n"
    b"Date: Sat, 11 Jul 2026 09:14:02 +0000\n\n"
    b"Confirmation Code: FAKE20\nTraveler: ALEXANDRA MORGAN KESTREL\n"
    b"\nFlight: DL 248\nDate: 2026-08-19\nFrom: JFK\nTo: LAX\n"
)


def _adapter(server, **overrides):
    settings = dict(
        host="imap.example.test",
        username="family-flights@example.com",
        connection_factory=lambda host, port, ssl: server,
        environ={"CLAWFLIGHT_IMAP_PASSWORD": "not-a-real-password"},
    )
    settings.update(overrides)
    username = settings.pop("username")
    host = settings.pop("host")
    return ImapAdapter(host, username, **settings)


def test_the_imap_adapter_fetches_and_parses_without_a_server() -> None:
    server = _FakeIMAP([RAW])

    messages = _adapter(server).fetch()

    assert [message.source_id for message in messages] == ["trip-2001@air.example"]
    assert "Flight: DL 248" in messages[0].body
    assert ("select", "INBOX", True) in server.calls
    assert ("search", None, "UNSEEN") in server.calls
    assert server.closed and server.logged_out


def test_the_imap_adapter_opens_the_mailbox_read_only_by_default() -> None:
    read_only = _FakeIMAP([RAW])
    writable = _FakeIMAP([RAW])

    _adapter(read_only).fetch()
    _adapter(writable, mark_seen=True).fetch()

    assert ("select", "INBOX", True) in read_only.calls
    assert ("select", "INBOX", False) in writable.calls


def test_the_imap_adapter_applies_its_limit_to_the_newest_messages() -> None:
    server = _FakeIMAP([RAW, RAW.replace(b"trip-2001", b"trip-2002")])

    messages = _adapter(server).fetch(limit=1)

    assert [message.source_id for message in messages] == ["trip-2002@air.example"]
    assert _adapter(_FakeIMAP([RAW])).fetch(limit=0) == []


def test_a_failed_search_returns_nothing_and_still_tears_down() -> None:
    server = _FakeIMAP([RAW], search_status="NO")

    assert _adapter(server).fetch() == []
    assert server.closed and server.logged_out


def test_incomplete_imap_settings_raise_without_naming_the_password() -> None:
    with pytest.raises(ImapConfigError) as missing_host:
        _adapter(_FakeIMAP([RAW]), host="").fetch()
    with pytest.raises(ImapConfigError) as missing_password:
        _adapter(_FakeIMAP([RAW]), environ={}).fetch()

    assert "host and username" in str(missing_host.value)
    assert "CLAWFLIGHT_IMAP_PASSWORD" in str(missing_password.value)
    assert "not-a-real-password" not in str(missing_password.value)


def test_the_password_is_read_at_call_time_and_never_stored() -> None:
    import os

    server = _FakeIMAP([RAW])
    adapter = _adapter(server, environ={"CLAWFLIGHT_IMAP_PASSWORD": "not-a-real-password"})

    adapter.fetch()

    # The adapter passes the password straight through to login and keeps only
    # the *name* of the variable it came from.
    assert ("login", "family-flights@example.com", "not-a-real-password") in server.calls
    own_state = {
        key: value for key, value in vars(adapter).items() if key != "_environ"
    }
    assert "not-a-real-password" not in repr(own_state)
    assert adapter.password_env == "CLAWFLIGHT_IMAP_PASSWORD"
    # By default the source of truth is the process environment, not a copy.
    assert ImapAdapter("imap.example.test", "user")._environ is os.environ


def test_the_shipped_adapters_satisfy_the_adapter_protocol(fixtures) -> None:
    assert isinstance(MboxAdapter(fixtures / "inbox.mbox"), MailboxAdapter)
    assert isinstance(_adapter(_FakeIMAP([RAW])), MailboxAdapter)


def test_a_custom_adapter_only_has_to_implement_fetch() -> None:
    # This is the whole "bring your own adapter" contract.
    class CalendarAdapter:
        def fetch(self, limit: int = 50):
            return [_message()]

    candidates, skipped = messages_to_candidates(CalendarAdapter().fetch(), TRUSTED)

    assert isinstance(CalendarAdapter(), MailboxAdapter)
    assert [item.confirmation_code for item in candidates] == ["FAKE20"]
    assert skipped == []


def test_email_utils_date_parsing_is_used_for_received_at() -> None:
    # Guards the assumption that RFC 2822 dates keep their offset.
    parsed = email.utils.parsedate_to_datetime("Sat, 11 Jul 2026 09:14:02 +0000")

    assert parsed.tzinfo is not None
