"""Durable itinerary registry: merge, attribute, and group bookings.

The registry is the single writer of ``registry.json``. It merges evidence from
every ingestion source, attributes each flight through an injected
:class:`~clawflight.people.PersonTable`, and groups same-day alternatives as
backup bookings instead of deduplicating them away.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from .email_ingest import EmailItineraryCandidate
from .models import FlightLeg, FlightRecord, PersonRef, flight_ident
from .parse import ParsedFlight
from .people import UNKNOWN_PERSON, PersonTable

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None


BACKUP_NOTE = "possible backup/duplicate booking"
SCHEDULE_CHANGE_NOTE = "Schedule change: airline notified a revised itinerary."

_TERMINAL_STATUSES = ("done", "cancelled")
_SOURCE_PREFIXES = ("cal:", "mail:", "email:", "manual:")
_DEFAULT_SOURCE_PREFIX = "cal:"


def extract_service_date(update) -> Optional[str]:
    """Return the valid service date carried by a vendor update, if any."""
    for source in (getattr(update, "service_date", None), update.departure_scheduled):
        if isinstance(source, str) and len(source) >= 10:
            candidate = source[:10]
            try:
                datetime.fromisoformat(candidate)
                return candidate
            except ValueError:
                continue
    return None


class Registry:
    def __init__(self, path: str, people: Optional[PersonTable] = None) -> None:
        self._path = path
        self._lock_path = path + ".lock"
        self._people = people if people is not None else PersonTable()
        self._records, self._attribution_ranks = self._load()

    @property
    def people(self) -> PersonTable:
        return self._people

    @contextlib.contextmanager
    def _cross_process_lock(self):
        """Serialize registry writes and reload current state before mutation."""
        if fcntl is None:  # pragma: no cover - non-POSIX fallback
            self._records, self._attribution_ranks = self._load()
            yield
            return
        directory = os.path.dirname(os.path.abspath(self._lock_path))
        os.makedirs(directory, exist_ok=True)
        handle = open(self._lock_path, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            self._records, self._attribution_ranks = self._load()
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def merge(self, parsed: "list[ParsedFlight]") -> dict:
        with self._cross_process_lock():
            created: List[str] = []
            updated: List[str] = []
            for flight in parsed:
                flight_id = flight_ident(
                    flight.leg.carrier, flight.leg.number, flight.leg.date
                )
                incoming = self._record_from_parsed(flight, flight_id)
                existing = self._records.get(flight_id)
                # A same-flight/same-date entry carrying a DIFFERENT confirmation
                # code is a separate intentional booking (two seats bought on one
                # day), not a duplicate to merge — key it distinctly by conf.
                if existing is not None and _differs_by_conf(existing, incoming):
                    flight_id = "{}#{}".format(flight_id, incoming.leg.conf_code)
                    incoming = self._record_from_parsed(flight, flight_id)
                    existing = self._records.get(flight_id)
                incoming_rank = self._people.rank(flight.hints)
                if existing is None:
                    self._records[flight_id] = incoming
                    self._attribution_ranks[flight_id] = incoming_rank
                    created.append(flight_id)
                    continue
                merged = self._merge_records(
                    existing,
                    incoming,
                    self._attribution_ranks.get(flight_id, _legacy_person_rank(existing)),
                    incoming_rank,
                )
                if merged != existing:
                    self._records[flight_id] = merged
                    updated.append(flight_id)
                self._attribution_ranks[flight_id] = max(
                    self._attribution_ranks.get(flight_id, _legacy_person_rank(existing)),
                    incoming_rank,
                )

            backup_groups = self._assign_backup_groups()
            if created or updated or backup_groups:
                self._write()
            return {
                "created": _unique(created),
                "updated": [value for value in _unique(updated) if value not in created],
                "backup_groups": backup_groups,
            }

    def merge_email_candidates(
        self, candidates: "Iterable[EmailItineraryCandidate]"
    ) -> dict:
        """Merge bounded itinerary evidence produced by trusted email ingestion."""
        parsed = [
            ParsedFlight(
                leg=FlightLeg(
                    carrier=candidate.carrier,
                    number=candidate.number,
                    date=candidate.service_date,
                    origin=candidate.origin,
                    dest=candidate.destination,
                    sched_dep_iso=None,
                    sched_arr_iso=None,
                    conf_code=candidate.confirmation_code,
                    seat=None,
                ),
                hints={
                    "passenger_name": candidate.traveler,
                    "source_id": _email_source_value(candidate),
                },
            )
            for candidate in candidates
        ]
        return self.merge(parsed)

    def upcoming(self, now_epoch: float, horizon_days: int) -> "list[FlightRecord]":
        if horizon_days < 0:
            return []
        earliest_epoch = now_epoch - 86400
        latest_epoch = now_epoch + (horizon_days * 86400)
        candidates = [
            record
            for record in self._records.values()
            if record.status not in _TERMINAL_STATUSES
            and earliest_epoch <= _departure_epoch(record) <= latest_epoch
        ]
        return sorted(
            candidates, key=lambda record: (_departure_epoch(record), record.flight_id)
        )

    def all_records(self) -> "list[FlightRecord]":
        return sorted(self._records.values(), key=lambda record: record.flight_id)

    def get(self, flight_id: str) -> "Optional[FlightRecord]":
        return self._records.get(flight_id)

    def matching_bookings(self, update) -> "list[FlightRecord]":
        """All bookings that match a vendor update's flight number and date."""
        flight_num = update.flight_number.upper().replace(" ", "").replace("-", "")
        service_date = extract_service_date(update)
        if service_date is None:
            return []
        matches = []
        for record in self._records.values():
            rec_num = "{}{}".format(record.leg.carrier, record.leg.number)
            if rec_num != flight_num or record.leg.date != service_date:
                continue
            matches.append(record)
        return sorted(matches, key=lambda record: record.flight_id)

    def nearest_date_for_number(
        self, flight_number: str, service_date: str
    ) -> Optional[str]:
        """Return the closest registry date for a flight number."""
        try:
            target_date = datetime.fromisoformat(service_date).date()
        except (TypeError, ValueError):
            return None
        flight_num = flight_number.upper().replace(" ", "").replace("-", "")
        candidates = []
        for record in self._records.values():
            rec_num = "{}{}".format(record.leg.carrier, record.leg.number)
            if rec_num != flight_num:
                continue
            try:
                record_date = datetime.fromisoformat(record.leg.date).date()
            except (TypeError, ValueError):
                continue
            candidates.append(
                (abs((record_date - target_date).days), record_date, record.leg.date)
            )
        if not candidates:
            return None
        return min(candidates)[2]

    def set_status_for_update(self, update, status: str) -> "list[str]":
        """Apply a status to all bookings matching a vendor update."""
        with self._cross_process_lock():
            changed: List[str] = []
            for record in self.matching_bookings(update):
                if record.status != status:
                    self._records[record.flight_id] = replace(record, status=status)
                    changed.append(record.flight_id)
            if changed:
                self._write()
            return changed

    def set_status(self, flight_id: str, status: str) -> None:
        with self._cross_process_lock():
            record = self._records.get(flight_id)
            if record is None or record.status == status:
                return
            self._records[flight_id] = replace(record, status=status)
            self._write()

    def set_status_by_conf(self, conf_code: str, status: str) -> "list[str]":
        """Apply a status to every leg sharing a confirmation code.

        Used when a calendar or email references an existing booking with
        cancellation/change language: update THAT itinerary instead of creating
        a new flight.
        """
        with self._cross_process_lock():
            target = (conf_code or "").strip().upper()
            if not target:
                return []
            changed: List[str] = []
            for flight_id, record in list(self._records.items()):
                if record.leg.conf_code == target and record.status != status:
                    self._records[flight_id] = replace(record, status=status)
                    changed.append(flight_id)
            if changed:
                self._write()
            return changed

    def forget(self, flight_id: str) -> bool:
        """Drop one record outright.

        Distinct from ``set_status(..., "cancelled")``: this is a person saying
        the flight should never have been here, so no trace of it should keep
        matching vendor updates or reappearing in status.
        """
        with self._cross_process_lock():
            if flight_id not in self._records:
                return False
            del self._records[flight_id]
            self._attribution_ranks.pop(flight_id, None)
            self._write()
            return True

    def prune_done(self, now_epoch: float, max_age_days: int = 30) -> "list[str]":
        """Drop 'done' records whose departure is older than max_age_days."""
        with self._cross_process_lock():
            cutoff = now_epoch - max_age_days * 86400
            dropped: List[str] = []
            for flight_id, record in list(self._records.items()):
                if record.status == "done" and _departure_epoch(record) < cutoff:
                    del self._records[flight_id]
                    self._attribution_ranks.pop(flight_id, None)
                    dropped.append(flight_id)
            if dropped:
                self._write()
            return dropped

    def _record_from_parsed(self, parsed: ParsedFlight, flight_id: str) -> FlightRecord:
        hints = parsed.hints
        source = _source_value(hints.get("source_id"))
        return FlightRecord(
            flight_id=flight_id,
            leg=parsed.leg,
            person=self._people.person_from_hints(hints),
            sources=(source,) if source is not None else (),
            backup_group=None,
            status="scheduled",
            notes=_notes_from_hints(hints),
        )

    def _merge_records(
        self,
        existing: FlightRecord,
        incoming: FlightRecord,
        existing_rank: int,
        incoming_rank: int,
    ) -> FlightRecord:
        return FlightRecord(
            flight_id=existing.flight_id,
            leg=_merge_legs(existing.leg, incoming.leg),
            person=incoming.person if incoming_rank > existing_rank else existing.person,
            sources=tuple(sorted(set(existing.sources + incoming.sources))),
            backup_group=existing.backup_group,
            status=existing.status,
            notes=tuple(_unique(list(existing.notes) + list(incoming.notes))),
        )

    def _assign_backup_groups(self) -> List[str]:
        assigned: List[str] = []
        planned: Dict[str, str] = {}
        groups: Dict[Tuple[str, str], List[FlightRecord]] = {}
        for record in self._records.values():
            groups.setdefault((record.person.key, record.leg.date), []).append(record)
        for person_key, date in sorted(groups):
            for component in _backup_components(groups[(person_key, date)]):
                if len(component) < 2:
                    continue
                existing_groups = sorted(
                    {
                        record.backup_group
                        for record in component
                        if record.backup_group is not None
                    }
                )
                group_id = (
                    existing_groups[0]
                    if existing_groups
                    else self._new_backup_group(person_key, date)
                )
                for record in component:
                    planned[record.flight_id] = group_id
                if not existing_groups and group_id not in assigned:
                    assigned.append(group_id)
        for record in list(self._records.values()):
            group_id = planned.get(record.flight_id)
            notes = record.notes
            if group_id is None:
                notes = tuple(note for note in notes if note != BACKUP_NOTE)
            elif BACKUP_NOTE not in notes:
                notes += (BACKUP_NOTE,)
            if record.backup_group != group_id or record.notes != notes:
                self._records[record.flight_id] = replace(
                    record, backup_group=group_id, notes=notes
                )
        return assigned

    def _new_backup_group(self, person_key: str, date: str) -> str:
        prefix = "bg-{}-{}-".format(person_key, date)
        sequence = 0
        for record in self._records.values():
            group = record.backup_group
            if group is not None and group.startswith(prefix):
                suffix = group[len(prefix) :]
                if suffix.isdigit():
                    sequence = max(sequence, int(suffix))
        return "{}{}".format(prefix, sequence + 1)

    def _load(self) -> Tuple[Dict[str, FlightRecord], Dict[str, int]]:
        try:
            with open(self._path, "r", encoding="utf-8") as state_file:
                payload = json.load(state_file)
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}, {}
        if not isinstance(payload, dict):
            return {}, {}
        serialized_records = payload.get("flights")
        if not isinstance(serialized_records, dict):
            return {}, {}
        records: Dict[str, FlightRecord] = {}
        ranks: Dict[str, int] = {}
        for flight_id, serialized in serialized_records.items():
            record = _deserialize_record(flight_id, serialized)
            if record is not None:
                records[flight_id] = record
                ranks[flight_id] = _stored_person_rank(serialized, record)
        return records, ranks

    def _write(self) -> None:
        directory = os.path.dirname(os.path.abspath(self._path))
        os.makedirs(directory, exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=".registry-", suffix=".json", dir=directory
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
            json.dump(
                {
                    "flights": {
                        flight_id: dict(
                            asdict(record),
                            _attribution_rank=self._attribution_ranks.get(flight_id, 0),
                        )
                        for flight_id, record in self._records.items()
                    }
                },
                state_file,
                sort_keys=True,
            )
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, self._path)


