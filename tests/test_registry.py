from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Dict, Optional

import pytest

from clawflight.email_ingest import EmailEvidence, EmailItineraryCandidate
from clawflight.models import FlightLeg
from clawflight.parse import ParsedFlight, parse_calendar_events
from clawflight.registry import BACKUP_NOTE, Registry, extract_service_date


def _parsed(
    carrier: str,
    number: int,
    date: str,
    source_id: str,
    *,
    origin: Optional[str] = "JFK",
    dep: Optional[str] = "2026-07-11T10:00:00-04:00",
    conf_code: Optional[str] = None,
    seat: Optional[str] = None,
    operating_carrier: Optional[str] = None,
    operating_number: Optional[int] = None,
    hints: Optional[Dict] = None,
) -> ParsedFlight:
    return ParsedFlight(
        leg=FlightLeg(
            carrier=carrier,
            number=number,
            date=date,
            origin=origin,
            dest="LAX",
            sched_dep_iso=dep,
            sched_arr_iso=None,
            conf_code=conf_code,
            seat=seat,
            operating_carrier=operating_carrier,
            operating_number=operating_number,
        ),
        hints={"source_id": source_id, **(hints or {})},
    )


def _email_candidate(
    *,
    confirmation_code: str = "FAKE20",
    source_id: str = "trip-1001@air.example",
    digest: str = "a" * 64,
    traveler: Optional[str] = "SAM KESTREL",
) -> EmailItineraryCandidate:
    return EmailItineraryCandidate(
        carrier="DL",
        number=248,
        service_date="2026-08-19",
        origin="JFK",
        destination="LAX",
        confirmation_code=confirmation_code,
        traveler=traveler,
        evidence=EmailEvidence(
            source_id=source_id,
            sender_identity="confirmations@air.example",
            organizer_identity=None,
            observed_at=datetime(2026, 7, 20, 14, 30, tzinfo=timezone.utc),
            source_kind="email",
            digest=digest,
        ),
    )


@pytest.fixture
def registry(tmp_path, people) -> Registry:
    return Registry(str(tmp_path / "registry.json"), people)


# -- vendor-update matching -------------------------------------------------


def test_date_guard_distinguishes_unknown_date_from_unknown_booking(registry) -> None:
    registry.merge(
        [
            _parsed("AA", 4912, "2026-07-10", "early"),
            _parsed("AA", 4912, "2026-07-14", "late"),
        ]
    )
    update = SimpleNamespace(
        flight_number="aa-4912",
        service_date="2026-07-12",
        departure_scheduled="2026-07-13T10:00:00-04:00",
    )

    # A vendor update for a date we do not hold must match nothing, but the
    # nearest known date is still reportable for a human to resolve.
    assert registry.matching_bookings(update) == []
    assert extract_service_date(update) == "2026-07-12"
    assert registry.nearest_date_for_number(" aa-4912 ", "2026-07-12") == "2026-07-10"


def test_extract_service_date_returns_none_without_a_parseable_date() -> None:
    assert (
        extract_service_date(
            SimpleNamespace(service_date="not-a-date", departure_scheduled=None)
        )
        is None
    )


def test_nearest_date_ties_choose_the_earlier_and_handle_no_match(registry) -> None:
    registry.merge(
        [
            _parsed("DL", 767, "2026-07-10", "early"),
            _parsed("DL", 767, "2026-07-12", "late"),
        ]
    )

    assert registry.nearest_date_for_number("dl 767", "2026-07-11") == "2026-07-10"
    assert registry.nearest_date_for_number("AA4912", "2026-07-11") is None
    assert registry.nearest_date_for_number("DL767", "not-a-date") is None


# -- email candidates -------------------------------------------------------


