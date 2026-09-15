"""Airline email layout parsing against the synthetic fixtures."""
import time
from datetime import datetime

import pytest

from clawflight.models import Observation, polling_callsign
from clawflight.monitor import Monitor
from clawflight.parse import (
    AIRLINE_EMAIL_CITY_TO_IATA,
    AIRLINE_NAME_TO_IATA,
    KNOWN_CARRIERS,
    parse_airline_email,
)
from clawflight.airports import default_airports
from clawflight.registry import Registry


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


def test_jetblue_generic_fixture_has_the_exact_departure_timestamp(fixtures) -> None:
    flight = _parse(fixtures, "email_jetblue_generic.txt")[0]

    assert flight.leg.sched_dep_iso == "2026-10-03T08:40:00-04:00"
    assert flight.leg.sched_arr_iso == "2026-10-03T11:55:00-07:00"


@pytest.mark.parametrize(
    ("fixture_name", "expected_departure"),
    [
        ("email_united_generic.txt", "2026-09-18T08:40:00-04:00"),
        ("email_jetblue_generic.txt", "2026-10-03T08:40:00-04:00"),
        ("email_southwest_generic.txt", "2026-11-07T08:40:00-07:00"),
        ("email_alaska_generic.txt", "2026-11-12T08:40:00-08:00"),
        ("email_spirit_generic.txt", "2026-11-19T08:40:00-05:00"),
        ("email_frontier_generic.txt", "2026-12-02T08:40:00-07:00"),
        ("email_british_airways_generic.txt", "2026-12-08T08:40:00-05:00"),
        ("email_air_france_generic.txt", "2026-12-14T08:40:00-05:00"),
        ("email_lufthansa_generic.txt", "2026-12-19T08:40:00-05:00"),
        ("email_emirates_generic.txt", "2026-12-27T08:40:00-05:00"),
    ],
)
def test_every_timed_generic_fixture_carries_its_departure_clock(
    fixtures, fixture_name, expected_departure
) -> None:
    flight = _parse(fixtures, fixture_name)[0]

    assert flight.leg.sched_dep_iso == expected_departure


@pytest.mark.parametrize(
    ("kind", "label"),
    [
        *[("departure", label + colon) for label in (
            "Departs", "Departing", "Departure", "Departure time",
            "Depart", "Dep", "Leaves",
        ) for colon in ("", ":")],
        *[("arrival", label + colon) for label in (
            "Arrives", "Arriving", "Arrival", "Arrival time",
            "Arrive", "Arr", "Reaches",
        ) for colon in ("", ":")],
    ],
)
def test_generic_email_accepts_every_time_label_with_or_without_colon(
    kind, label
) -> None:
    departure_line = "Departs: 8:40 AM"
    arrival_line = "Arrives: 11:55 AM"
    if kind == "departure":
        departure_line = "{} 8:40 AM".format(label)
    else:
        arrival_line = "{} 11:55 AM".format(label)
    text = "\n".join([
        "Confirmation: FAKEL1",
        "Travel Date: 2026-10-03",
        "Flight: JetBlue 611",
        "Route: BOS -> LAX",
        departure_line,
        arrival_line,
    ])

    flight = parse_airline_email(text, 2026)[0].leg

    assert flight.sched_dep_iso == "2026-10-03T08:40:00-04:00"
    assert flight.sched_arr_iso == "2026-10-03T11:55:00-07:00"


@pytest.mark.parametrize("departure_label", ["Departing:", "Dep:", "Departure:", "Leaves:"])
def test_generic_email_with_departure_label_reaches_watch(
    tmp_path, people, departure_label
) -> None:
    text = "\n".join([
        "Passenger: Alex Kestrel",
        "Confirmation: FAKEW1",
        "Travel Date: 2026-10-03",
        "Flight: JetBlue 611",
        "Route: BOS -> LAX",
        "{} 8:40 AM".format(departure_label),
        "Arrival: 11:55 AM",
    ])
    registry = Registry(str(tmp_path / "registry.json"), people)
    registry.merge(parse_airline_email(text, 2026))
    record = registry.get("B6611-2026-10-03")
    assert record is not None and record.leg.sched_dep_iso is not None
    departure = datetime.fromisoformat(record.leg.sched_dep_iso).timestamp()
    now = departure - 2 * 60 * 60
    monitor = Monitor(str(tmp_path / "monitor.json"))

    events = monitor.assess(
        record,
        Observation(record.flight_id, None, None, None, now),
        default_airports(),
        now,
    )

    assert [event.kind for event in events] == ["tracking_started"]
    assert monitor.state_snapshot()[record.flight_id]["phase"] == "watch"


