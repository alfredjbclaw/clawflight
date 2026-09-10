from dataclasses import FrozenInstanceError

import pytest

from clawflight.models import (
    AIRLINE_ICAO,
    EVENT_KINDS,
    MILESTONES,
    Airport,
    FlightEvent,
    FlightLeg,
    FlightRecord,
    Observation,
    PersonRef,
    Position,
    flight_ident,
    icao_callsign,
    polling_callsign,
)


def test_constants_match_the_event_contract() -> None:
    # Given: the engine's fixed event vocabulary.
    # When: the public constants are inspected.
    # Then: they retain the declared ordering and known airline mapping.
    assert MILESTONES == ("takeoff", "halfway", "landing")
    assert EVENT_KINDS == (
        "takeoff",
        "halfway",
        "landing",
        "delay",
        "gate_change",
        "schedule_change",
        "cancelled",
        "diverted",
        "stale_data",
        "backup_reminder",
        "tracking_started",
        "landing_hold",
        "push_stale",
        "trip_card",
        "arrival",
        "connection_alert",
    )
    assert AIRLINE_ICAO["AA"] == "AAL"
    assert AIRLINE_ICAO["EK"] == "UAE"


def test_models_hold_flight_tracking_data_when_constructed() -> None:
    # Given: values from a booked and observed flight.
    leg = FlightLeg(
        carrier="AA",
        number=4912,
        date="2026-07-11",
        origin="ASE",
        dest="DFW",
        sched_dep_iso="2026-07-11T12:51:00-06:00",
        sched_arr_iso="2026-07-11T16:10:00-05:00",
        conf_code="FAKE01",
        seat="12A",
    )
    person = PersonRef(key="alex", name="Alex")
    position = Position(
        lat=39.2232,
        lon=-106.8688,
        alt_ft=12000.0,
        gs_kt=340.0,
        vert_rate_fpm=500.0,
        ts_epoch=1_783_788_120.0,
    )

    # When: the related immutable model values are created.
    record = FlightRecord(
        flight_id="AA4912-2026-07-11",
        leg=leg,
        person=person,
        sources=("cal:cal-0001",),
        backup_group=None,
        status="scheduled",
        notes=("booking confirmed",),
    )
    observation = Observation(
        flight_id=record.flight_id,
        position=position,
        origin_delay=None,
        dest_delay=None,
        fetched_at_epoch=position.ts_epoch,
    )
    event = FlightEvent(
        flight_id=record.flight_id,
        kind="takeoff",
        message="AA4912 is airborne.",
        critical=True,
        at_epoch=position.ts_epoch,
    )

    # Then: values preserve the supplied fields and optional data.
    assert record.leg == leg
    assert record.person.name == "Alex"
    assert observation.position == position
    assert event.critical is True


def test_models_reject_mutation_when_frozen() -> None:
    # Given: an immutable airport model.
    airport = Airport(
        iata="ASE", name="Aspen-Pitkin County", lat=39.2232, lon=-106.8688, tz="America/Denver"
    )

    # When/Then: dataclass immutability rejects an attempted field replacement.
    with pytest.raises(FrozenInstanceError):
        airport.iata = "DFW"


def test_models_require_all_declared_fields_when_constructed() -> None:
    # Given: an incomplete attempt to create a flight leg.
    # When: a required contract field is omitted.
    # Then: construction fails instead of silently creating partial data.
    with pytest.raises(TypeError):
        FlightLeg("AA", 4912, "2026-07-11", "ASE", "DFW", None, None, None)


def test_icao_callsign_uses_known_prefix_and_passes_unknown_through() -> None:
    assert icao_callsign("AA", 4912) == "AAL4912"
    assert icao_callsign("ZZ", 12) == "ZZ12"


def test_flight_ident_joins_carrier_number_and_date() -> None:
    assert flight_ident("AA", 4912, "2026-07-11") == "AA4912-2026-07-11"


def test_polling_callsign_prefers_a_complete_operating_identity() -> None:
    # Given: a marketed flight operated by a regional partner.
    marketed = FlightLeg(
        "AA", 4912, "2026-07-11", "ASE", "DFW", None, None, None, None
    )
    operated = FlightLeg(
        "AA", 4912, "2026-07-11", "ASE", "DFW", None, None, None, None,
        operating_carrier="OO", operating_number=5678,
    )
    partial = FlightLeg(
        "AA", 4912, "2026-07-11", "ASE", "DFW", None, None, None, None,
        operating_carrier="OO", operating_number=None,
    )

    # When/Then: only a complete operating identity changes the ADS-B callsign.
    assert polling_callsign(marketed) == "AAL4912"
    assert polling_callsign(operated) == icao_callsign("OO", 5678) == "OO5678"
    assert polling_callsign(partial) == "AAL4912"
