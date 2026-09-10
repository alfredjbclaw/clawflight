"""Calendar-export parsing against the synthetic multi-person fixture."""
from clawflight.parse import (
    AIRPORT_TIMEZONES,
    airport_timezone,
    parse_calendar_events,
    parse_cancellation,
    parse_cancellations,
    parse_passenger,
    parse_schedule_change,
)


def test_multileg_itinerary_becomes_one_flight_per_leg(calendar_text) -> None:
    # Given: the synthetic calendar export.
    parsed = parse_calendar_events(calendar_text, default_year=2026)

    # Then: the two-leg trip becomes one parsed flight per leg with booking detail.
    itinerary = [
        flight for flight in parsed if flight.hints["source_id"] == "cal-0001"
    ]
    assert [(flight.leg.carrier, flight.leg.number) for flight in itinerary] == [
        ("AA", 4912),
        ("AA", 1203),
    ]
    assert [(flight.leg.origin, flight.leg.dest) for flight in itinerary] == [
        ("ASE", "DFW"),
        ("DFW", "JFK"),
    ]
    assert [flight.leg.seat for flight in itinerary] == ["10C", "21D"]
    assert all(flight.leg.conf_code == "FAKE01" for flight in itinerary)
    assert all(flight.leg.date == "2026-07-11" for flight in itinerary)
    assert itinerary[0].leg.sched_dep_iso == "2026-07-11T12:51:00-06:00"
    assert itinerary[1].leg.sched_arr_iso == "2026-07-11T22:30:00-04:00"


def test_named_airline_and_passenger_resolve_from_an_award_receipt(calendar_text) -> None:
    # Given: the award-receipt block, whose carrier is named rather than coded.
    parsed = parse_calendar_events(calendar_text, default_year=2026)

    booking = next(
        flight for flight in parsed if flight.hints["source_id"] == "cal-0004"
    )

    # Then: the airline name resolves to IATA and booking hints are preserved.
    assert (booking.leg.carrier, booking.leg.number) == ("DL", 767)
    assert (booking.leg.origin, booking.leg.dest) == ("JFK", "LAX")
    assert booking.leg.conf_code == "FAKE02"
    assert booking.leg.seat == "22E"
    assert booking.hints["passenger_name"] == "ALEXANDRA MORGAN KESTREL"


def test_city_and_iata_route_endpoints_are_both_resolved(calendar_text) -> None:
    # Given: a booking whose location mixes a city name with an IATA code.
    parsed = parse_calendar_events(calendar_text, default_year=2026)

    booking = next(
        flight for flight in parsed if flight.hints["source_id"] == "cal-0005"
    )

    assert (booking.leg.origin, booking.leg.dest) == ("ASE", "LAX")


def test_window_times_are_used_when_notes_carry_no_leg_times(calendar_text) -> None:
    # Given: a possessive-title booking whose notes describe the flight in prose.
    parsed = parse_calendar_events(calendar_text, default_year=2026)

    booking = next(
        flight for flight in parsed if flight.hints["source_id"] == "cal-0012"
    )

    # Then: the event's own time window supplies the schedule instead of nothing.
    assert booking.leg.sched_dep_iso == "2026-07-12T21:15:00-07:00"
    assert booking.leg.sched_arr_iso == "2026-07-12T23:59:00-04:00"


def test_attendee_only_event_still_parses_its_leg(calendar_text) -> None:
    parsed = parse_calendar_events(calendar_text, default_year=2026)

    booking = next(
        flight for flight in parsed if flight.hints["source_id"] == "cal-0030"
    )

    assert (booking.leg.carrier, booking.leg.number) == ("B6", 622)
    assert booking.hints["attendees"] == [
        "sam.kestrel@example.com",
        "ops@example.com",
    ]


def test_non_flight_blocks_produce_nothing(calendar_text) -> None:
    # Given: the fixture's dinner, reminder and hotel blocks.
    source_ids = {
        flight.hints["source_id"]
        for flight in parse_calendar_events(calendar_text, default_year=2026)
    }

    # Then: none of them became a flight, and a bare confirmation code did not
    # invent one either.
    assert {"cal-0020", "cal-0021", "cal-0022"}.isdisjoint(source_ids)


def test_iata_designator_in_notes_is_accepted() -> None:
    # Given: a flight-like block whose IATA designator appears only in notes.
    calendar_text = """\
── Sunday, Jul 12, 2026 ────────────────────────────────────────

  Flight booking
  ID: notes-only-designator
  Location: JFK to LAX
  Notes:
    AA 123 confirmed
"""

    parsed = parse_calendar_events(calendar_text, default_year=2026)

    assert [(flight.leg.carrier, flight.leg.number) for flight in parsed] == [("AA", 123)]


