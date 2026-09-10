"""Phase state machine, delay buckets, and push-update diffing."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import pytest

from clawflight.aerodatabox import normalize_notification
from clawflight.models import (
    Airport,
    FlightLeg,
    FlightRecord,
    FlightUpdate,
    Observation,
    PersonRef,
    Position,
)
from clawflight.monitor import Monitor, _announced_list, ingest_push
from clawflight.notify import compose_post


def _record(
    *,
    departure: Optional[str] = "2026-07-11T12:00:00+00:00",
    backup_group: Optional[str] = None,
    notes: Tuple[str, ...] = (),
    status: str = "scheduled",
) -> FlightRecord:
    return FlightRecord(
        flight_id="AA4912-2026-07-11",
        leg=FlightLeg(
            carrier="AA",
            number=4912,
            date="2026-07-11",
            origin="ASE",
            dest="DFW",
            sched_dep_iso=departure,
            sched_arr_iso="2026-07-11T16:10:00-05:00",
            conf_code="FAKE01",
            seat="12A",
        ),
        person=PersonRef(key="alex", name="Alex"),
        sources=("cal:cal-0001",),
        backup_group=backup_group,
        status=status,
        notes=notes,
    )


def _observation(
    record: FlightRecord,
    *,
    position: Optional[Position] = None,
    origin_delay: Optional[dict] = None,
    dest_delay: Optional[dict] = None,
    fetched_at: float,
) -> Observation:
    return Observation(
        flight_id=record.flight_id,
        position=position,
        origin_delay=origin_delay,
        dest_delay=dest_delay,
        fetched_at_epoch=fetched_at,
    )


def _airports() -> Dict[str, Airport]:
    # A synthetic 0/0 to 0/10 route keeps progress arithmetic obvious.
    return {
        "ASE": Airport("ASE", "Origin", 0.0, 0.0, "America/Denver"),
        "DFW": Airport("DFW", "Destination", 0.0, 10.0, "America/Chicago"),
    }


DEPARTURE = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc).timestamp()


# -- poll assessment --------------------------------------------------------


def test_milestones_persist_and_never_re_fire(tmp_path) -> None:
    # Given: a backup flight already halfway along its known route.
    record = _record(backup_group="bg-alex-2026-07-11-1")
    monitor = Monitor(str(tmp_path / "monitor.json"))
    now = DEPARTURE - 3600
    position = Position(0.0, 5.5, 24_000.0, 410.0, 600.0, now)

    events = monitor.assess(
        record, _observation(record, position=position, fetched_at=now), _airports(), now
    )

    assert [event.kind for event in events] == [
        "tracking_started",
        "backup_reminder",
        "takeoff",
        "halfway",
    ]
    assert all(len(event.message) < 200 for event in events)
    assert "ETA" in events[-1].message

    # When: a new Monitor reloads the persisted state for the same observation.
    repeated = Monitor(str(tmp_path / "monitor.json")).assess(
        record,
        _observation(record, position=position, fetched_at=now + 60),
        _airports(),
        now + 60,
    )

    assert repeated == []


def test_delays_and_schedule_changes_are_reported_once_each(tmp_path) -> None:
    # Given: a watched flight with an airline change notice and an FAA ground stop.
    record = _record()
    monitor = Monitor(str(tmp_path / "monitor.json"))
    enter_watch = DEPARTURE - (5 * 60 * 60)
    monitor.assess(
        record, _observation(record, fetched_at=enter_watch), _airports(), enter_watch
    )

    delayed_at = DEPARTURE + (46 * 60)
    changed = replace(
        record,
        notes=(
            "Schedule change: departure now 1:00 PM.",
            "Your flight changed: please review the updated itinerary.",
        ),
    )
    events = monitor.assess(
        changed,
        _observation(
            changed,
            origin_delay={"type": "ground_stop", "reason": "weather"},
            fetched_at=delayed_at,
        ),
        _airports(),
        delayed_at,
    )

    # No push has ever arrived for this flight, so nothing warns about a quiet
    # push feed: polling-only is the normal mode, not a degraded one.
    assert [event.kind for event in events] == ["delay", "schedule_change"]
    assert "45 minutes" in events[0].message
    assert "weather" in events[0].message
    assert events[1].critical is True

    # The same notice seen again, below a larger delay bucket, is suppressed.
    repeated = monitor.assess(
        changed,
        _observation(
            changed, origin_delay={"type": "ground_stop"}, fetched_at=delayed_at + 60
        ),
        _airports(),
        delayed_at + 60,
    )
    assert repeated == []

    # A distinct later notice is reported once, without repeating the first.
    later_change = replace(changed, notes=changed.notes + ("Your flight changed again.",))
    later = monitor.assess(
        later_change,
        _observation(
            later_change, origin_delay={"type": "ground_stop"}, fetched_at=delayed_at + 120
        ),
        _airports(),
        delayed_at + 120,
    )
    assert [event.kind for event in later] == ["schedule_change"]


def test_landing_from_destination_proximity_and_unknown_airports(tmp_path) -> None:
    # Given: an airborne flight first seen with no airport lookup data at all.
    record = _record()
    monitor = Monitor(str(tmp_path / "monitor.json"))
    takeoff_at = DEPARTURE - 3600
    monitor.assess(
        record,
        _observation(
            record, position=Position(0.0, 1.0, 20_000.0, 300.0, 500.0, takeoff_at),
            fetched_at=takeoff_at,
        ),
        {},
        takeoff_at,
    )

    landing_at = takeoff_at + 7_200
    events = monitor.assess(
        record,
        _observation(
            record, position=Position(0.0, 9.5, 2_000.0, 180.0, -500.0, landing_at),
            fetched_at=landing_at,
        ),
        _airports(),
        landing_at,
    )

    # The airport-free pass did not invent halfway; the known route now does.
    assert [event.kind for event in events] == ["halfway", "landing"]
    assert events[-1].critical is True


def test_stale_position_is_reported_once_while_airborne(tmp_path) -> None:
    record = _record()
    monitor = Monitor(str(tmp_path / "monitor.json"))
    airborne_at = DEPARTURE - 3600
    monitor.assess(
        record,
        _observation(
            record, position=Position(0.0, 1.0, 20_000.0, 300.0, 500.0, airborne_at),
            fetched_at=airborne_at,
        ),
        _airports(),
        airborne_at,
    )

    stale_at = airborne_at + 901
    events = monitor.assess(
        record, _observation(record, fetched_at=stale_at), _airports(), stale_at
    )

    assert [event.kind for event in events] == ["stale_data"]
    assert events[0].critical is False
    assert (
        monitor.assess(
            record, _observation(record, fetched_at=stale_at + 60), _airports(), stale_at + 60
        )
        == []
    )


def test_landing_is_inferred_when_a_near_complete_track_disappears(tmp_path) -> None:
    # Given: an airborne flight whose last position was >85% of the way there.
    record = _record()
    monitor = Monitor(str(tmp_path / "monitor.json"))
    airborne_at = DEPARTURE - 3600
    monitor.assess(
        record,
        _observation(
            record, position=Position(0.0, 9.0, 20_000.0, 300.0, 500.0, airborne_at),
            fetched_at=airborne_at,
        ),
        _airports(),
        airborne_at,
    )

    events = monitor.assess(
        record,
        _observation(record, fetched_at=airborne_at + 901),
        _airports(),
        airborne_at + 901,
    )

    assert [event.kind for event in events] == ["landing"]


def test_an_invalid_ground_position_is_not_a_takeoff(tmp_path) -> None:
    record = _record()
    monitor = Monitor(str(tmp_path / "monitor.json"))
    now = DEPARTURE - 3600

    events = monitor.assess(
        record,
        _observation(record, position=Position(0.0, 0.0, None, None, None, now), fetched_at=now),
        _airports(),
        now,
    )

    assert [event.kind for event in events] == ["tracking_started"]


def test_no_milestone_fires_before_the_watch_window_opens(tmp_path) -> None:
    # Given: a departure seven hours away, outside the six-hour watch window.
    record = _record()
    monitor = Monitor(str(tmp_path / "monitor.json"))
    early_at = DEPARTURE - (7 * 60 * 60)
    position = Position(0.0, 1.0, 20_000.0, 300.0, 500.0, early_at)

    early = monitor.assess(
        record, _observation(record, position=position, fetched_at=early_at), _airports(), early_at
    )
    in_watch_at = DEPARTURE - (5 * 60 * 60)
    events = monitor.assess(
        record,
        _observation(
            record, position=replace(position, ts_epoch=in_watch_at), fetched_at=in_watch_at
        ),
        _airports(),
        in_watch_at,
    )

    assert early == []
    assert [event.kind for event in events] == ["tracking_started", "takeoff"]


def test_long_messages_are_bounded_to_one_sentence(tmp_path) -> None:
    record = replace(_record(), person=PersonRef("alex", "Alex " * 50))
    monitor = Monitor(str(tmp_path / "monitor.json"))
    now = DEPARTURE - (5 * 60 * 60)

    events = monitor.assess(
        record,
        _observation(
            record, position=Position(0.0, 1.0, 20_000.0, 300.0, 500.0, now), fetched_at=now
        ),
        _airports(),
        now,
    )

    assert events
    assert all(len(event.message) <= 200 and event.message.endswith(".") for event in events)


def test_terminal_statuses_stop_assessment(tmp_path) -> None:
    monitor = Monitor(str(tmp_path / "monitor.json"))
    now = DEPARTURE - 3600

    for status in ("done", "cancelled"):
        record = _record(status=status)
        assert (
            monitor.assess(record, _observation(record, fetched_at=now), _airports(), now)
            == []
        )


def test_corrupt_or_missing_state_recovers_instead_of_crashing(tmp_path) -> None:
    # Given: a state file containing malformed non-UTF-8 data.
    path = tmp_path / "monitor.json"
    path.write_bytes(b"\xff")
    record = _record()
    now = DEPARTURE - 3600

    events = Monitor(str(path)).assess(
        record, _observation(record, fetched_at=now), _airports(), now
    )

    assert [event.kind for event in events] == ["tracking_started"]


def test_a_missing_state_directory_is_created_and_z_times_parse(tmp_path) -> None:
    path = tmp_path / "new" / "monitor.json"
    record = _record(departure="2026-07-11T12:00:00Z")
    now = DEPARTURE - 3600

    events = Monitor(str(path)).assess(
        record, _observation(record, fetched_at=now), _airports(), now
    )

    assert path.exists()
    assert [event.kind for event in events] == ["tracking_started"]


def test_a_naive_departure_time_never_enters_the_watch_window(tmp_path) -> None:
    # A time without an offset is ambiguous; refusing it beats guessing a zone.
    record = _record(departure="2026-07-11T12:00:00")
    monitor = Monitor(str(tmp_path / "monitor.json"))

    assert (
        monitor.assess(record, _observation(record, fetched_at=DEPARTURE), _airports(), DEPARTURE)
        == []
    )


def test_poll_cadence_matches_the_time_to_departure(tmp_path) -> None:
    monitor = Monitor(str(tmp_path / "monitor.json"))

    assert [
        monitor.recommend_poll_seconds(_record(status="done"), DEPARTURE),
        monitor.recommend_poll_seconds(_record(status="cancelled"), DEPARTURE),
        monitor.recommend_poll_seconds(_record(), DEPARTURE - (25 * 60 * 60)),
        monitor.recommend_poll_seconds(_record(), DEPARTURE - (7 * 60 * 60)),
        monitor.recommend_poll_seconds(_record(), DEPARTURE - (2 * 60 * 60)),
        monitor.recommend_poll_seconds(_record(), DEPARTURE - (60 * 60)),
        monitor.recommend_poll_seconds(_record(), DEPARTURE - 300),
        monitor.recommend_poll_seconds(_record(departure=None), DEPARTURE),
    ] == [0, 0, 3600, 1800, 900, 900, 300, 900]


def test_an_airborne_flight_polls_every_five_minutes(tmp_path) -> None:
    record = _record()
    monitor = Monitor(str(tmp_path / "monitor.json"))
    airborne_at = DEPARTURE - 3600
    monitor.assess(
        record,
        _observation(
            record, position=Position(0.0, 1.0, 20_000.0, 300.0, 500.0, airborne_at),
            fetched_at=airborne_at,
        ),
        _airports(),
        airborne_at,
    )

    assert monitor.recommend_poll_seconds(record, airborne_at) == 300


# -- delay corroboration ----------------------------------------------------


def test_elapsed_time_alone_never_raises_a_delay(tmp_path) -> None:
    # Given: a regional/codeshare callsign we can never match on ADS-B.
    monitor = Monitor(str(tmp_path / "monitor.json"))
    record = _record()
    enter_watch = DEPARTURE - (5 * 60 * 60)
    monitor.assess(
        record, _observation(record, fetched_at=enter_watch), _airports(), enter_watch
    )

    elapsed_at = DEPARTURE + (50 * 60)
    events = monitor.assess(
        record, _observation(record, fetched_at=elapsed_at), _airports(), elapsed_at
    )

    # Then: one informational no-position note instead of a false delay.
    kinds = [event.kind for event in events]
    assert "delay" not in kinds
    assert "stale_data" in kinds
    assert next(e for e in events if e.kind == "stale_data").critical is False

    later = monitor.assess(
        record, _observation(record, fetched_at=elapsed_at + 600), _airports(), elapsed_at + 600
    )
    assert "stale_data" not in [event.kind for event in later]


def test_a_push_revision_corroborates_an_elapsed_time_delay(tmp_path) -> None:
    monitor = Monitor(str(tmp_path / "monitor.json"))
    record = _record()
    enter_watch = DEPARTURE - (5 * 60 * 60)
    monitor.assess(
        record, _observation(record, fetched_at=enter_watch), _airports(), enter_watch
    )
    monitor.ingest_push(
        normalize_notification(
            {
                "flight": {
                    "number": "AA4912",
                    "departure": {
                        "scheduledTime": {"local": "2026-07-11T12:00:00+00:00"},
                        "revisedTime": {"local": "2026-07-11T12:50:00+00:00"},
                    },
                }
            }
        )[0],
        enter_watch,
    )

    elapsed_at = DEPARTURE + (50 * 60)
    events = monitor.assess(
        record, _observation(record, fetched_at=elapsed_at), _airports(), elapsed_at
    )

    assert "delay" in [event.kind for event in events]


def test_an_arrival_ground_stop_warns_of_a_landing_hold_once(tmp_path) -> None:
    record = _record()
    monitor = Monitor(str(tmp_path / "monitor.json"))
    airborne_at = DEPARTURE - 3600
    position = Position(0.0, 1.0, 20_000.0, 300.0, 500.0, airborne_at)
    monitor.assess(
        record, _observation(record, position=position, fetched_at=airborne_at),
        _airports(), airborne_at,
    )
    monitor.ingest_push(
        normalize_notification({"flight": {"number": "AA4912", "status": "Scheduled"}})[0],
        airborne_at,
    )

    events = monitor.assess(
        record,
        _observation(
            record,
            position=replace(position, ts_epoch=airborne_at + 1801),
            dest_delay={"type": "ground_stop", "reason": "weather"},
            fetched_at=airborne_at + 1801,
        ),
        _airports(),
        airborne_at + 1801,
    )

    assert [event.kind for event in events] == ["landing_hold", "push_stale"]
    assert events[0].critical is True
    assert events[1].critical is False


# -- push diffing -----------------------------------------------------------


def test_push_diffs_revisions_gate_and_status_idempotently() -> None:
    update = normalize_notification(
        {
            "flight": {
                "number": "AA4912",
                "status": "Delayed",
                "departure": {
                    "scheduledTime": {"local": "2026-07-11T12:00:00-05:00"},
                    "revisedTime": {"local": "2026-07-11T12:30:00-05:00"},
                    "gate": "B12",
                },
            }
        }
    )[0]
    state = {
        "flight_id": "AA4912-2026-07-11",
        "flight_number": "AA4912",
        "status": "Scheduled",
        "departure_scheduled": "2026-07-11T12:00:00-05:00",
        "departure_revised": None,
        "departure_gate": "A1",
        "_now_epoch": 100.0,
    }

    first = ingest_push(update, state)
    second = ingest_push(update, state)

    assert [event.kind for event in first] == ["delay", "schedule_change", "gate_change"]
    assert second == []
    assert state["last_push_epoch"] == 100.0


def test_push_cancellation_and_diversion_alert_once_each() -> None:
    cancelled = normalize_notification(
        {"flight": {"number": "DL4133", "status": "Cancelled"}}
    )[0]
    diverted = normalize_notification(
        {"flight": {"number": "DL4133", "status": "Diverted"}}
    )[0]
    state = {"flight_id": "DL4133-2026-07-11", "_now_epoch": 100.0}

    first = ingest_push(cancelled, state)
    repeat = ingest_push(cancelled, state)
    second = ingest_push(diverted, state)

    assert [event.kind for event in first] == ["cancelled"]
    assert state["phase"] == "cancelled"
    assert repeat == []
    assert [event.kind for event in second] == ["diverted"]


def _departure_update(scheduled: str, revised: str, status: str = "Scheduled") -> FlightUpdate:
    return FlightUpdate(
        "DL1", status, scheduled, revised, None, None, None, None, None, None
    )


def test_a_later_revision_alerts_normally_in_any_phase(caplog) -> None:
    update = _departure_update("2026-07-28T20:00:00-04:00", "2026-07-28T20:00:30-04:00")
    now = datetime.fromisoformat("2026-07-28T19:00:00-04:00").timestamp()

    with caplog.at_level("WARNING", logger="clawflight.monitor"):
        predeparture = ingest_push(
            update, {"flight_id": "DL1-2026-07-28", "phase": "watch", "_now_epoch": now}
        )
        postdeparture = ingest_push(
            update, {"flight_id": "DL1-2026-07-28", "phase": "airborne", "_now_epoch": now}
        )

    assert [event.kind for event in predeparture] == ["schedule_change"]
    assert [event.kind for event in postdeparture] == ["schedule_change"]
    assert all("30 seconds" in event.message for event in predeparture + postdeparture)
    assert all(
        "unconfirmed" not in event.message.casefold()
        for event in predeparture + postdeparture
    )
    assert not [r for r in caplog.records if r.getMessage() == "data_anomaly"]


def test_a_revision_already_in_the_past_is_suppressed_and_logged_once(caplog) -> None:
    update = _departure_update("2026-07-28T20:15:00-04:00", "2026-07-28T17:35:00-04:00")
    state = {
        "flight_id": "DL1-2026-07-28",
        "phase": "watch",
        "_now_epoch": datetime.fromisoformat("2026-07-28T19:14:00-04:00").timestamp(),
    }

    with caplog.at_level("WARNING", logger="clawflight.monitor"):
        first = ingest_push(update, state)
        second = ingest_push(update, state)

    records = [r for r in caplog.records if r.getMessage() == "data_anomaly"]
    assert first == [] and second == []
    assert state["departure_revised"] == "2026-07-28T17:35:00-04:00"
    assert state["data_anomaly_revisions"] == ["2026-07-28T17:35:00-04:00"]
    assert len(records) == 1
    assert (
        records[0].flight_id,
        records[0].revised,
        records[0].sched,
        records[0].reason,
    ) == (
        "DL1-2026-07-28",
        "2026-07-28T17:35:00-04:00",
        "2026-07-28T20:15:00-04:00",
        "past_revision",
    )


def test_an_implausibly_early_revision_is_hedged_not_asserted(caplog) -> None:
    scheduled = "2026-07-28T22:00:00-04:00"
    revised = "2026-07-28T20:30:00-04:00"
    state = {
        "flight_id": "DL1-2026-07-28",
        "phase": "watch",
        "_now_epoch": datetime.fromisoformat("2026-07-28T19:00:00-04:00").timestamp(),
    }

    with caplog.at_level("WARNING", logger="clawflight.monitor"):
        events = ingest_push(_departure_update(scheduled, revised), state)
        repeated = ingest_push(_departure_update(scheduled, revised), state)

    assert [event.kind for event in events] == ["schedule_change"]
    assert repeated == []
    assert "airline reports DL1 departing 8:30 PM" in events[0].message
    assert "scheduled 10:00 PM" in events[0].message
    assert "unconfirmed" in events[0].message.casefold()

    record = FlightRecord(
        flight_id="DL1-2026-07-28",
        leg=FlightLeg("DL", 1, "2026-07-28", "JFK", "SFO", scheduled, None, None, None),
        person=PersonRef("alex", "Alex"),
        sources=(),
        backup_group=None,
        status="scheduled",
        notes=(),
    )
    post = compose_post(events[0], record)
    assert "https://www.flightaware.com/live/flight/DAL1" in post
    assert "https://www.flightradar24.com/data/flights/dl1" in post
    anomalies = [r for r in caplog.records if r.getMessage() == "data_anomaly"]
    assert len(anomalies) == 1 and anomalies[0].reason == "early_revision"


def test_a_small_early_revision_alerts_without_hedging(caplog) -> None:
    state = {
        "flight_id": "DL1-2026-07-28",
        "phase": "watch",
        "_now_epoch": datetime.fromisoformat("2026-07-28T19:00:00-04:00").timestamp(),
    }

    with caplog.at_level("WARNING", logger="clawflight.monitor"):
        events = ingest_push(
            _departure_update("2026-07-28T22:00:00-04:00", "2026-07-28T21:55:00-04:00"), state
        )

    assert [event.kind for event in events] == ["schedule_change"]
    assert "unconfirmed" not in events[0].message.casefold()
    assert not [r for r in caplog.records if r.getMessage() == "data_anomaly"]


def test_a_post_departure_revision_is_never_hedged(caplog) -> None:
    state = {
        "flight_id": "DL1-2026-07-28",
        "phase": "airborne",
        "_now_epoch": datetime.fromisoformat("2026-07-28T21:00:00-04:00").timestamp(),
    }

    with caplog.at_level("WARNING", logger="clawflight.monitor"):
        events = ingest_push(
            _departure_update(
                "2026-07-28T22:00:00-04:00", "2026-07-28T20:00:00-04:00", status="Departed"
            ),
            state,
        )

    assert [event.kind for event in events] == ["schedule_change"]
    assert "unconfirmed" not in events[0].message.casefold()
    assert not [r for r in caplog.records if r.getMessage() == "data_anomaly"]


def test_an_already_announced_revision_emits_nothing(caplog) -> None:
    revised = "2026-07-28T20:30:00-04:00"
    state = {
        "flight_id": "DL1-2026-07-28",
        "phase": "watch",
        "departure_revised": revised,
        "announced_dep_revisions": [revised],
        "_now_epoch": datetime.fromisoformat("2026-07-28T19:00:00-04:00").timestamp(),
    }

    with caplog.at_level("WARNING", logger="clawflight.monitor"):
        events = ingest_push(_departure_update("2026-07-28T22:00:00-04:00", revised), state)

    assert events == []
    assert not [r for r in caplog.records if r.getMessage() == "data_anomaly"]


def test_a_schedule_flip_back_does_not_re_alert(tmp_path) -> None:
    # Given: a departure revised to B, back to A, then forward to B again.
    monitor = Monitor(str(tmp_path / "monitor.json"))
    base = {"number": "AA4912", "departure": {"scheduledTime": {"local": "2026-07-11T12:00:00-05:00"}}}

    def _revision(local: str) -> FlightUpdate:
        return normalize_notification(
            {
                "flight": {
                    **base,
                    "departure": {**base["departure"], "revisedTime": {"local": local}},
                }
            }
        )[0]

    first = monitor.ingest_push(_revision("2026-07-11T12:30:00-05:00"), 100.0)
    flip_back = monitor.ingest_push(_revision("2026-07-11T12:00:00-05:00"), 100.0)
    flip_forward = monitor.ingest_push(_revision("2026-07-11T12:30:00-05:00"), 100.0)

    assert "schedule_change" in [event.kind for event in first]
    assert flip_back == []
    assert flip_forward == []


def test_saved_schedules_back_a_sparse_revision() -> None:
    departure = normalize_notification(
        {"flight": {"number": "AA4912", "departure": {"revisedTime": {"local": "2026-07-11T12:30:00-05:00"}}}}
    )[0]
    arrival = normalize_notification(
        {"flight": {"number": "AA4912", "arrival": {"revisedTime": {"local": "2026-07-11T17:00:00-05:00"}}}}
    )[0]
    state = {
        "flight_id": "AA4912-2026-07-11",
        "departure_scheduled": "2026-07-11T12:00:00-05:00",
        "arrival_scheduled": "2026-07-11T16:30:00-05:00",
        "_now_epoch": 1.0,
    }

    first = ingest_push(departure, state)
    second = ingest_push(arrival, state)
    small = normalize_notification(
        {"flight": {"number": "AA4912", "departure": {"revisedTime": {"local": "2026-07-11T12:05:00-05:00"}}}}
    )[0]
    low_delay = ingest_push(
        small, {"departure_scheduled": "2026-07-11T12:00:00-05:00", "_now_epoch": 1.0}
    )

    assert [event.kind for event in first] == ["delay", "schedule_change"]
    assert [event.kind for event in second] == ["schedule_change"]
    # A five-minute slip is a schedule change but not yet a delay bucket.
    assert [event.kind for event in low_delay] == ["schedule_change"]


def test_alerts_state_the_new_time_in_local_and_reference_zones(tmp_path) -> None:
    monitor = Monitor(str(tmp_path / "monitor.json"))
    record = _record()
    now = DEPARTURE - 3600
    monitor.assess(record, _observation(record, fetched_at=now), _airports(), now)
    update = normalize_notification(
        {
            "flight": {
                "number": "AA4912",
                "departure": {
                    "scheduledTime": {"local": "2026-07-11T12:00:00-06:00"},
                    "revisedTime": {"local": "2026-07-11T12:45:00-06:00"},
                },
            }
        }
    )[0]

    events = monitor.ingest_push(update, now)

    delay = next(event for event in events if event.kind == "delay")
    assert "New departure 12:45 (ASE local) / 14:45 ET" in delay.message


def test_an_undated_push_attaches_to_the_most_recent_matching_state(tmp_path) -> None:
    monitor = Monitor(str(tmp_path / "monitor.json"))
    for day, epoch in (("2026-07-10", 100.0), ("2026-07-11", 500.0)):
        monitor.ingest_push(
            normalize_notification(
                {
                    "flight": {
                        "number": "AA4912",
                        "departure": {"scheduledTime": {"local": "{}T12:00:00-05:00".format(day)}},
                    }
                }
            )[0],
            epoch,
        )

    monitor.ingest_push(
        normalize_notification({"flight": {"number": "AA4912", "status": "Boarding"}})[0], 600.0
    )

    state = json.loads((tmp_path / "monitor.json").read_text())
    assert "AA4912" not in state
    assert state["AA4912-2026-07-11"]["status"] == "Boarding"


def test_a_dated_push_never_falls_back_to_another_date(tmp_path) -> None:
    monitor = Monitor(str(tmp_path / "monitor.json"))
    record = _record()
    now = DEPARTURE - 3600
    monitor.assess(record, _observation(record, fetched_at=now), _airports(), now)
    update = normalize_notification(
        {
            "flight": {
                "number": "AA4912",
                "departure": {
                    "scheduledTime": {"local": "2026-07-12T12:00:00-05:00"},
                    "revisedTime": {"local": "2026-07-12T12:30:00-05:00"},
                },
            }
        }
    )[0]

    events = monitor.ingest_push(update, now)

    assert {event.flight_id for event in events} == {"AA4912-2026-07-12"}


def test_a_push_received_before_the_first_poll_stays_attached(tmp_path) -> None:
    record = _record()
    monitor = Monitor(str(tmp_path / "monitor.json"))
    poll_at = DEPARTURE - 3600
    monitor.ingest_push(
        normalize_notification({"flight": {"number": "AA4912", "status": "Scheduled"}})[0],
        poll_at - 1801,
    )

    events = monitor.assess(
        record,
        _observation(
            record, position=Position(0.0, 1.0, 20_000.0, 300.0, 500.0, poll_at), fetched_at=poll_at
        ),
        _airports(),
        poll_at,
    )

    assert "push_stale" in [event.kind for event in events]


def test_one_update_fans_out_to_every_booking_on_that_flight(tmp_path) -> None:
    monitor = Monitor(str(tmp_path / "monitor.json"))
    first = _record()
    second = replace(first, flight_id="AA4912-2026-07-11#FAKE02")
    update = normalize_notification(
        {
            "flight": {
                "number": "AA4912",
                "status": "Delayed",
                "departure": {
                    "scheduledTime": {"local": "2026-07-11T12:00:00-05:00"},
                    "revisedTime": {"local": "2026-07-11T13:00:00-05:00"},
                },
            }
        }
    )[0]

    events = monitor.ingest_push_for_bookings(update, [first, second], 100.0)

    # The physical update is diffed once; both bookings receive every event.
    assert {event.flight_id for event in events} == {
        "AA4912-2026-07-11",
        "AA4912-2026-07-11#FAKE02",
    }
    assert len(events) == 2 * len({event.kind for event in events})
    assert monitor.ingest_push_for_bookings(update, [], 100.0) == []


def test_fanning_out_across_different_flight_instances_is_rejected(tmp_path) -> None:
    monitor = Monitor(str(tmp_path / "monitor.json"))
    first = _record()
    other = replace(
        _record(), flight_id="AA4912-2026-07-12",
        leg=replace(_record().leg, date="2026-07-12"),
    )
    update = normalize_notification({"flight": {"number": "AA4912", "status": "Delayed"}})[0]

    with pytest.raises(ValueError):
        monitor.ingest_push_for_bookings(update, [first, other], 100.0)


# -- persistence and housekeeping -------------------------------------------


def test_concurrent_writers_do_not_clobber_each_other(tmp_path) -> None:
    # Given: two Monitors on one file, as a receiver and a tick would be.
    path = str(tmp_path / "monitor.json")
    record = _record()
    now = DEPARTURE - 3600
    receiver = Monitor(path)
    ticker = Monitor(path)

    receiver.ingest_push(
        normalize_notification(
            {"flight": {"number": "AA4912", "status": "Boarding", "departure": {"gate": "B7"}}}
        )[0],
        now,
    )
    ticker.assess(record, _observation(record, fetched_at=now), _airports(), now)

    state = json.loads((tmp_path / "monitor.json").read_text())["AA4912-2026-07-11"]
    assert state["departure_gate"] == "B7"
    assert state["status"] == "Boarding"


def test_baggage_belt_is_persisted_without_alerting(tmp_path) -> None:
    path = tmp_path / "monitor.json"
    monitor = Monitor(str(path))
    first = normalize_notification(
        {
            "flight": {
                "number": "AA4912",
                "departure": {"scheduledTime": {"local": "2026-07-11T12:00:00-05:00"}},
                "arrival": {"baggageBelt": "7"},
            }
        }
    )[0]
    changed = replace(first, arrival_baggage_belt="9")

    assert monitor.ingest_push(first, 100.0) == []
    assert monitor.ingest_push(changed, 200.0) == []
    assert Monitor(str(path)).arrival_baggage_belt("AA4912-2026-07-11") == "9"
    assert json.loads(path.read_text())["AA4912-2026-07-11"]["arrival_baggage_belt"] == "9"


def test_baggage_belt_queries_tolerate_state_without_the_field(tmp_path) -> None:
    path = tmp_path / "monitor.json"
    path.write_text(json.dumps({"AA4912-2026-07-11": {"status": "Landed"}}))

    monitor = Monitor(str(path))

    assert monitor.arrival_baggage_belt("AA4912-2026-07-11") is None
    assert monitor.arrival_baggage_belt("AA4912-2026-07-12") is None


def test_unknown_persisted_keys_do_not_break_a_later_push(tmp_path) -> None:
    # Given: state written by a future or older version carrying extra keys.
    path = tmp_path / "monitor.json"
    revised = "2026-07-28T20:30:00-04:00"
    path.write_text(
        json.dumps(
            {
                "DL1-2026-07-28": {
                    "status": "Scheduled",
                    "departure_revised": revised,
                    "some_unknown_field": {"nested": True},
                    "data_anomaly_revisions": [revised, 17],
                }
            }
        )
    )
    monitor = Monitor(str(path))
    now = datetime.fromisoformat("2026-07-28T19:00:00-04:00").timestamp()

    events = monitor.ingest_push(
        _departure_update("2026-07-28T22:00:00-04:00", revised), now
    )

    assert [event.kind for event in events] == ["schedule_change"]
    assert "unconfirmed" in events[0].message.casefold()


def test_landed_flights_become_promotable_after_the_grace_period(tmp_path) -> None:
    monitor = Monitor(str(tmp_path / "monitor.json"))
    record = _record()
    airborne_at = DEPARTURE - 3600
    monitor.assess(
        record,
        _observation(
            record, position=Position(0.0, 9.0, 20_000.0, 300.0, 500.0, airborne_at),
            fetched_at=airborne_at,
        ),
        _airports(),
        airborne_at,
    )
    landed_at = airborne_at + 901
    monitor.assess(record, _observation(record, fetched_at=landed_at), _airports(), landed_at)

    assert monitor.landed_awaiting_done(landed_at + 1000) == []
    assert monitor.landed_awaiting_done(landed_at + 1800) == ["AA4912-2026-07-11"]


def test_prune_drops_orphaned_state_absent_from_the_registry(tmp_path) -> None:
    monitor = Monitor(str(tmp_path / "monitor.json"))
    record = _record()
    now = DEPARTURE - 3600
    monitor.assess(record, _observation(record, fetched_at=now), _airports(), now)

    kept = monitor.prune({"AA4912-2026-07-11"}, now + 49 * 3600)
    removed = monitor.prune(set(), now + 49 * 3600)

    assert kept == []
    assert removed == ["AA4912-2026-07-11"]
    assert json.loads((tmp_path / "monitor.json").read_text()) == {}


def test_state_snapshot_is_a_copy(tmp_path) -> None:
    monitor = Monitor(str(tmp_path / "monitor.json"))
    record = _record()
    now = DEPARTURE - 3600
    monitor.assess(record, _observation(record, fetched_at=now), _airports(), now)

    snapshot = monitor.state_snapshot()
    snapshot["AA4912-2026-07-11"]["phase"] = "tampered"

    assert monitor.state_snapshot()["AA4912-2026-07-11"]["phase"] == "watch"


def test_announced_list_preserves_lists_and_normalizes_other_values() -> None:
    original = ["first", 2]
    state = {"list": original, "tuple": ("first", 2, "second"), "garbage": "first"}

    assert _announced_list(state, "list") is original
    assert _announced_list(state, "tuple") == ["first", "second"]
    assert state["tuple"] == ["first", "second"]
    assert _announced_list(state, "garbage") == []
    assert state["garbage"] == []
