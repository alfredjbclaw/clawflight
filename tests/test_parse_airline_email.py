"""Airline email layout parsing against the synthetic fixtures."""
from clawflight.parse import parse_airline_email


def _parse(fixtures, name, year=2026):
    return parse_airline_email((fixtures / name).read_text(encoding="utf-8"), year)


def test_receipt_parses_each_day_and_deduplicates_repeated_flights(fixtures) -> None:
    # Given: a receipt that lists DELTA 667 twice under the same day header.
    flights = _parse(fixtures, "email_delta_receipt.txt")

    assert [
        (
            item.leg.carrier,
            item.leg.number,
            item.leg.date,
            item.leg.origin,
            item.leg.dest,
            item.leg.conf_code,
            item.leg.seat,
            item.hints["passenger_name"],
        )
        for item in flights
    ] == [
        ("DL", 667, "2026-05-11", "JFK", "SFO", "FAKEA1", "12A", "ROBIN J KESTREL"),
        ("DL", 1226, "2026-05-14", "SJC", "LAX", "FAKEA1", "8C", "ROBIN J KESTREL"),
    ]
    assert sum(item.leg.number == 667 for item in flights) == 1
    # Receipts carry no timezone-resolvable times, so the ISO fields stay empty.
    assert all(item.leg.sched_dep_iso is None for item in flights)


def test_receipt_keeps_a_leg_whose_city_is_unmapped(fixtures) -> None:
    # Given: the same receipt with an origin city the table does not know.
    text = (fixtures / "email_delta_receipt.txt").read_text(encoding="utf-8")
    text = text.replace("NYC-KENNEDY", "MYSTERY CITY")

    flights = parse_airline_email(text, 2026)

    # Then: the leg survives with a missing airport rather than a guessed one.
    assert flights[0].leg.number == 667
    assert flights[0].leg.origin is None
    assert flights[0].leg.dest == "SFO"


def test_trip_confirmation_parses_each_leg_with_seat_and_greeting(fixtures) -> None:
    flights = _parse(fixtures, "email_aa_trip_confirmation.txt")

    assert [
        (
            item.leg.carrier,
            item.leg.number,
            item.leg.date,
            item.leg.origin,
            item.leg.dest,
            item.leg.conf_code,
            item.leg.seat,
            item.hints["passenger_name"],
        )
        for item in flights
    ] == [
        ("AA", 5134, "2026-02-27", "ITH", "CLT", "FAKEA2", "12A", "Sam Kestrel"),
        ("AA", 776, "2026-02-28", "CLT", "LAX", "FAKEA2", "4C", "Sam Kestrel"),
    ]
    assert all(item.leg.sched_arr_iso is None for item in flights)


def test_schedule_change_uses_only_the_new_itinerary(fixtures) -> None:
    # Given: a notice showing both the new and the original flight.
    flights = _parse(fixtures, "email_delta_schedule_change.txt")

    # Then: only the replacement itinerary becomes a leg.
    assert len(flights) == 1
    assert (
        flights[0].leg.carrier,
        flights[0].leg.number,
        flights[0].leg.date,
        flights[0].leg.origin,
        flights[0].leg.dest,
        flights[0].leg.conf_code,
    ) == ("DL", 365, "2026-05-21", "JFK", "SFO", "FAKEA3")
    assert "your flight changed" in flights[0].hints["notes_excerpt"].lower()


def test_status_email_without_a_booking_is_ignored(fixtures) -> None:
    assert _parse(fixtures, "email_aa_current_flight.txt") == []


def test_empty_garbage_and_oversized_bodies_are_ignored() -> None:
    assert parse_airline_email("", 2026) == []
    assert parse_airline_email("not an airline itinerary", 2026) == []
    assert parse_airline_email("x" * (256 * 1024 + 1), 2026) == []
    assert parse_airline_email(None, 2026) == []


def test_a_damaged_date_does_not_stop_ingestion(fixtures) -> None:
    # Given: a caller-supplied year that cannot form a valid date.
    text = (fixtures / "email_delta_receipt.txt").read_text(encoding="utf-8")

    # Then: the parser returns nothing instead of raising into the sweep.
    assert parse_airline_email(text, 10**9) == []