def _legacy_person_rank(record: FlightRecord) -> int:
    return 0 if record.person.key == UNKNOWN_PERSON.key else 1


def _stored_person_rank(value: object, record: FlightRecord) -> int:
    if isinstance(value, dict):
        rank = value.get("_attribution_rank")
        if isinstance(rank, int) and 0 <= rank <= 4:
            return rank
    return _legacy_person_rank(record)


def _source_value(value: object) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    return (
        value
        if value.startswith(_SOURCE_PREFIXES)
        else "{}{}".format(_DEFAULT_SOURCE_PREFIX, value)
    )


def _email_source_value(candidate: EmailItineraryCandidate) -> str:
    source_id, digest = candidate.evidence.identity
    return "mail:{}:{}".format(source_id, digest)


def _notes_from_hints(hints: dict) -> Tuple[str, ...]:
    excerpt = hints.get("notes_excerpt")
    notes = [excerpt] if isinstance(excerpt, str) and excerpt else []
    if notes and "your flight changed" in notes[0].casefold():
        notes.append(SCHEDULE_CHANGE_NOTE)
    return tuple(notes)


def _merge_legs(existing: FlightLeg, incoming: FlightLeg) -> FlightLeg:
    return FlightLeg(
        carrier=existing.carrier,
        number=existing.number,
        date=existing.date,
        origin=existing.origin or incoming.origin,
        dest=existing.dest or incoming.dest,
        sched_dep_iso=existing.sched_dep_iso or incoming.sched_dep_iso,
        sched_arr_iso=existing.sched_arr_iso or incoming.sched_arr_iso,
        conf_code=existing.conf_code or incoming.conf_code,
        seat=existing.seat or incoming.seat,
        operating_carrier=existing.operating_carrier or incoming.operating_carrier,
        operating_number=existing.operating_number or incoming.operating_number,
    )


