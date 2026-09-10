from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from clawflight.email_ingest import (
    TrustedSenderPolicy,
    ingest_email,
    is_trusted_sender,
)


OBSERVED = datetime(2026, 7, 20, 14, 30, tzinfo=timezone.utc)
TRUSTED = {"air.example", "receipts@trusted-agency.example"}

SINGLE_LEG = """Your flight is confirmed
Confirmation Code: FAKE20
Traveler: ALEXANDRA MORGAN KESTREL

Flight: DL 248
Date: 2026-08-19
From: JFK
To: LAX
"""


def _ingest(body, sender="Trips <confirmations@air.example>", source_id="trip-1001", **kwargs):
    return ingest_email(
        source_id=source_id,
        sender=sender,
        observed_at=OBSERVED,
        body=body,
        trusted_sources=kwargs.pop("trusted_sources", TRUSTED),
        **kwargs,
    )


def test_trusted_policy_handles_addresses_subdomains_and_lookalikes() -> None:
    policy = TrustedSenderPolicy(
        exact_addresses={"receipts@trusted-agency.example"},
        base_domains={"air.example"},
    )

    assert policy.trusts("Air Example <confirmations@mail.air.example>")
    assert policy.trusts("receipts@trusted-agency.example")
    # A base domain matches true subdomains only, never a lookalike suffix.
    assert not policy.trusts("confirmations@air.example.evil.test")
    assert not policy.trusts("other@trusted-agency.example")
    assert is_trusted_sender("CONFIRMATIONS@AIR.EXAMPLE", {"air.example"})
    assert not is_trusted_sender("not-an-address", {"air.example"})


def test_policy_rejects_malformed_allow_list_entries() -> None:
    with pytest.raises(ValueError):
        TrustedSenderPolicy(exact_addresses={"not-an-address"})
    with pytest.raises(ValueError):
        TrustedSenderPolicy(base_domains={"has space"})
    with pytest.raises(ValueError):
        TrustedSenderPolicy(base_domains={"has@at.example"})


def test_trusted_message_produces_an_immutable_candidate_and_bounded_evidence() -> None:
    (candidate,) = _ingest(SINGLE_LEG, organizer="Travel Desk")

    assert (candidate.carrier, candidate.number) == ("DL", 248)
    assert (candidate.service_date, candidate.origin, candidate.destination) == (
        "2026-08-19",
        "JFK",
        "LAX",
    )
    assert candidate.confirmation_code == "FAKE20"
    assert candidate.traveler == "ALEXANDRA MORGAN KESTREL"
    assert candidate.evidence.sender_identity == "confirmations@air.example"
    assert candidate.evidence.organizer_identity == "Travel Desk"
    assert candidate.evidence.observed_at is OBSERVED
    assert candidate.evidence.source_kind == "email"
    assert len(candidate.evidence.digest) == 64
    # The body is deliberately absent from durable provenance.
    assert "body" not in vars(candidate.evidence)
    with pytest.raises(FrozenInstanceError):
        candidate.origin = "ATL"
    with pytest.raises(FrozenInstanceError):
        candidate.evidence.digest = "different"


def test_exact_agency_sender_is_accepted() -> None:
    (candidate,) = _ingest(
        SINGLE_LEG.replace("DL 248", "AA 91"),
        sender="receipts@trusted-agency.example",
    )

    assert (candidate.carrier, candidate.number) == ("AA", 91)


def test_untrusted_lookalike_and_incomplete_bodies_produce_nothing() -> None:
    assert _ingest(SINGLE_LEG, sender="confirmations@air.example.evil.test") == ()
    assert _ingest("Save on your next trip from JFK to LAX with code SPECIAL") == ()
    assert _ingest(SINGLE_LEG.replace("Confirmation Code: FAKE20\n", "")) == ()
    assert _ingest(SINGLE_LEG.replace("Date: 2026-08-19\n", "")) == ()
    assert _ingest(SINGLE_LEG.replace("To: LAX", "To: JFK")) == ()


def test_an_unknown_carrier_rejects_the_whole_message() -> None:
    # A designator we do not recognise means we misread the body; refuse all of it.
    assert _ingest(SINGLE_LEG.replace("DL 248", "ZZ 248")) == ()


def test_ambiguous_message_level_fields_are_refused() -> None:
    # Given: two different confirmation codes above the flight, so neither can
    # be the message-level value and the leg carries none of its own.
    body = "Confirmation Code: FAKE20\nConfirmation Code: FAKE21\n" + (
        "\nFlight: DL 248\nDate: 2026-08-19\nFrom: JFK\nTo: LAX\n"
    )

    assert _ingest(body) == ()


def test_a_leg_level_confirmation_overrides_an_ambiguous_message_level_one() -> None:
    # Given: a per-leg code, which is the more specific evidence.
    body = "Confirmation Code: FAKE20\n" + (
        "\nFlight: DL 248\nDate: 2026-08-19\nFrom: JFK\nTo: LAX\n"
        "Confirmation Code: FAKE21\n"
    )

    (candidate,) = _ingest(body)

    assert candidate.confirmation_code == "FAKE21"


def test_evidence_identity_is_repeatable_and_content_sensitive() -> None:
    first = _ingest(SINGLE_LEG)[0].evidence
    duplicate = _ingest(SINGLE_LEG)[0].evidence
    changed = _ingest(SINGLE_LEG.replace("To: LAX", "To: SFO"))[0].evidence

    assert first.identity == duplicate.identity
    assert first.digest == duplicate.digest
    assert changed.identity != first.identity


def test_line_ending_rewrites_do_not_change_the_digest() -> None:
    unix = _ingest(SINGLE_LEG)[0].evidence
    windows = _ingest(SINGLE_LEG.replace("\n", "\r\n"))[0].evidence

    assert unix.digest == windows.digest


def test_multiple_legs_share_confirmation_and_durable_evidence() -> None:
    body = """Itinerary confirmed
Booking Reference: FAKE21
Passenger: SAMUEL T KESTREL

Flight Number: UA 410
Departure Date: August 21, 2026
Route: EWR -> ORD

Flight Number: UA 882
Departure Date: August 21, 2026
Origin: ORD
Destination: SFO
"""
    candidates = _ingest(body)

    assert [
        (item.number, item.origin, item.destination) for item in candidates
    ] == [(410, "EWR", "ORD"), (882, "ORD", "SFO")]
    assert {item.confirmation_code for item in candidates} == {"FAKE21"}
    # One message, one evidence object shared by every leg it produced.
    assert candidates[0].evidence is candidates[1].evidence


def test_too_many_legs_are_refused() -> None:
    leg = "\nFlight: DL {number}\nDate: 2026-08-19\nFrom: JFK\nTo: LAX\n"
    body = "Confirmation Code: FAKE20\n" + "".join(
        leg.format(number=index) for index in range(1, 10)
    )

    assert _ingest(body) == ()


def test_oversized_bodies_are_refused_without_reading_them() -> None:
    assert _ingest("x" * (256 * 1024 + 1)) == ()


def test_programmer_errors_in_provenance_raise() -> None:
    with pytest.raises(ValueError):
        _ingest(SINGLE_LEG, source_id="   ")
    with pytest.raises(ValueError):
        ingest_email(
            source_id="trip-1001",
            sender="confirmations@air.example",
            observed_at="2026-07-20",
            body=SINGLE_LEG,
            trusted_sources=TRUSTED,
        )