def test_email_candidate_merges_as_a_scheduleless_durable_record(tmp_path, people) -> None:
    path = tmp_path / "registry.json"
    registry = Registry(str(path), people)

    report = registry.merge_email_candidates([_email_candidate()])

    assert report["created"] == ["DL248-2026-08-19"]
    record = Registry(str(path), people).get("DL248-2026-08-19")
    assert record is not None
    assert (record.leg.carrier, record.leg.number, record.leg.date) == (
        "DL",
        248,
        "2026-08-19",
    )
    assert (record.leg.origin, record.leg.dest) == ("JFK", "LAX")
    assert record.leg.conf_code == "FAKE20"
    assert record.leg.sched_dep_iso is None
    assert record.person.key == "sam"
    assert record.sources == ("mail:trip-1001@air.example:{}".format("a" * 64),)
    # Provenance is an identifier and a digest — never the sender or the body.
    persisted = path.read_text(encoding="utf-8")
    assert "confirmations@air.example" not in persisted


def test_email_reingestion_is_idempotent(tmp_path, people) -> None:
    path = tmp_path / "registry.json"
    registry = Registry(str(path), people)
    candidate = _email_candidate()
    registry.merge_email_candidates([candidate])
    before = path.read_text(encoding="utf-8")

    report = registry.merge_email_candidates([candidate])

    assert report == {"created": [], "updated": [], "backup_groups": []}
    assert path.read_text(encoding="utf-8") == before


def test_calendar_enriches_an_email_record_and_email_cannot_erase_detail(
    tmp_path, people
) -> None:
    path = tmp_path / "registry.json"
    registry = Registry(str(path), people)
    candidate = _email_candidate()
    registry.merge_email_candidates([candidate])
    calendar = _parsed(
        "DL",
        248,
        "2026-08-19",
        "cal-9",
        dep="2026-08-19T09:45:00-04:00",
        conf_code="FAKE20",
        seat="14C",
        operating_carrier="9E",
        operating_number=5248,
    )

    registry.merge([calendar])
    registry.merge_email_candidates([candidate])

    record = Registry(str(path), people).get("DL248-2026-08-19")
    assert record is not None
    assert record.leg.sched_dep_iso == "2026-08-19T09:45:00-04:00"
    assert record.leg.seat == "14C"
    assert (record.leg.operating_carrier, record.leg.operating_number) == ("9E", 5248)
    assert (record.leg.origin, record.leg.dest, record.leg.conf_code) == (
        "JFK",
        "LAX",
        "FAKE20",
    )
    assert record.person.key == "sam"
    assert record.sources == (
        "cal:cal-9",
        "mail:trip-1001@air.example:{}".format("a" * 64),
    )


def test_bookings_with_distinct_confirmations_stay_separate(registry) -> None:
    first = _email_candidate(
        confirmation_code="FAKE20", source_id="trip-1001@air.example", digest="1" * 64
    )
    second = _email_candidate(
        confirmation_code="FAKE21", source_id="trip-1002@air.example", digest="2" * 64
    )

    report = registry.merge_email_candidates([first, second])

    assert report["created"] == ["DL248-2026-08-19", "DL248-2026-08-19#FAKE21"]
    assert registry.get("DL248-2026-08-19").leg.conf_code == "FAKE20"
    assert registry.get("DL248-2026-08-19#FAKE21").leg.conf_code == "FAKE21"


# -- attribution ------------------------------------------------------------


def test_fixture_bookings_merge_and_attribute_their_passengers(
    registry, calendar_text
) -> None:
    # Given: the synthetic calendar export.
    report = registry.merge(parse_calendar_events(calendar_text, default_year=2026))

    # Then: every unique leg is retained and passenger evidence wins.
    assert {
        "AA4912-2026-07-11",
        "AA1203-2026-07-11",
        "DL767-2026-07-16",
        "UA512-2026-07-12",
        "AS318-2026-07-12",
    } <= set(report["created"])
    first_leg = registry.get("AA4912-2026-07-11")
    award = registry.get("DL767-2026-07-16")
    assert first_leg is not None and first_leg.person.key == "alex"
    assert award is not None and award.person.name == "Alex"
    # Two calendars described the same leg; both provenances survive.
    assert first_leg.sources == ("cal:cal-0001", "cal:cal-0002")