def test_generic_email_named_passenger_is_consumed_by_registry(
    fixtures, tmp_path, people
) -> None:
    text = (fixtures / "email_jetblue_generic.txt").read_text(encoding="utf-8")
    text = text.replace("Juniper Wren", "Alex Kestrel")
    registry = Registry(str(tmp_path / "registry.json"), people)

    registry.merge(parse_airline_email(text, 2026))

    assert registry.get("B6611-2026-10-03").person.key == "alex"


def test_generic_email_attributes_every_passenger_on_the_record(tmp_path, people) -> None:
    text = "\n".join([
        "Passenger: Alex Kestrel",
        "Passenger: Sam Kestrel",
        "Confirmation: FAKEG2",
        "Travel Date: 2026-10-03",
        "Flight: JetBlue 611",
        "Route: BOS -> LAX",
        "Departs: 8:40 AM",
        "Arrives: 11:55 AM",
    ])

    registry = Registry(str(tmp_path / "registry.json"), people)

    registry.merge(parse_airline_email(text, 2026))

    assert {record.person.key for record in registry.all_records()} == {"alex", "sam"}


def test_generic_clock_with_unresolved_origin_stays_untimed_and_is_reported(caplog) -> None:
    text = "\n".join([
        "Passenger: Tobias Voss",
        "Confirmation: FAKEG3",
        "Travel Date: 2026-10-03",
        "Flight: JetBlue 611",
        "Route: ZZZ -> LAX",
        "Departs: 8:40 AM",
    ])
    caplog.set_level("WARNING", logger="clawflight.parse")

    flight = parse_airline_email(text, 2026)[0]

    assert flight.leg.origin is None
    assert flight.leg.sched_dep_iso is None
    assert flight.hints["unresolved_airports"] == ["ZZZ"]
    assert "ZZZ" in caplog.text


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


def test_operating_carrier_on_marketed_flight_supplies_the_polling_identity() -> None:
    text = "\n".join([
        "Confirmation: FAKEO1",
        "Date: 2026-10-10",
        "Flight: American Airlines 4912 operated by SkyWest Airlines as American Eagle",
        "Route: JFK -> LAX",
    ])

    flight = parse_airline_email(text, 2026)[0].leg

    assert (flight.operating_carrier, flight.operating_number) == ("OO", 4912)
    assert polling_callsign(flight) == "OO4912"


def test_operating_alias_with_flight_number_supplies_the_polling_identity() -> None:
    text = "\n".join([
        "Confirmation: FAKEO3",
        "Date: 2026-10-10",
        "Flight: United Airlines 9 operated by ANA as NH 0009",
        "Route: JFK -> LAX",
    ])

    flight = parse_airline_email(text, 2026)[0].leg

    assert (flight.operating_carrier, flight.operating_number) == ("NH", 9)
    assert polling_callsign(flight) == "NH9"


def test_schedule_change_operating_carrier_supplies_the_polling_identity(fixtures) -> None:
    text = (fixtures / "email_delta_schedule_change.txt").read_text(encoding="utf-8")
    text = text.replace(
        "Delta 365", "Delta 365 operated by SkyWest as Delta Connection", 1
    )

    flight = parse_airline_email(text, 2026)[0].leg

    assert (flight.operating_carrier, flight.operating_number) == ("OO", 365)
    assert polling_callsign(flight) == "OO365"


