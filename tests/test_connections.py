"""Connection intelligence: tight, missed, and comfortable connections."""
from __future__ import annotations

from typing import Optional

from clawflight.connections import connection_alerts
from clawflight.models import FlightLeg, FlightRecord, PersonRef


def _leg(
    carrier: str,
    number: int,
    origin: str,
    dest: str,
    dep: Optional[str],
    arr: Optional[str],
    conf: Optional[str],
) -> FlightLeg:
    return FlightLeg(
        carrier=carrier,
        number=number,
        date="2026-07-11",
        origin=origin,
        dest=dest,
        sched_dep_iso=dep,
        sched_arr_iso=arr,
        conf_code=conf,
        seat=None,
    )


def _record(flight_id: str, leg: FlightLeg, person_key: str = "alex") -> FlightRecord:
    return FlightRecord(
        flight_id=flight_id,
        leg=leg,
        person=PersonRef(key=person_key, name=person_key.title()),
        sources=("cal:cal-0001",),
        backup_group=None,
        status="scheduled",
        notes=(),
    )


def _pair(first_arr: str, second_dep: str, conf=("FAKE01", "FAKE01"), person=("alex", "alex")):
    first = _record(
        "AA4912-2026-07-11",
        _leg("AA", 4912, "ASE", "DFW", "2026-07-11T12:51:00-06:00", first_arr, conf[0]),
        person[0],
    )
    second = _record(
        "AA1203-2026-07-11",
        _leg("AA", 1203, "DFW", "JFK", second_dep, "2026-07-11T22:30:00-04:00", conf[1]),
        person[1],
    )
    return [first, second]


def test_a_missed_connection_is_critical() -> None:
    # Arriving DFW 5:00 PM, next flight departs DFW 4:30 PM.
    alerts = connection_alerts(
        _pair("2026-07-11T17:00:00-05:00", "2026-07-11T16:30:00-05:00"), {}, 1_000_000.0
    )

    assert len(alerts) == 1
    assert alerts[0].kind == "connection_alert"
    assert alerts[0].critical is True
    assert "Missed" in alerts[0].message
    assert alerts[0].flight_id == "AA4912-2026-07-11"


def test_a_very_tight_connection_is_critical() -> None:
    alerts = connection_alerts(
        _pair("2026-07-11T16:00:00-05:00", "2026-07-11T16:30:00-05:00"), {}, 1_000_000.0
    )

    assert len(alerts) == 1
    assert alerts[0].critical is True
    assert "30 min" in alerts[0].message


def test_a_tight_connection_is_informational() -> None:
    # 60 minutes: below the 75-minute comfort threshold, above the 45-minute floor.
    alerts = connection_alerts(
        _pair("2026-07-11T16:00:00-05:00", "2026-07-11T17:00:00-05:00"), {}, 1_000_000.0
    )

    assert len(alerts) == 1
    assert alerts[0].critical is False
    assert "60 min" in alerts[0].message


def test_a_comfortable_connection_is_silent() -> None:
    assert (
        connection_alerts(
            _pair("2026-07-11T16:00:00-05:00", "2026-07-11T18:00:00-05:00"),
            {},
            1_000_000.0,
        )
        == []
    )


def test_revised_times_from_monitor_state_win_over_the_schedule() -> None:
    # Scheduled gap is a comfortable 90 minutes.
    records = _pair("2026-07-11T16:00:00-05:00", "2026-07-11T17:30:00-05:00")
    state = {"AA4912-2026-07-11": {"arrival_revised": "2026-07-11T17:10:00-05:00"}}

    alerts = connection_alerts(records, state, 1_000_000.0)

    assert len(alerts) == 1
    assert alerts[0].critical is True
    assert "20 min" in alerts[0].message


def test_a_revised_departure_can_rescue_a_tight_connection() -> None:
    records = _pair("2026-07-11T16:00:00-05:00", "2026-07-11T16:30:00-05:00")
    state = {"AA1203-2026-07-11": {"departure_revised": "2026-07-11T18:00:00-05:00"}}

    assert connection_alerts(records, state, 1_000_000.0) == []


def test_legs_are_grouped_only_by_person_and_confirmation_code() -> None:
    # Different confirmation codes are separate bookings, not a connection.
    assert (
        connection_alerts(
            _pair(
                "2026-07-11T16:00:00-05:00",
                "2026-07-11T16:30:00-05:00",
                conf=("FAKE01", "FAKE02"),
            ),
            {},
            1_000_000.0,
        )
        == []
    )
    # Two travelers on one code are not each other's connection either.
    assert (
        connection_alerts(
            _pair(
                "2026-07-11T16:00:00-05:00",
                "2026-07-11T16:30:00-05:00",
                person=("alex", "sam"),
            ),
            {},
            1_000_000.0,
        )
        == []
    )


def test_legs_without_a_confirmation_code_are_not_grouped() -> None:
    assert (
        connection_alerts(
            _pair(
                "2026-07-11T16:00:00-05:00",
                "2026-07-11T16:30:00-05:00",
                conf=(None, None),
            ),
            {},
            1_000_000.0,
        )
        == []
    )


def test_non_connecting_airports_produce_no_alert() -> None:
    records = _pair("2026-07-11T16:00:00-05:00", "2026-07-11T16:30:00-05:00")
    records[1] = _record(
        "AA1203-2026-07-11",
        _leg(
            "AA", 1203, "ORD", "JFK",
            "2026-07-11T16:30:00-05:00", "2026-07-11T22:30:00-04:00", "FAKE01",
        ),
    )

    assert connection_alerts(records, {}, 1_000_000.0) == []


def test_missing_or_unparseable_times_produce_no_alert() -> None:
    assert (
        connection_alerts(
            _pair(None, "2026-07-11T16:30:00-05:00"), {}, 1_000_000.0
        )
        == []
    )
    assert (
        connection_alerts(
            _pair("not-a-time", "2026-07-11T16:30:00-05:00"), {}, 1_000_000.0
        )
        == []
    )


def test_a_single_leg_itinerary_is_never_a_connection() -> None:
    records = _pair("2026-07-11T16:00:00-05:00", "2026-07-11T16:30:00-05:00")

    assert connection_alerts(records[:1], {}, 1_000_000.0) == []
    assert connection_alerts([], {}, 1_000_000.0) == []


def test_utc_z_suffixed_times_are_understood() -> None:
    records = _pair("2026-07-11T21:00:00Z", "2026-07-11T21:30:00Z")

    alerts = connection_alerts(records, {}, 1_000_000.0)

    assert len(alerts) == 1
    assert "30 min" in alerts[0].message