def test_fixture_possessive_title_and_unknown_traveler_are_attributed(
    registry, calendar_text
) -> None:
    registry.merge(parse_calendar_events(calendar_text, default_year=2026))

    assert registry.get("DL1120-2026-07-12").person.key == "robin"
    assert registry.get("B6622-2026-07-16").person.key == "sam"
    assert registry.get("WN1470-2026-07-12").person.key == "unknown"


def test_fixture_schedule_change_notice_enriches_the_existing_leg(
    registry, calendar_text
) -> None:
    # Given: a calendar block that restates AA1203 with change language.
    registry.merge(parse_calendar_events(calendar_text, default_year=2026))

    record = registry.get("AA1203-2026-07-11")

    # Then: it updated THAT itinerary rather than creating a second flight.
    assert record is not None
    assert record.leg.conf_code == "FAKE01"
    assert record.leg.origin == "DFW"
    assert "cal:cal-0006" in record.sources
    assert any("schedule change" in note.lower() for note in record.notes)


def test_richer_source_enriches_a_sparse_booking_and_status_persists(
    tmp_path, people
) -> None:
    # Given: a sparse booking followed by a richer copy from another source.
    path = tmp_path / "registry.json"
    registry = Registry(str(path), people)
    sparse = _parsed(
        "AA", 1203, "2026-07-11", "one", conf_code=None, seat=None,
        hints={"attendees": ["sam.kestrel@example.com"]},
    )
    rich = _parsed(
        "AA", 1203, "2026-07-11", "two", conf_code="FAKE30", seat="12A",
        hints={"passenger_name": "SAMUEL T KESTREL", "notes_excerpt": "booking confirmed"},
    )

    first_report = registry.merge([sparse])
    second_report = registry.merge([rich])
    registry.set_status("AA1203-2026-07-11", "active")

    # Then: one enriched record survives an atomic persistence round trip.
    assert first_report["created"] == ["AA1203-2026-07-11"]
    assert second_report["updated"] == ["AA1203-2026-07-11"]
    reloaded = Registry(str(path), people).get("AA1203-2026-07-11")
    assert reloaded is not None
    assert reloaded.person.key == "sam"
    assert reloaded.leg.conf_code == "FAKE30"
    assert reloaded.leg.seat == "12A"
    assert reloaded.sources == ("cal:one", "cal:two")
    assert reloaded.status == "active"


def test_passenger_evidence_wins_regardless_of_merge_order(tmp_path, people) -> None:
    # Given: an attendee-attributed booking and a later copy naming a passenger.
    path = tmp_path / "registry.json"
    registry = Registry(str(path), people)
    registry.merge(
        [
            _parsed(
                "AA", 1203, "2026-07-11", "attendee",
                hints={"attendees": ["sam.kestrel@example.com"]},
            ),
            _parsed(
                "AA", 1203, "2026-07-11", "passenger",
                hints={"passenger_name": "ALEXANDRA MORGAN KESTREL"},
            ),
        ]
    )

    merged = Registry(str(path), people).get("AA1203-2026-07-11")
    assert merged is not None
    assert (merged.person.key, merged.person.name) == ("alex", "Alex")


def test_passenger_attribution_outranks_an_earlier_possessive_title(registry) -> None:
    registry.merge(
        [_parsed("DL", 767, "2026-07-16", "cal-title", hints={"title": "Alex's flight"})]
    )
    registry.merge(
        [
            _parsed(
                "DL", 767, "2026-07-16", "mail:message-767",
                hints={"passenger_name": "ROBIN J KESTREL"},
            )
        ]
    )

    assert registry.get("DL767-2026-07-16").person.key == "robin"


def test_a_weaker_later_source_never_downgrades_attribution(registry) -> None:
    registry.merge(
        [
            _parsed(
                "DL", 767, "2026-07-16", "strong",
                hints={"passenger_name": "ALEX KESTREL"},
            )
        ]
    )
    registry.merge(
        [
            _parsed(
                "DL", 767, "2026-07-16", "weak",
                hints={"attendees": ["sam.kestrel@example.com"]},
            )
        ]
    )

    assert registry.get("DL767-2026-07-16").person.key == "alex"


