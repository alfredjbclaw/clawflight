"""Airline email layout parsing against the synthetic fixtures."""
import time

import pytest

from clawflight.models import polling_callsign
from clawflight.parse import (
    AIRLINE_EMAIL_CITY_TO_IATA,
    AIRLINE_NAME_TO_IATA,
    KNOWN_CARRIERS,
    parse_airline_email,
)
from clawflight.airports import default_airports


def _parse(fixtures, name, year=2026):
    return parse_airline_email((fixtures / name).read_text(encoding="utf-8"), year)


@pytest.mark.parametrize(
    ("fixture_name", "carrier", "number", "date", "route"),
    [
        ("email_united_generic.txt", "UA", 184, "2026-09-18", ("JFK", "SFO")),
        ("email_jetblue_generic.txt", "B6", 611, "2026-10-03", ("BOS", "LAX")),
        ("email_southwest_generic.txt", "WN", 925, "2026-11-07", ("DEN", "LGA")),
        ("email_alaska_generic.txt", "AS", 332, "2026-11-12", ("SEA", "SFO")),
        ("email_spirit_generic.txt", "NK", 707, "2026-11-19", ("LGA", "ORD")),
        ("email_frontier_generic.txt", "F9", 418, "2026-12-02", ("DEN", "LAX")),
        ("email_british_airways_generic.txt", "BA", 178, "2026-12-08", ("JFK", "LHR")),
        ("email_air_france_generic.txt", "AF", 9, "2026-12-14", ("JFK", "CDG")),
        ("email_lufthansa_generic.txt", "LH", 401, "2026-12-19", ("JFK", "FRA")),
        ("email_emirates_generic.txt", "EK", 202, "2026-12-27", ("JFK", "DXB")),
    ],
)
def test_generic_email_parses_known_airline(
    fixtures, fixture_name, carrier, number, date, route
) -> None:
    flights = _parse(fixtures, fixture_name)

    assert len(flights) == 1
    assert (flights[0].leg.carrier, flights[0].leg.number, flights[0].leg.date) == (
        carrier, number, date
    )
    assert (flights[0].leg.origin, flights[0].leg.dest) == route


def test_delta_receipt_layout_resolves_a_united_carrier(fixtures) -> None:
    text = (fixtures / "email_delta_receipt.txt").read_text(encoding="utf-8")
    text = text.replace("DELTA", "United Airlines")

    flights = parse_airline_email(text, 2026)

    assert [(item.leg.carrier, item.leg.number) for item in flights] == [
        ("UA", 667),
        ("UA", 1226),
    ]


@pytest.mark.parametrize("token", ["US 100", "RE 2024"])
def test_unknown_designator_in_prose_does_not_become_a_flight(token) -> None:
    text = "\n".join([
        "Confirmation: FAKEP1",
        "Date: 2026-10-10",
        "Flight: {}".format(token),
        "Route: JFK -> LAX",
    ])

    assert parse_airline_email(text, 2026) == []


def test_airline_name_mappings_are_known_designators() -> None:
    assert set(AIRLINE_NAME_TO_IATA.values()) <= KNOWN_CARRIERS


def test_airline_email_city_table_covers_known_airports() -> None:
    mapped_airports = set(AIRLINE_EMAIL_CITY_TO_IATA.values())

    assert len(mapped_airports) >= 40
    assert mapped_airports <= set(default_airports())


def test_hub_cities_in_an_airline_email_resolve(fixtures) -> None:
    flights = _parse(fixtures, "email_hub_city_routes.txt")

    assert [
        (flight.leg.origin, flight.leg.dest) for flight in flights
    ] == [
        ("ATL", "DTW"),
        ("DTW", "MSP"),
        ("MSP", "SLC"),
        ("MCI", "ATL"),
        ("DAL", "MDW"),
        ("MDW", "HOU"),
    ]


def test_generic_email_requires_date_and_route() -> None:
    base = "Flight: United Airlines 184"

    assert parse_airline_email(base + "\nRoute: JFK -> SFO", 2026) == []
    assert parse_airline_email(base + "\nDate: 2026-09-18", 2026) == []


def test_adversarial_near_matches_are_bounded_by_the_email_size_cap() -> None:
    text = ("Flight: US 100 almost\n" * 9000)[:200_000]

    started = time.monotonic()
    assert parse_airline_email(text, 2026) == []
    assert time.monotonic() - started < 2.0


def test_explicit_operating_airline_without_number_is_partial_and_polls_marketed() -> None:
    text = "\n".join([
        "Confirmation: FAKEO1",
        "Date: 2026-10-10",
        "Flight: American Airlines 4912 operated by SkyWest Airlines as American Eagle",
        "Route: JFK -> LAX",
    ])

    flight = parse_airline_email(text, 2026)[0].leg

    assert (flight.operating_carrier, flight.operating_number) == ("OO", None)
    assert polling_callsign(flight) == "AAL4912"


