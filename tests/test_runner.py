"""One monitoring pass: ordering, error isolation, outbox routing, lifecycle."""
from __future__ import annotations

from dataclasses import replace
from typing import Dict, List

from clawflight.models import Airport, FlightEvent, FlightRecord, Observation
from clawflight.notify import DeliveryOutbox, FakePoster
from clawflight.runner import LANDING_TO_DONE_SECONDS, _LANDING_EPOCHS, run_once

from conftest import make_leg, make_record


NOW = 1_700_000_000.0


def _observation(record: FlightRecord) -> Observation:
    return Observation(
        flight_id=record.flight_id,
        position=None,
        origin_delay=None,
        dest_delay=None,
        fetched_at_epoch=NOW,
    )


class _Registry:
    def __init__(self, records: List[FlightRecord]) -> None:
        self.records = records
        self.statuses: List[tuple] = []

    def upcoming(self, now_epoch: float, horizon_days: int) -> List[FlightRecord]:
        return self.records

    def set_status(self, flight_id: str, status: str) -> None:
        self.statuses.append((flight_id, status))


class _Monitor:
    _state: Dict[str, dict] = {}

    def __init__(self, events: List[FlightEvent], poll_seconds: int = 300) -> None:
        self.events = events
        self.poll_seconds = poll_seconds

    def assess(self, record, observation, airports: Dict[str, Airport], now_epoch: float):
        return self.events

    def recommend_poll_seconds(self, record, now_epoch: float) -> int:
        return self.poll_seconds

    def state_snapshot(self) -> Dict[str, dict]:
        return dict(self._state)


def _clear_landing_state() -> None:
    _LANDING_EPOCHS.clear()


def test_every_event_reaches_the_poster_critical_first() -> None:
    # Given: one tracked flight producing an informational and a critical event.
    record = make_record()
    events = [
        FlightEvent(record.flight_id, "tracking_started", "Tracking started.", False, NOW),
        FlightEvent(record.flight_id, "delay", "AA4912 is delayed.", True, NOW),
    ]
    poster = FakePoster()

    report = run_once(
        _observation, _Registry([record]), _Monitor(events), {}, poster, now_epoch=NOW
    )

    # Then: critical events are posted before informational ones.
    assert report["checked"] == 1
    assert [event["kind"] for event in report["events"]] == ["delay", "tracking_started"]
    assert report["posts"] == 2
    assert report["errors"] == []
    assert report["next_poll_seconds"] == 300
    assert len(poster.calls) == 2


def test_a_fetch_error_is_isolated_to_its_flight() -> None:
    first = make_record()
    second = replace(first, flight_id="AA1203-2026-07-11")

    def fetcher(record: FlightRecord) -> Observation:
        if record.flight_id == first.flight_id:
            raise RuntimeError("feed unavailable")
        return _observation(record)

    report = run_once(
        fetcher, _Registry([first, second]), _Monitor([]), {}, FakePoster(), NOW
    )

    assert report["checked"] == 2
    assert report["events"] == []
    assert len(report["errors"]) == 1
    assert first.flight_id in report["errors"][0]
    assert report["next_poll_seconds"] == 300


def test_the_arrival_ground_callback_runs_only_at_landing() -> None:
    record = make_record()
    poster = FakePoster()
    seen = []

    run_once(
        _observation,
        _Registry([record]),
        _Monitor([FlightEvent(record.flight_id, "landing", "AA4912 landed.", True, NOW)]),
        {},
        poster,
        now_epoch=NOW,
        arrival_ground_info=lambda record: seen.append(record) or "Baggage claim 4",
    )
    _clear_landing_state()

    assert "Ground: Baggage claim 4" in poster.calls[0]
    assert len(seen) == 1


def test_a_landing_is_persisted_then_promoted_after_the_grace_period() -> None:
    _clear_landing_state()
    landing_record = make_record()
    landed_record = replace(
        landing_record,
        status="landed",
        leg=make_leg(sched_arr_iso="1970-01-01T00:00:00+00:00"),
    )
    landing = FlightEvent(landing_record.flight_id, "landing", "AA4912 landed.", True, 1_000.0)
    registry = _Registry([landing_record])

    class LandingMonitor(_Monitor):
        def assess(self, record, observation, airports, now_epoch):
            return [landing] if record.flight_id == landing_record.flight_id else []

    run_once(_observation, registry, LandingMonitor([]), {}, FakePoster(), now_epoch=1_000.0)
    registry.records = [landed_record]
    run_once(
        _observation, registry, _Monitor([]), {}, FakePoster(),
        now_epoch=1_000.0 + LANDING_TO_DONE_SECONDS - 1,
    )
    assert registry.statuses == [(landing_record.flight_id, "landed")]

    run_once(
        _observation, registry, _Monitor([]), {}, FakePoster(),
        now_epoch=1_000.0 + LANDING_TO_DONE_SECONDS,
    )

    # The grace period runs from the landing event, not from the scheduled arrival.
    assert registry.statuses == [
        (landing_record.flight_id, "landed"),
        (landing_record.flight_id, "done"),
    ]
    _clear_landing_state()