def test_source_prefixes_are_preserved_and_defaulted(registry) -> None:
    registry.merge(
        [
            _parsed("DL", 248, "2026-08-19", "cal-248"),
            _parsed("DL", 248, "2026-08-19", "mail:message-248"),
        ]
    )

    assert registry.get("DL248-2026-08-19").sources == (
        "cal:cal-248",
        "mail:message-248",
    )


def test_unmatched_passenger_stays_tracked_and_upcoming(registry) -> None:
    registry.merge(
        [
            _parsed(
                "UA", 404, "2026-07-11", "guest",
                hints={"passenger_name": "TAYLOR RIVERS"},
            )
        ]
    )
    now = datetime(2026, 7, 11, 12, tzinfo=timezone.utc).timestamp()

    record = registry.get("UA404-2026-07-11")
    assert record is not None and record.person.key == "unknown"
    assert [flight.flight_id for flight in registry.upcoming(now, horizon_days=1)] == [
        "UA404-2026-07-11"
    ]


# -- backup grouping --------------------------------------------------------


def test_same_person_nearby_departures_group_as_backups(registry) -> None:
    # Given: two Alex flights on one date departing less than four hours apart.
    primary = _parsed("DL", 100, "2026-07-11", "primary", hints={"title": "Alex's flight"})
    alternative = _parsed(
        "AA", 200, "2026-07-11", "alternative",
        dep="2026-07-11T13:30:00-04:00", hints={"title": "Alex's flight"},
    )

    report = registry.merge([primary, alternative])

    # Then: neither is discarded and both share one explanatory backup group.
    first = registry.get("DL100-2026-07-11")
    second = registry.get("AA200-2026-07-11")
    assert len(report["backup_groups"]) == 1
    assert first.backup_group == second.backup_group == report["backup_groups"][0]
    assert first.notes.count(BACKUP_NOTE) == 1
    assert second.notes.count(BACKUP_NOTE) == 1


def test_fixture_primary_and_backup_pair_is_grouped(registry, calendar_text) -> None:
    # Given: the fixture's deliberate same-day primary/backup pair for Sam.
    report = registry.merge(parse_calendar_events(calendar_text, default_year=2026))

    primary = registry.get("UA512-2026-07-12")
    backup = registry.get("AS318-2026-07-12")

    assert len(report["backup_groups"]) == 1
    assert primary.backup_group == backup.backup_group == report["backup_groups"][0]
    assert BACKUP_NOTE in primary.notes


def test_unknown_time_options_group_only_when_origin_matches(registry) -> None:
    first = _parsed(
        "UA", 1, "2026-07-11", "one", origin="SFO", dep=None,
        hints={"passenger_name": "SAM KESTREL"},
    )
    second = _parsed(
        "AS", 2, "2026-07-11", "two", origin="SFO", dep=None,
        hints={"passenger_name": "SAMUEL T KESTREL"},
    )
    unmatched = _parsed(
        "WN", 3, "2026-07-11", "three", origin="SEA", dep=None,
        hints={"passenger_name": "SAM KESTREL"},
    )

    registry.merge([first, second, unmatched])

    assert registry.get("UA1-2026-07-11").backup_group == (
        registry.get("AS2-2026-07-11").backup_group
    )
    assert registry.get("WN3-2026-07-11").backup_group is None


def test_stale_backup_group_is_removed_after_person_enrichment(registry) -> None:
    # Given: two unknown-person same-origin options previously grouped as backups.
    registry.merge(
        [
            _parsed("AA", 1, "2026-07-11", "one", dep=None, hints={}),
            _parsed("DL", 2, "2026-07-11", "two", dep=None, hints={}),
        ]
    )

    # When: one gains stronger passenger attribution and leaves the group.
    registry.merge(
        [
            _parsed(
                "AA", 1, "2026-07-11", "three", dep=None,
                hints={"passenger_name": "ALEX KESTREL"},
            )
        ]
    )

    # Then: neither now-unpaired record keeps an obsolete grouping or note.
    first = registry.get("AA1-2026-07-11")
    second = registry.get("DL2-2026-07-11")
    assert first.backup_group is None and second.backup_group is None
    assert BACKUP_NOTE not in first.notes + second.notes