def test_explicit_operating_flight_sets_both_operating_fields() -> None:
    text = "\n".join([
        "Confirmation: FAKEO2",
        "Date: 2026-10-10",
        "Flight: American Airlines 4912",
        "Route: JFK -> LAX",
        "OO 5678 operated by SkyWest Airlines",
    ])

    flight = parse_airline_email(text, 2026)[0].leg

    assert (flight.operating_carrier, flight.operating_number) == ("OO", 5678)
    assert polling_callsign(flight) == "OO5678"


def test_email_without_operated_by_text_has_no_operating_identity() -> None:
    flight = parse_airline_email("\n".join([
        "Date: 2026-10-10",
        "Flight: American Airlines 4912",
        "Route: JFK -> LAX",
        "Connection on SkyWest Airlines 5678",
    ]), 2026)[0].leg

    assert (flight.operating_carrier, flight.operating_number) == (None, None)


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
    assert flights[0].leg.sched_dep_iso == "2026-05-11T06:00:00-04:00"
    assert flights[0].leg.sched_arr_iso == "2026-05-11T09:35:00-07:00"


@pytest.mark.parametrize(
    "departure_line",
    ["6:00 XM SAN FRANCISCO", "garbage SAN FRANCISCO", "SAN FRANCISCO"],
)
def test_receipt_keeps_route_when_departure_time_is_bad_or_missing(
    fixtures, departure_line
) -> None:
    text = (fixtures / "email_delta_receipt.txt").read_text(encoding="utf-8")
    text = text.replace("6:00 AM SAN FRANCISCO", departure_line)

    flight = parse_airline_email(text, 2026)[0]

    assert (flight.leg.number, flight.leg.origin, flight.leg.dest) == (667, "JFK", "SFO")
    assert flight.leg.sched_dep_iso is None
    assert flight.leg.sched_arr_iso == "2026-05-11T09:35:00-07:00"


def test_receipt_keeps_and_surfaces_a_leg_whose_city_is_unmapped(fixtures, caplog) -> None:
    # Given: the same receipt with an origin city the table does not know.
    text = (fixtures / "email_delta_receipt.txt").read_text(encoding="utf-8")
    text = text.replace("NYC-KENNEDY", "MYSTERY CITY")

    caplog.set_level("WARNING", logger="clawflight.parse")
    flights = parse_airline_email(text, 2026)

    # Then: the leg survives with a missing airport rather than a guessed one.
    assert flights[0].leg.number == 667
    assert flights[0].leg.origin is None
    assert flights[0].leg.dest == "SFO"
    assert flights[0].leg.sched_dep_iso is None
    assert flights[0].leg.sched_arr_iso == "2026-05-11T09:35:00-07:00"
    assert flights[0].hints["unresolved_airports"] == ["MYSTERY CITY"]
    assert "MYSTERY CITY" in caplog.text
    assert "DL 667" in caplog.text


@pytest.mark.parametrize(
    "city",
    ["LOS ANGELES", "ORLANDO", "PHOENIX", "BUENOS AIRES", "DUBAI", "ROME"],
)
def test_ambiguous_city_only_text_is_reported_not_guessed(city, caplog) -> None:
    text = "\n".join([
        "** Confirmation Number ** FAKEB1",
        "** MON, 11MAY**DEPART**ARRIVE**",
        "DELTA 667",
        city,
        "6:00 AM (LAX)",
        "9:35 AM",
    ])

    caplog.set_level("WARNING", logger="clawflight.parse")
    flight = parse_airline_email(text, 2026)[0]

    assert (flight.leg.origin, flight.leg.dest) == (None, "LAX")
    assert flight.hints["unresolved_airports"] == [city]
    assert city in caplog.text


def test_ambiguous_san_jose_needs_a_qualifier(fixtures, caplog) -> None:
    text = (fixtures / "email_delta_receipt.txt").read_text(encoding="utf-8")
    ambiguous = text.replace("SAN JOSE, CALIFORNIA", "SAN JOSE")

    caplog.set_level("WARNING", logger="clawflight.parse")
    unresolved = parse_airline_email(ambiguous, 2026)[1]
    qualified = parse_airline_email(text, 2026)[1]

    assert unresolved.leg.origin is None
    assert unresolved.hints["unresolved_airports"] == ["SAN JOSE"]
    assert "SAN JOSE" in caplog.text
    assert qualified.leg.origin == "SJC"


def test_san_jose_costa_rica_qualifier_resolves_to_sjo(fixtures) -> None:
    text = (fixtures / "email_delta_receipt.txt").read_text(encoding="utf-8")
    costa_rica = text.replace("SAN JOSE, CALIFORNIA", "SAN JOSE, COSTA RICA")

    flight = parse_airline_email(costa_rica, 2026)[1]

    assert flight.leg.origin == "SJO"