def test_unknown_and_defunct_carrier_designators_are_rejected() -> None:
    # Given: prose carrying a non-IATA "US 100" reference alongside airline words.
    calendar_text = """\
── Sunday, Jul 12, 2026 ────────────────────────────────────────

  Flight logistics
  ID: prose-us
  Location: JFK to LAX
  Notes:
    Meet at the gate, US 100 dollars for the airline lounge day pass.
"""

    # Then: no phantom "US" flight is created from prose.
    assert parse_calendar_events(calendar_text, default_year=2026) == []


def test_full_timezone_table_gives_non_hub_airports_iso_times() -> None:
    # Given: a booking between two airports outside the original hardcoded set.
    calendar_text = """\
── Sunday, Jul 12, 2026 ────────────────────────────────────────

  Flight booking
  ID: sfo-sea
  Location: SFO to SEA
  Notes:
    SFO 8:00 AM AA 100 SEA 10:30 AM
"""

    parsed = parse_calendar_events(calendar_text, default_year=2026)

    assert len(parsed) == 1
    assert parsed[0].leg.sched_dep_iso == "2026-07-12T08:00:00-07:00"
    assert parsed[0].leg.sched_arr_iso == "2026-07-12T10:30:00-07:00"


def test_airport_timezone_covers_the_packaged_table() -> None:
    assert len(AIRPORT_TIMEZONES) > 50
    assert airport_timezone("SFO") == "America/Los_Angeles"
    assert airport_timezone("lhr") == "Europe/London"
    assert airport_timezone("PHX") == "America/Phoenix"
    assert airport_timezone("ZZZ") is None
    assert airport_timezone(None) is None


def test_schedule_change_reports_new_and_original_times() -> None:
    notes = (
        "Your flight changed We made these changes to your flight: "
        "* New depart time: 6:07 PM * New arrival time: 11:00 PM "
        "NEW FLIGHT Jul 11, 2026 DFW JFK 6:07 PM 11:00 PM AA 1203 "
        "ORIGINAL FLIGHT Jul 11, 2026 DFW JFK 5:39 PM 10:30 PM AA 1203"
    )

    assert parse_schedule_change(notes) == {
        "flight": "AA1203",
        "new_dep": "6:07 PM",
        "new_arr": "11:00 PM",
        "original_dep": "5:39 PM",
        "original_arr": "10:30 PM",
    }


def test_schedule_change_returns_none_for_unrelated_text() -> None:
    assert parse_schedule_change("Your itinerary is confirmed. Have a good trip.") is None


def test_schedule_change_prefers_the_flight_in_the_new_section() -> None:
    # Given: a change notice with an unrelated reference before its schedule.
    notes = (
        "Your flight changed. Reference AB 99. New depart time: 6:07 PM. "
        "New arrival time: 11:00 PM. NEW FLIGHT DFW JFK AA 123. "
        "ORIGINAL FLIGHT DFW JFK 5:39 PM 10:30 PM AA 123."
    )

    change = parse_schedule_change(notes)

    assert change is not None and change["flight"] == "AA123"


def test_passenger_name_is_extracted_and_absent_names_return_none() -> None:
    assert (
        parse_passenger("Passenger Info Name: ALEXANDRA MORGAN KESTREL SkyMiles #x")
        == "ALEXANDRA MORGAN KESTREL"
    )
    assert parse_passenger("Passenger Info Name: SAM KESTREL") == "SAM KESTREL"
    assert parse_passenger("No passenger details are present.") is None


def test_cancellation_returns_the_referenced_confirmation_code() -> None:
    notice = "Your trip has been cancelled. Confirmation code: FAKE05. No action needed."

    assert parse_cancellation(notice) == "FAKE05"
    assert parse_cancellation("Your itinerary is confirmed. Conf: FAKE01") is None


def test_cancellations_are_harvested_from_standalone_notices(calendar_text) -> None:
    # Given: the fixture's standalone cancellation block, which has no route or
    # times and therefore never survives parse_calendar_events.
    assert parse_cancellations(calendar_text, default_year=2026) == ["FAKE05"]
    assert parse_cancellations(calendar_text + calendar_text, default_year=2026) == [
        "FAKE05"
    ]


def test_award_receipt_fare_rules_are_not_read_as_a_cancellation() -> None:
    # Given: award fare-rule boilerplate that mentions cancellation twice.
    receipt = (
        "Confirmation code: FAKE02 AWARD RECEIPT You are all set. If your plans "
        "change you may adjust or cancel this itinerary online. Award tickets "
        "are usually non-refundable once the risk free cancellation period ends. "
        "Failure to appear for any flight without notice will result in "
        "cancellation of the remaining reservation."
    )

    # Then: boilerplate must never cancel an active booking.
    assert parse_cancellation(receipt) is None


def test_declarative_cancellation_phrasings_all_match() -> None:
    for notice in (
        "Your trip has been canceled. Confirmation code: FAKE05",
        "Your flight was cancelled. Confirmation: FAKE05",
        "Cancelled itinerary — Confirmation code: FAKE05",
        "Cancellation confirmation for Confirmation code: FAKE05",
    ):
        assert parse_cancellation(notice) == "FAKE05", notice