def test_same_flight_different_conf_stays_two_bookings(registry) -> None:
    report = registry.merge(
        [
            _parsed("DL", 767, "2026-07-16", "one", conf_code="FAKE02"),
            _parsed("DL", 767, "2026-07-16", "two", conf_code="FAKE03"),
        ]
    )

    assert len(report["created"]) == 2
    assert {
        registry.get(flight_id).leg.conf_code for flight_id in report["created"]
    } == {"FAKE02", "FAKE03"}


def test_shared_conf_connection_legs_are_not_backups(registry) -> None:
    # Given: two legs of one itinerary departing within four hours.
    report = registry.merge(
        [
            _parsed(
                "AA", 4912, "2026-07-11", "one", conf_code="FAKE01",
                dep="2026-07-11T12:51:00-06:00",
                hints={"passenger_name": "ALEX KESTREL"},
            ),
            _parsed(
                "AA", 1203, "2026-07-11", "two", conf_code="FAKE01",
                dep="2026-07-11T14:39:00-06:00",
                hints={"passenger_name": "ALEX KESTREL"},
            ),
        ]
    )

    assert report["backup_groups"] == []
    assert registry.get("AA4912-2026-07-11").backup_group is None
    assert registry.get("AA1203-2026-07-11").backup_group is None


# -- status and lifecycle ---------------------------------------------------


def test_set_status_by_conf_updates_the_matching_itinerary(registry) -> None:
    registry.merge([_parsed("DL", 767, "2026-07-16", "one", conf_code="FAKE05")])

    changed = registry.set_status_by_conf("fake05", "cancelled")

    assert changed == ["DL767-2026-07-16"]
    assert registry.get("DL767-2026-07-16").status == "cancelled"
    assert registry.set_status_by_conf("NOPE00", "cancelled") == []
    assert registry.set_status_by_conf("", "cancelled") == []


def test_set_status_for_update_applies_to_every_matching_booking(registry) -> None:
    registry.merge(
        [
            _parsed("DL", 4133, "2026-07-11", "one", conf_code="FAKE40"),
            _parsed("DL", 4133, "2026-07-11", "two", conf_code="FAKE41"),
        ]
    )
    update = SimpleNamespace(
        flight_number="DL4133",
        service_date=None,
        departure_scheduled="2026-07-11T08:00:00-04:00",
    )

    changed = registry.set_status_for_update(update, "cancelled")

    assert len(changed) == 2
    assert all(registry.get(fid).status == "cancelled" for fid in changed)


def test_upcoming_respects_the_horizon_and_terminal_statuses(registry) -> None:
    now = datetime(2026, 7, 11, 12, tzinfo=timezone.utc).timestamp()
    registry.merge(
        [
            _parsed("AA", 1, "2026-07-11", "today", dep="2026-07-11T10:00:00-04:00"),
            _parsed("AA", 2, "2026-07-20", "far", dep="2026-07-20T10:00:00-04:00"),
            _parsed("AA", 3, "2026-07-11", "done", dep="2026-07-11T11:00:00-04:00"),
        ]
    )
    registry.set_status("AA3-2026-07-11", "done")

    assert [item.flight_id for item in registry.upcoming(now, horizon_days=1)] == [
        "AA1-2026-07-11"
    ]
    assert registry.upcoming(now, horizon_days=-1) == []


def test_prune_done_drops_only_old_completed_records(registry) -> None:
    registry.merge(
        [
            _parsed("AA", 1, "2026-01-01", "old", dep="2026-01-01T10:00:00-05:00"),
            _parsed("AA", 2, "2026-07-11", "new", dep="2026-07-11T10:00:00-04:00"),
        ]
    )
    registry.set_status("AA1-2026-01-01", "done")
    now = datetime(2026, 7, 11, 12, tzinfo=timezone.utc).timestamp()

    dropped = registry.prune_done(now, max_age_days=30)

    assert dropped == ["AA1-2026-01-01"]
    assert registry.get("AA1-2026-01-01") is None
    assert registry.get("AA2-2026-07-11") is not None