@pytest.mark.parametrize(
    ("route", "expected"),
    [
        ("JFK -> LAX", ("JFK", "LAX")),
        ("(JFK) -> (LAX)", ("JFK", "LAX")),
        ("ZZZ -> LAX", (None, "LAX")),
    ],
)
def test_airline_email_accepts_only_known_iata_codes(route, expected, caplog) -> None:
    caplog.set_level("WARNING", logger="clawflight.parse")
    text = "\n".join([
        "Confirmation: FAKEI1",
        "Date: 2026-10-10",
        "Flight: Delta 123",
        "Route: {}".format(route),
    ])

    flight = parse_airline_email(text, 2026)[0]

    assert (flight.leg.origin, flight.leg.dest) == expected
    if expected[0] is None:
        assert flight.hints["unresolved_airports"] == ["ZZZ"]
        assert "ZZZ" in caplog.text


@pytest.mark.parametrize(
    ("layout", "origin", "destination", "expected", "unresolved"),
    [
        ("receipt", "(JFK)", "(LAX)", ("JFK", "LAX"), None),
        ("receipt", "(JFK)", "(ZZZ)", ("JFK", None), "(ZZZ)"),
        ("receipt", "(ZZZ)", "(LAX)", (None, "LAX"), "(ZZZ)"),
        ("trip", "(ITH)", "(CLT)", ("ITH", "CLT"), None),
        ("trip", "(ITH)", "(ZZZ)", ("ITH", None), "(ZZZ)"),
        ("trip", "(ZZZ)", "(CLT)", (None, "CLT"), "(ZZZ)"),
    ],
)
def test_parenthesised_iata_in_receipt_and_trip_layouts_is_resolved_or_reported(
    fixtures, caplog, layout, origin, destination, expected, unresolved
) -> None:
    fixture_name = (
        "email_delta_receipt.txt" if layout == "receipt" else "email_aa_trip_confirmation.txt"
    )
    text = (fixtures / fixture_name).read_text(encoding="utf-8")
    if layout == "receipt":
        text = text.replace("NYC-KENNEDY", origin, 1)
        text = text.replace("6:00 AM SAN FRANCISCO", "6:00 AM " + destination, 1)
    else:
        text = text.replace("ITH", origin, 1)
        text = text.replace("CLT", destination, 1)

    caplog.set_level("WARNING", logger="clawflight.parse")
    flight = parse_airline_email(text, 2026)[0]

    assert (flight.leg.origin, flight.leg.dest) == expected
    if unresolved is not None:
        assert flight.hints["unresolved_airports"] == [unresolved]
        assert unresolved in caplog.text


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
    assert flights[0].leg.sched_dep_iso == "2026-02-27T07:45:00-05:00"
    assert flights[0].leg.sched_arr_iso == "2026-02-27T10:20:00-05:00"


@pytest.mark.parametrize("departure_time", ["7:45 XM", ""])
def test_trip_confirmation_keeps_leg_when_departure_time_is_bad_or_missing(
    fixtures, departure_time
) -> None:
    text = (fixtures / "email_aa_trip_confirmation.txt").read_text(encoding="utf-8")
    text = text.replace("7:45 AM", departure_time, 1)

    flight = parse_airline_email(text, 2026)[0]

    assert (flight.leg.number, flight.leg.origin, flight.leg.dest) == (5134, "ITH", "CLT")
    assert flight.leg.sched_dep_iso is None
    assert flight.leg.sched_arr_iso == "2026-02-27T10:20:00-05:00"


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
    assert flights[0].leg.sched_dep_iso == "2026-05-21T06:00:00-04:00"
    assert flights[0].leg.sched_arr_iso == "2026-05-21T09:35:00-07:00"


@pytest.mark.parametrize("departure_time", ["6:00 XM", ""])
def test_schedule_change_keeps_new_leg_when_departure_time_is_bad_or_missing(
    fixtures, departure_time
) -> None:
    text = (fixtures / "email_delta_schedule_change.txt").read_text(encoding="utf-8")
    text = text.replace("6:00 AM", departure_time, 1)

    flight = parse_airline_email(text, 2026)[0]

    assert (flight.leg.number, flight.leg.origin, flight.leg.dest) == (365, "JFK", "SFO")
    assert flight.leg.sched_dep_iso is None
    assert flight.leg.sched_arr_iso == "2026-05-21T09:35:00-07:00"


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


def test_receipt_captures_every_name_and_keeps_the_first_primary(fixtures) -> None:
    text = (fixtures / "email_delta_receipt.txt").read_text(encoding="utf-8")
    text = text.replace(
        "Name: ROBIN J KESTREL",
        "Name: HARRIET Q VOSS\nName: TOBIAS VOSS",
    )

    flights = parse_airline_email(text, 2026)

    assert flights[0].hints["passenger_names"] == ["HARRIET Q VOSS", "TOBIAS VOSS"]
    assert flights[0].hints["passenger_name"] == "HARRIET Q VOSS"


def test_untitled_trip_greeting_yields_a_passenger_name(fixtures) -> None:
    text = (fixtures / "email_aa_trip_confirmation.txt").read_text(encoding="utf-8")
    text = text.replace("[Hello Mr. Sam Kestrel!]", "[Hello Tobias Voss!]")

    flights = parse_airline_email(text, 2026)

    assert flights[0].hints["passenger_name"] == "Tobias Voss"