def test_the_reported_cadence_reflects_post_assessment_state() -> None:
    class StateChangingMonitor(_Monitor):
        def __init__(self) -> None:
            super().__init__([])
            self.airborne = False

        def assess(self, record, observation, airports, now_epoch):
            self.airborne = True
            return []

        def recommend_poll_seconds(self, record, now_epoch):
            return 300 if self.airborne else 900

    report = run_once(
        _observation,
        _Registry([make_record()]),
        StateChangingMonitor(),
        {},
        FakePoster(),
        now_epoch=NOW,
    )

    assert report["next_poll_seconds"] == 300


def test_no_upcoming_flights_reports_the_default_cadence() -> None:
    report = run_once(_observation, _Registry([]), _Monitor([]), {}, FakePoster(), NOW)

    assert report == {
        "checked": 0,
        "events": [],
        "posts": 0,
        "errors": [],
        "next_poll_seconds": 900,
    }


def test_outbox_mode_enqueues_then_drains_instead_of_posting_directly(tmp_path) -> None:
    record = make_record()
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))
    poster = FakePoster()

    report = run_once(
        _observation,
        _Registry([record]),
        _Monitor([FlightEvent(record.flight_id, "takeoff", "Airborne", True, 1_000.0)]),
        {},
        poster,
        now_epoch=1_000.0,
        outbox=outbox,
    )

    # The poster is called once — by the outbox drain, not by event processing.
    assert len(outbox.entries()) == 1
    assert report["posts"] == 1
    assert len(poster.calls) == 1


def test_outbox_mode_fans_out_to_the_resolved_recipients(tmp_path) -> None:
    record = make_record()
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))
    posted = {}

    def poster_for(recipient_key: str):
        posted.setdefault(recipient_key, FakePoster())
        return posted[recipient_key]

    report = run_once(
        _observation,
        _Registry([record]),
        _Monitor([FlightEvent(record.flight_id, "delay", "Delayed", True, 1_000.0)]),
        {},
        FakePoster(),
        now_epoch=1_000.0,
        outbox=outbox,
        recipients_for=lambda record: ["alex", "sam"],
        poster_for=poster_for,
    )

    assert {entry.recipient for entry in outbox.entries()} == {"alex", "sam"}
    assert report["posts"] == 2
    assert len(posted["alex"].calls) == 1 and len(posted["sam"].calls) == 1


def test_a_flight_with_no_resolved_recipients_is_not_enqueued(tmp_path) -> None:
    record = make_record()
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))

    report = run_once(
        _observation,
        _Registry([record]),
        _Monitor([FlightEvent(record.flight_id, "delay", "Delayed", True, 1_000.0)]),
        {},
        FakePoster(),
        now_epoch=1_000.0,
        outbox=outbox,
        recipients_for=lambda record: [],
    )

    assert outbox.entries() == []
    assert report["posts"] == 0
    # The event is still reported, so a misconfiguration is visible.
    assert [event["kind"] for event in report["events"]] == ["delay"]


def test_connection_alerts_are_emitted_across_the_whole_pass() -> None:
    # Given: two connected legs with a 30-minute gap at DFW.
    first = make_record(
        leg=make_leg(sched_arr_iso="2026-07-11T16:00:00-05:00"),
    )
    second = make_record(
        flight_id="AA1203-2026-07-11",
        leg=make_leg(
            number=1203,
            origin="DFW",
            dest="JFK",
            sched_dep_iso="2026-07-11T16:30:00-05:00",
            sched_arr_iso="2026-07-11T22:30:00-04:00",
        ),
    )
    poster = FakePoster()

    report = run_once(
        _observation,
        _Registry([first, second]),
        _Monitor([]),
        {},
        poster,
        now_epoch=1_000.0,
    )

    alerts = [event for event in report["events"] if event["kind"] == "connection_alert"]
    assert len(alerts) == 1
    assert any("30 min" in call for call in poster.calls)


def test_a_failure_inside_connection_analysis_is_reported_not_raised() -> None:
    class BrokenMonitor(_Monitor):
        def state_snapshot(self):
            raise RuntimeError("state unavailable")

    report = run_once(
        _observation,
        _Registry([make_record()]),
        BrokenMonitor([]),
        {},
        FakePoster(),
        now_epoch=1_000.0,
    )

    assert any("connection_alerts" in error for error in report["errors"])


def test_a_monitor_without_a_state_snapshot_still_runs() -> None:
    class Minimal:
        def assess(self, record, observation, airports, now_epoch):
            return []

        def recommend_poll_seconds(self, record, now_epoch):
            return 900

    report = run_once(
        _observation, _Registry([make_record()]), Minimal(), {}, FakePoster(), NOW
    )

    assert report["errors"] == []
