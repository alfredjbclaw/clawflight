"""Whole-pipeline flows over the synthetic fixtures.

Ingest -> attribute -> watch -> compose -> deliver, with every edge faked and
no network, credential, or clock dependency.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from clawflight.adapters.mailbox import messages_to_candidates
from clawflight.adapters.mailbox_mbox import MboxAdapter
from clawflight.airports import default_airports
from clawflight.audit import run_audit
from clawflight.connections import connection_alerts
from clawflight.models import FlightUpdate, Observation, Position
from clawflight.monitor import Monitor
from clawflight.notify import DeliveryOutbox, FakePoster, compose_trip_card
from clawflight.parse import parse_calendar_events, parse_cancellations
from clawflight.recipients import FollowStore, resolve_recipients
from clawflight.registry import BACKUP_NOTE, Registry
from clawflight.runner import run_once


JUL11 = datetime(2026, 7, 11, 15, 0, tzinfo=timezone.utc).timestamp()
JUL12 = datetime(2026, 7, 12, 14, 0, tzinfo=timezone.utc).timestamp()


def _seeded(tmp_path, people, calendar_text) -> Registry:
    registry = Registry(str(tmp_path / "registry.json"), people)
    registry.merge(parse_calendar_events(calendar_text, default_year=2026))
    return registry


# -- ingestion --------------------------------------------------------------


def test_a_calendar_export_becomes_an_attributed_multi_person_registry(
    tmp_path, people, calendar_text
) -> None:
    registry = _seeded(tmp_path, people, calendar_text)

    attribution = {
        record.flight_id: record.person.key for record in registry.all_records()
    }

    assert attribution == {
        "AA1203-2026-07-11": "alex",
        "AA4912-2026-07-11": "alex",
        "AS318-2026-07-12": "sam",
        "B6622-2026-07-16": "sam",
        "DL1120-2026-07-12": "robin",
        "DL2201-2026-07-17": "alex",
        "DL767-2026-07-16": "alex",
        "UA512-2026-07-12": "sam",
        "WN1470-2026-07-12": "unknown",
    }


def test_a_cancellation_notice_updates_the_itinerary_it_names(
    tmp_path, people, calendar_text
) -> None:
    # Given: a registry in which one booking carries the code the standalone
    # cancellation notice names.
    booked = calendar_text.replace("Confirmation code: FAKE03", "Confirmation code: FAKE05")
    registry = _seeded(tmp_path, people, booked)
    assert registry.get("DL2201-2026-07-17").leg.conf_code == "FAKE05"

    codes = parse_cancellations(calendar_text, default_year=2026)
    changed = [
        flight_id for code in codes for flight_id in registry.set_status_by_conf(code, "cancelled")
    ]

    # Then: the status changed in place; no phantom flight was created.
    assert codes == ["FAKE05"]
    assert changed == ["DL2201-2026-07-17"]
    assert registry.get("DL2201-2026-07-17").status == "cancelled"


def test_a_mailbox_sweep_and_a_calendar_export_enrich_one_another(
    tmp_path, people, fixtures, calendar_text
) -> None:
    registry = _seeded(tmp_path, people, calendar_text)
    messages = MboxAdapter(fixtures / "inbox.mbox").fetch()

    candidates, _skipped = messages_to_candidates(messages, {"air.example"})
    report = registry.merge_email_candidates(candidates)

    assert "DL248-2026-08-19" in report["created"]
    assert "UA410-2026-08-21" in report["created"]
    mailed = registry.get("DL248-2026-08-19")
    assert mailed.person.key == "alex"
    assert mailed.sources[0].startswith("mail:trip-1001@air.example:")
    # Calendar flights are untouched by the mail sweep.
    assert registry.get("AA4912-2026-07-11").sources == ("cal:cal-0001", "cal:cal-0002")


def test_a_same_day_pair_is_flagged_as_backups_not_deduplicated(
    tmp_path, people, calendar_text
) -> None:
    registry = _seeded(tmp_path, people, calendar_text)

    primary = registry.get("UA512-2026-07-12")
    backup = registry.get("AS318-2026-07-12")

    assert primary is not None and backup is not None
    assert primary.backup_group == backup.backup_group is not None
    assert BACKUP_NOTE in primary.notes and BACKUP_NOTE in backup.notes


# -- watching and delivery --------------------------------------------------


def test_a_travel_day_produces_a_trip_card_for_each_traveler(
    tmp_path, people, calendar_text
) -> None:
    registry = _seeded(tmp_path, people, calendar_text)
    legs = [
        record
        for record in registry.all_records()
        if record.leg.date == "2026-07-11" and record.person.key == "alex"
    ]

    card = compose_trip_card(sorted(legs, key=lambda record: record.leg.sched_dep_iso))

    assert "Travel Day — 2026-07-11 (Alex)" in card
    assert "AA4912 ASE -> DFW" in card
    assert "AA1203 DFW -> JFK" in card
    assert "[FAKE01]" in card


def test_a_full_pass_watches_delivers_and_records_provenance(
    tmp_path, people, calendar_text, recipients
) -> None:
    registry = _seeded(tmp_path, people, calendar_text)
    monitor = Monitor(str(tmp_path / "monitor.json"))
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))
    store = FollowStore(str(tmp_path / "follows.json"))
    poster = FakePoster()

    report = run_once(
        lambda record: Observation(record.flight_id, None, None, None, JUL11),
        registry,
        monitor,
        default_airports(),
        poster,
        now_epoch=JUL11,
        outbox=outbox,
        recipients_for=lambda record: [
            recipient.key for recipient in resolve_recipients(record, recipients, store)
        ],
    )

    kinds = {event["kind"] for event in report["events"]}
    assert "tracking_started" in kinds
    assert report["errors"] == []
    # Alex follows everything, so every event fans out to at least Alex.
    assert all(entry.recipient in ("alex", "sam") for entry in outbox.entries())
    assert report["posts"] == len(outbox.entries())
    assert all(entry.state == "acknowledged" for entry in outbox.entries())


def test_a_second_pass_repeats_nothing(tmp_path, people, calendar_text) -> None:
    registry = _seeded(tmp_path, people, calendar_text)
    monitor = Monitor(str(tmp_path / "monitor.json"))
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))
    poster = FakePoster()

    def pass_once(now: float):
        return run_once(
            lambda record: Observation(record.flight_id, None, None, None, now),
            registry,
            monitor,
            default_airports(),
            poster,
            now_epoch=now,
            outbox=outbox,
        )

    first = pass_once(JUL11)
    delivered_after_first = len(poster.calls)
    second = pass_once(JUL11 + 120)

    assert first["events"]
    assert second["events"] == []
    assert len(poster.calls) == delivered_after_first


def test_a_flight_flies_from_watch_to_landed_across_passes(
    tmp_path, people, calendar_text
) -> None:
    registry = _seeded(tmp_path, people, calendar_text)
    monitor = Monitor(str(tmp_path / "monitor.json"))
    airports = default_airports()
    record = registry.get("AA4912-2026-07-11")
    origin, dest = airports["ASE"], airports["DFW"]

    def at(fraction: float, altitude: float, now: float) -> Observation:
        return Observation(
            record.flight_id,
            Position(
                lat=origin.lat + (dest.lat - origin.lat) * fraction,
                lon=origin.lon + (dest.lon - origin.lon) * fraction,
                alt_ft=altitude,
                gs_kt=420.0,
                vert_rate_fpm=500.0 if fraction < 0.9 else -800.0,
                ts_epoch=now,
            ),
            None,
            None,
            now,
        )

    departure = datetime(2026, 7, 11, 18, 51, tzinfo=timezone.utc).timestamp()
    takeoff = monitor.assess(record, at(0.02, 6_000.0, departure), airports, departure)
    cruise = monitor.assess(
        record, at(0.55, 34_000.0, departure + 3600), airports, departure + 3600
    )
    arrival = monitor.assess(
        record, at(0.995, 2_000.0, departure + 7200), airports, departure + 7200
    )

    assert [event.kind for event in takeoff] == ["tracking_started", "takeoff"]
    assert [event.kind for event in cruise] == ["halfway"]
    assert [event.kind for event in arrival] == ["landing"]
    assert monitor.landed_awaiting_done(departure + 7200 + 1800) == [
        "AA4912-2026-07-11"
    ]


def test_a_connection_alert_reflects_a_pushed_delay(
    tmp_path, people, calendar_text
) -> None:
    # Given: the two legs of Alex's itinerary, whose scheduled gap is 89 minutes.
    registry = _seeded(tmp_path, people, calendar_text)
    monitor = Monitor(str(tmp_path / "monitor.json"))
    legs = [
        registry.get("AA4912-2026-07-11"),
        registry.get("AA1203-2026-07-11"),
    ]
    assert connection_alerts(legs, monitor.state_snapshot(), JUL11) == []

    # When: the airline pushes a 70-minute arrival delay on the first leg.
    monitor.ingest_push_for_bookings(
        FlightUpdate(
            flight_number="AA4912",
            status="Delayed",
            departure_scheduled="2026-07-11T12:51:00-06:00",
            departure_revised=None,
            arrival_scheduled="2026-07-11T16:10:00-05:00",
            arrival_revised="2026-07-11T17:20:00-05:00",
            departure_terminal=None,
            departure_gate=None,
            arrival_terminal=None,
            arrival_gate=None,
        ),
        [legs[0]],
        JUL11,
    )
    alerts = connection_alerts(legs, monitor.state_snapshot(), JUL11)

    # Then: the connection becomes a critical 19-minute risk.
    assert [alert.kind for alert in alerts] == ["connection_alert"]
    assert alerts[0].critical is True
    assert "19 min" in alerts[0].message


# -- housekeeping -----------------------------------------------------------


def test_state_written_by_a_pass_passes_its_own_audit(
    tmp_path, people, calendar_text
) -> None:
    registry = _seeded(tmp_path, people, calendar_text)
    monitor = Monitor(str(tmp_path / "monitor.json"))
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))
    run_once(
        lambda record: Observation(record.flight_id, None, None, None, JUL11),
        registry,
        monitor,
        default_airports(),
        FakePoster(),
        now_epoch=JUL11,
        outbox=outbox,
    )

    report = run_audit(
        registry_path=str(tmp_path / "registry.json"),
        monitor_path=str(tmp_path / "monitor.json"),
        outbox_path=str(tmp_path / "outbox.json"),
    )

    assert report.summary["critical"] == 0
    # Only the deliberately unattributed guest itinerary is flagged.
    unattributed = [f for f in report.findings if f.category == "attribution"]
    assert [f.message for f in unattributed] == [
        "Unattributed flight: WN1470-2026-07-12"
    ]


def test_every_state_file_survives_a_process_restart(
    tmp_path, people, calendar_text
) -> None:
    _seeded(tmp_path, people, calendar_text)
    monitor = Monitor(str(tmp_path / "monitor.json"))
    outbox = DeliveryOutbox(str(tmp_path / "outbox.json"))
    store = FollowStore(str(tmp_path / "follows.json"))
    record = Registry(str(tmp_path / "registry.json"), people).get("AA4912-2026-07-11")
    monitor.assess(
        record,
        Observation(record.flight_id, None, None, None, JUL11),
        default_airports(),
        JUL11,
    )
    store.follow("sam", record.flight_id)

    reloaded_registry = Registry(str(tmp_path / "registry.json"), people)
    reloaded_monitor = Monitor(str(tmp_path / "monitor.json"))
    reloaded_store = FollowStore(str(tmp_path / "follows.json"))

    assert reloaded_registry.get("AA4912-2026-07-11").leg.seat == "10C"
    assert reloaded_monitor.state_snapshot()["AA4912-2026-07-11"]["phase"] == "watch"
    assert reloaded_store.followed("sam") == ["AA4912-2026-07-11"]
    assert DeliveryOutbox(str(tmp_path / "outbox.json")).entries() == outbox.entries()


def test_a_prune_pass_leaves_only_live_state(tmp_path, people, calendar_text) -> None:
    registry = _seeded(tmp_path, people, calendar_text)
    monitor = Monitor(str(tmp_path / "monitor.json"))
    record = registry.get("AA4912-2026-07-11")
    monitor.assess(
        record,
        Observation(record.flight_id, None, None, None, JUL11),
        default_airports(),
        JUL11,
    )
    registry.set_status(record.flight_id, "done")

    dropped = registry.prune_done(JUL11 + 40 * 86400)
    orphaned = monitor.prune(
        {item.flight_id for item in registry.all_records()}, JUL11 + 40 * 86400
    )

    assert dropped == ["AA4912-2026-07-11"]
    assert orphaned == ["AA4912-2026-07-11"]
    assert json.loads((tmp_path / "monitor.json").read_text()) == {}
