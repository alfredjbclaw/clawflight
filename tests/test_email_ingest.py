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
Departure Time: 4:55 PM
Arrival Time: 7:30 PM
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
    assert candidate.sched_dep_iso == "2026-08-19T16:55:00-04:00"
    assert candidate.sched_arr_iso == "2026-08-19T19:30:00-07:00"
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


def test_an_unknown_carrier_leg_does_not_discard_a_valid_leg() -> None:
    body = SINGLE_LEG + """
Flight: ZZ 249
Date: 2026-08-20
From: LAX
To: JFK
Confirmation Code: FAKE20
"""

    result = _ingest(body)

    assert [(candidate.carrier, candidate.number) for candidate in result] == [("DL", 248)]
    assert [(leg.carrier, leg.number, leg.reason) for leg in result.skipped_legs] == [
        ("ZZ", 249, "unknown carrier")
    ]


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

@pytest.mark.parametrize(
    ("fixture_name", "expected"),
    [
        ("email_labelled_hash_compact.txt", ("DL", 767, "ATL", "JFK")),
        ("email_labelled_bare_compact.txt", ("UA", 410, "EWR", "ORD")),
        ("email_labelled_hash_hyphen.txt", ("AA", 91, "JFK", "LAX")),
        ("email_labelled_bare_hyphen.txt", ("B6", 611, "BOS", "LAX")),
        ("email_labelled_bare_space.txt", ("AS", 332, "SEA", "SFO")),
        ("email_labelled_hash_space.txt", ("WN", 925, "DEN", "LGA")),
    ],
)
def test_realistic_labelled_shapes_produce_candidates(fixtures, fixture_name, expected) -> None:
    (candidate,) = _ingest((fixtures / fixture_name).read_text(encoding="utf-8"))

    assert (candidate.carrier, candidate.number, candidate.origin, candidate.destination) == expected


def test_one_malformed_leg_does_not_discard_a_good_leg() -> None:
    body = """Booking Reference: FAKE41
Flight: DL 767
Date: 2026-11-09
From: ATL
To: JFK

Flight: DL 768
Date: definitely not a date
From: JFK
To: ATL
"""

    result = _ingest(body)

    assert [(candidate.carrier, candidate.number) for candidate in result] == [("DL", 767)]
    assert [(leg.number, leg.reason) for leg in result.skipped_legs] == [
        (768, "invalid service date")
    ]


def test_every_malformed_leg_still_produces_no_candidates_and_reports_each_leg() -> None:
    body = """Booking Reference: FAKE42
Flight: DL 767
Date: bad one
From: ATL
To: JFK

Flight: UA 410
Date: bad two
From: EWR
To: ORD
"""

    result = _ingest(body)

    assert result == ()
    assert [(leg.carrier, leg.number, leg.reason) for leg in result.skipped_legs] == [
        ("DL", 767, "invalid service date"),
        ("UA", 410, "invalid service date"),
    ]

@pytest.mark.parametrize(
    ("raw_date", "expected"),
    [
        ("2026-11-09", "2026-11-09"),
        ("November 9, 2026", "2026-11-09"),
        ("Nov 9, 2026", "2026-11-09"),
        ("09 Nov 2026", "2026-11-09"),
        ("9 November 2026", "2026-11-09"),
        ("2026/11/09", "2026-11-09"),
    ],
)
def test_supported_service_dates_survive_ingestion(raw_date, expected) -> None:
    (candidate,) = _ingest(SINGLE_LEG.replace("2026-08-19", raw_date))

    assert candidate.service_date == expected


def test_ambiguous_slash_date_uses_us_month_first_and_warns(caplog) -> None:
    with caplog.at_level("WARNING", logger="clawflight.email_ingest"):
        (candidate,) = _ingest(SINGLE_LEG.replace("2026-08-19", "11/09/2026"))

    assert candidate.service_date == "2026-11-09"
    assert "US month-first" in caplog.text
    assert "11/09/2026" in caplog.text


def test_unambiguous_slash_date_uses_day_first_without_warning(caplog) -> None:
    with caplog.at_level("WARNING", logger="clawflight.email_ingest"):
        (candidate,) = _ingest(SINGLE_LEG.replace("2026-08-19", "25/12/2026"))

    assert candidate.service_date == "2026-12-25"
    assert not caplog.records


@pytest.mark.parametrize(
    ("origin", "expected"),
    [("Atlanta", "ATL"), ("(ATL)", "ATL"), ("ATL", "ATL")],
)
def test_airport_shapes_resolve_through_ingestion(origin, expected) -> None:
    (candidate,) = _ingest(SINGLE_LEG.replace("From: JFK", "From: {}".format(origin)))

    assert candidate.origin == expected


def test_unresolvable_airport_skips_the_leg_and_warns(caplog) -> None:
    with caplog.at_level("WARNING", logger="clawflight.email_ingest"):
        result = _ingest(SINGLE_LEG.replace("From: JFK", "From: Mystery Borough"))

    assert result == ()
    assert result.skipped_legs[0].reason == "invalid route"
    assert "unresolved airport or city" in caplog.text
    assert "Mystery Borough" in caplog.text


def test_unknown_prose_designators_remain_gated() -> None:
    for token in ("US 100", "RE 2024"):
        body = "Confirmation Code: FAKE51\n{}\nDate: 2026-11-09\nFrom: ATL\nTo: JFK\n".format(token)
        assert _ingest(body) == ()


def test_adversarial_near_matches_are_bounded() -> None:
    import time
    from clawflight.email_ingest import MAX_BODY_CHARS

    body = ("Flight Number ################################################ DL- almost 9999\n" * 6000)[
        :MAX_BODY_CHARS
    ]
    assert len(body) == MAX_BODY_CHARS
    started = time.monotonic()

    assert _ingest(body) == ()
    assert time.monotonic() - started < 2.0


def test_max_size_multiline_whitespace_near_match_is_bounded() -> None:
    import time
    from clawflight.email_ingest import MAX_BODY_CHARS

    body = " \n" * (MAX_BODY_CHARS // 2)
    assert len(body) == MAX_BODY_CHARS
    started = time.monotonic()

    assert _ingest(body) == ()
    assert time.monotonic() - started < 2.0


def test_max_size_single_line_whitespace_near_match_is_bounded() -> None:
    import time
    from clawflight.email_ingest import MAX_BODY_CHARS

    body = "DL" + " " * (MAX_BODY_CHARS - 2)
    assert len(body) == MAX_BODY_CHARS
    started = time.monotonic()

    assert _ingest(body) == ()
    assert time.monotonic() - started < 2.0