def _backup_components(records: Iterable[FlightRecord]) -> List[List[FlightRecord]]:
    remaining = {record.flight_id: record for record in records}
    components: List[List[FlightRecord]] = []
    while remaining:
        _, seed = remaining.popitem()
        component = [seed]
        frontier = [seed]
        while frontier:
            current = frontier.pop()
            matches = [
                candidate
                for candidate in remaining.values()
                if _are_backup_options(current, candidate)
            ]
            for candidate in matches:
                del remaining[candidate.flight_id]
                component.append(candidate)
                frontier.append(candidate)
        components.append(component)
    return components


def _differs_by_conf(existing: FlightRecord, incoming: FlightRecord) -> bool:
    first, second = existing.leg.conf_code, incoming.leg.conf_code
    return bool(first) and bool(second) and first != second


def _are_backup_options(first: FlightRecord, second: FlightRecord) -> bool:
    first_conf, second_conf = first.leg.conf_code, second.leg.conf_code
    if first_conf and second_conf and first_conf == second_conf:
        # A shared confirmation code means legs of one itinerary (a connection),
        # never alternative bookings — do not mark them as backups.
        return False
    first_dep = first.leg.sched_dep_iso
    second_dep = second.leg.sched_dep_iso
    if first_dep is None and second_dep is None:
        return first.leg.origin is not None and first.leg.origin == second.leg.origin
    if first_dep is None or second_dep is None:
        return False
    try:
        first_time = datetime.fromisoformat(first_dep)
        second_time = datetime.fromisoformat(second_dep)
    except ValueError:
        return False
    return abs((first_time - second_time).total_seconds()) <= 4 * 60 * 60


def _departure_epoch(record: FlightRecord) -> float:
    departure = record.leg.sched_dep_iso
    if departure is not None:
        try:
            parsed = datetime.fromisoformat(departure)
            if parsed.tzinfo is not None:
                return parsed.timestamp()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(record.leg.date).replace(
            tzinfo=timezone.utc
        ).timestamp()
    except ValueError:
        return float("inf")


def _deserialize_record(flight_id: object, value: object) -> Optional[FlightRecord]:
    if not isinstance(flight_id, str) or not isinstance(value, dict):
        return None
    try:
        leg = FlightLeg(**value["leg"])
        person = PersonRef(**value["person"])
        return FlightRecord(
            flight_id=flight_id,
            leg=leg,
            person=person,
            sources=tuple(value.get("sources", ())),
            backup_group=value.get("backup_group"),
            status=value.get("status", "scheduled"),
            notes=tuple(value.get("notes", ())),
        )
    except (KeyError, TypeError):
        return None


def _unique(values: List[str]) -> List[str]:
    return list(dict.fromkeys(values))