def test_separate_operating_carrier_line_uses_the_marketed_flight_number() -> None:
    text = "\n".join([
        "Confirmation: FAKEO4",
        "Date: 2026-10-10",
        "Flight: Delta 365",
        "Route: JFK -> LAX",
        "Operated by SkyWest as Delta Connection",
    ])

    flight = parse_airline_email(text, 2026)[0].leg

    assert (flight.operating_carrier, flight.operating_number) == ("OO", 365)
    assert polling_callsign(flight) == "OO365"


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


@pytest.mark.parametrize(
    ("month_token", "expected_dates"),
    [
        ("Jun", ["2026-06-11", "2026-06-14"]),
        ("June", ["2026-06-11", "2026-06-14"]),
    ],
)
def test_receipt_accepts_abbreviated_and_full_month_names(
    fixtures, month_token, expected_dates
) -> None:
    text = (fixtures / "email_delta_receipt.txt").read_text(encoding="utf-8")
    text = text.replace("MAY", month_token)

    flights = parse_airline_email(text, 2026)

    assert [flight.leg.date for flight in flights] == expected_dates
    assert flights[0].leg.sched_dep_iso == "2026-06-11T06:00:00-04:00"


@pytest.mark.parametrize("month_token", ["Feb", "February"])
def test_trip_confirmation_accepts_abbreviated_and_full_month_names(
    fixtures, month_token
) -> None:
    text = (fixtures / "email_aa_trip_confirmation.txt").read_text(encoding="utf-8")
    text = text.replace("February", month_token)

    flights = parse_airline_email(text, 2026)

    assert [flight.leg.date for flight in flights] == ["2026-02-27", "2026-02-28"]
    assert flights[0].leg.sched_dep_iso == "2026-02-27T07:45:00-05:00"


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


@pytest.mark.parametrize(
    ("month_token", "month_number"),
    [
        ("Jan", 1), ("January", 1), ("Feb", 2), ("February", 2),
        ("Mar", 3), ("March", 3), ("Apr", 4), ("April", 4),
        ("May", 5), ("May", 5), ("Jun", 6), ("June", 6),
        ("Jul", 7), ("July", 7), ("Aug", 8), ("August", 8),
        ("Sep", 9), ("September", 9), ("Oct", 10), ("October", 10),
        ("Nov", 11), ("November", 11), ("Dec", 12), ("December", 12),
        ("Sept", 9), ("Jun.", 6),
    ],
)
def test_schedule_change_accepts_the_month_matrix(
    fixtures, month_token, month_number
) -> None:
    text = (fixtures / "email_delta_schedule_change.txt").read_text(encoding="utf-8")
    text = text.replace("May", month_token)

    flights = parse_airline_email(text, 2026)

    assert len(flights) == 1
    assert flights[0].leg.date == "2026-{:02d}-21".format(month_number)


def test_schedule_change_rejects_a_non_month_token(fixtures) -> None:
    text = (fixtures / "email_delta_schedule_change.txt").read_text(encoding="utf-8")

    assert parse_airline_email(text.replace("May", "Tues"), 2026) == []


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


def test_receipt_passenger_names_are_consumed_by_registry(fixtures, tmp_path, people) -> None:
    text = (fixtures / "email_delta_receipt.txt").read_text(encoding="utf-8")
    text = text.replace(
        "Name: ROBIN J KESTREL",
        "Name: ALEX KESTREL\nName: SAM KESTREL",
    )
    registry = Registry(str(tmp_path / "registry.json"), people)

    registry.merge(parse_airline_email(text, 2026))

    by_flight = {}
    for record in registry.all_records():
        by_flight.setdefault((record.leg.number, record.leg.date), set()).add(record.person.key)
    assert by_flight == {
        (667, "2026-05-11"): {"alex", "sam"},
        (1226, "2026-05-14"): {"alex", "sam"},
    }


def test_untitled_trip_greeting_yields_a_passenger_name(fixtures) -> None:
    text = (fixtures / "email_aa_trip_confirmation.txt").read_text(encoding="utf-8")
    text = text.replace("[Hello Mr. Sam Kestrel!]", "[Hello Tobias Voss!]")

    flights = parse_airline_email(text, 2026)

    assert flights[0].hints["passenger_name"] == "Tobias Voss"