# -- persistence ------------------------------------------------------------


def test_empty_merges_and_missing_lookups_are_harmless(registry) -> None:
    now = datetime(2026, 7, 11, 12, tzinfo=timezone.utc).timestamp()

    empty_report = registry.merge([])
    registry.merge([_parsed("ZZ", 9, "2026-07-11", "unknown", hints={})])
    upcoming = registry.upcoming(now, horizon_days=1)
    registry.set_status("missing-flight", "active")

    assert empty_report == {"created": [], "updated": [], "backup_groups": []}
    assert registry.get("ZZ9-2026-07-11").person.key == "unknown"
    assert [item.flight_id for item in upcoming] == ["ZZ9-2026-07-11"]
    assert registry.get("missing-flight") is None


def test_mutations_reload_disk_state_before_writing(tmp_path, people) -> None:
    # Given: two Registry instances on one file, as tick and sweep would be.
    path = str(tmp_path / "registry.json")
    Registry(path, people).merge(
        [_parsed("AA", 1, "2026-07-11", "one"), _parsed("AA", 2, "2026-07-11", "two")]
    )
    first_writer = Registry(path, people)
    second_writer = Registry(path, people)

    first_writer.set_status("AA1-2026-07-11", "done")
    second_writer.set_status("AA2-2026-07-11", "cancelled")

    # Then: the second writer's write preserved the first writer's change.
    reloaded = Registry(path, people)
    assert reloaded.get("AA1-2026-07-11").status == "done"
    assert reloaded.get("AA2-2026-07-11").status == "cancelled"


def test_corrupt_and_legacy_state_files_load_safely(tmp_path, people) -> None:
    path = tmp_path / "registry.json"

    path.write_text("{not json", encoding="utf-8")
    assert Registry(str(path), people).all_records() == []

    path.write_bytes(b"\xff\xfe")
    assert Registry(str(path), people).all_records() == []

    path.write_text('{"flights": "not-a-dict"}', encoding="utf-8")
    assert Registry(str(path), people).all_records() == []


def test_records_without_optional_leg_fields_load(tmp_path, people) -> None:
    # Given: a state file written before operating-carrier fields existed.
    path = tmp_path / "registry.json"
    path.write_text(
        '{"flights": {"AA4912-2026-07-11": {'
        '"flight_id": "AA4912-2026-07-11",'
        '"leg": {"carrier": "AA", "number": 4912, "date": "2026-07-11",'
        ' "origin": "ASE", "dest": "DFW", "sched_dep_iso": null,'
        ' "sched_arr_iso": null, "conf_code": "FAKE01", "seat": "10C"},'
        '"person": {"key": "alex", "name": "Alex"},'
        '"status": "scheduled", "sources": ["cal:x"], "notes": [],'
        '"_attribution_rank": 2}}}',
        encoding="utf-8",
    )

    record = Registry(str(path), people).get("AA4912-2026-07-11")

    assert record is not None
    assert record.leg.operating_carrier is None
    assert record.leg.operating_number is None


def test_state_file_is_written_with_owner_only_permissions(tmp_path, people) -> None:
    import stat

    path = tmp_path / "registry.json"
    Registry(str(path), people).merge([_parsed("AA", 1, "2026-07-11", "one")])

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_default_registry_attributes_nothing_without_a_people_table(tmp_path) -> None:
    # Given: a Registry built with no configured people (the fresh-install case).
    registry = Registry(str(tmp_path / "registry.json"))

    registry.merge(
        [_parsed("AA", 1, "2026-07-11", "one", hints={"passenger_name": "ALEX KESTREL"})]
    )

    assert registry.get("AA1-2026-07-11").person.key == "unknown"
