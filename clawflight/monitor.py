"""Flight phase state machine, delay buckets, and push-update diffing.

``monitor.json`` is shared by the poll tick and (in push mode) the webhook
receiver, so every read-modify-write runs under an exclusive file lock and
reloads from disk inside it.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

from .airports import eta_epoch, gc_km, progress_fraction
from .models import (
    Airport,
    FlightEvent,
    FlightRecord,
    FlightUpdate,
    Observation,
    Position,
)


_LOGGER = logging.getLogger(__name__)

WATCH_WINDOW_SECONDS = 6 * 60 * 60
STALE_POSITION_SECONDS = 900
PUSH_QUIET_SECONDS = 1800
LANDING_GRACE_SECONDS = 1800.0


def ingest_push(update: FlightUpdate, state: Dict[str, object]) -> List[FlightEvent]:
    now_epoch = state.get("_now_epoch")
    timestamp = (
        float(now_epoch) if isinstance(now_epoch, (int, float)) else update_timestamp()
    )
    flight_id = state.get("flight_id")
    event_flight_id = flight_id if isinstance(flight_id, str) else update.flight_number
    events: List[FlightEvent] = []
    previous_status = state.get("status")
    status = update.status
    if (
        status is not None
        and status.casefold() in ("cancelled", "canceled")
        and previous_status != status
    ):
        events.append(
            FlightEvent(
                event_flight_id,
                "cancelled",
                "{} has been cancelled.".format(update.flight_number),
                True,
                timestamp,
            )
        )
        state["phase"] = "cancelled"
    if (
        status is not None
        and "divert" in status.casefold()
        and previous_status != status
    ):
        events.append(
            FlightEvent(
                event_flight_id,
                "diverted",
                "{} has been diverted.".format(update.flight_number),
                True,
                timestamp,
            )
        )
    origin_code = _state_string(state, "origin")
    dest_code = _state_string(state, "dest")
    times_phrase = _revised_times_phrase(update, origin_code, dest_code)
    announced_dep = _announced_list(state, "announced_dep_revisions")
    announced_arr = _announced_list(state, "announced_arr_revisions")
    previous_revised = state.get("departure_revised")
    revised_changed = (
        update.departure_revised is not None
        and previous_revised != update.departure_revised
    )
    departure_scheduled = update.departure_scheduled or _state_string(
        state, "departure_scheduled"
    )
    delay_minutes = _minutes_between(departure_scheduled, update.departure_revised)
    suppress_revision, hedge_revision = _revision_policy(
        update, state, timestamp, delay_minutes
    )
    revision_is_announced = update.departure_revised in announced_dep
    if (
        (suppress_revision or hedge_revision)
        and update.departure_revised is not None
        and not revision_is_announced
    ):
        logged_revisions = _anomaly_revisions(state)
        if update.departure_revised not in logged_revisions:
            _LOGGER.warning(
                "data_anomaly",
                extra={
                    "flight_id": event_flight_id,
                    "revised": update.departure_revised,
                    "sched": departure_scheduled,
                    "reason": "past_revision" if suppress_revision else "early_revision",
                },
            )
            logged_revisions.append(update.departure_revised)
    delay_bucket = _push_delay_bucket(delay_minutes)
    previous_bucket = state.get("push_delay_bucket")
    # Only announce a departure revision the first time we see that value, so an
    # A->B->A schedule flip does not re-alert on the return to A.
    dep_is_new = update.departure_revised is not None and not revision_is_announced
    if (
        not suppress_revision
        and revised_changed
        and delay_bucket > (previous_bucket if isinstance(previous_bucket, int) else 0)
    ):
        events.append(
            FlightEvent(
                event_flight_id,
                "delay",
                _with_times(
                    "{} departure is delayed by at least {} minutes.".format(
                        update.flight_number, delay_bucket
                    ),
                    times_phrase,
                ),
                True,
                timestamp,
            )
        )
        state["push_delay_bucket"] = delay_bucket
    if (
        not suppress_revision
        and dep_is_new
        and delay_minutes is not None
        and delay_minutes != 0
    ):
        if hedge_revision:
            message = _hedged_early_message(
                update.flight_number,
                update.departure_revised,
                departure_scheduled,
                delay_minutes,
            )
        else:
            difference = _normal_revision_difference(delay_minutes)
            message = _with_times(
                "{} departure schedule changed by {}.".format(
                    update.flight_number, difference
                ),
                times_phrase,
            )
        events.append(
            FlightEvent(event_flight_id, "schedule_change", message, True, timestamp)
        )
    if (
        not suppress_revision
        and update.departure_revised is not None
        and update.departure_revised not in announced_dep
    ):
        announced_dep.append(update.departure_revised)
    arrival_changed = (
        update.arrival_revised is not None
        and state.get("arrival_revised") != update.arrival_revised
    )
    arr_is_new = arrival_changed and update.arrival_revised not in announced_arr
    arrival_scheduled = update.arrival_scheduled or _state_string(
        state, "arrival_scheduled"
    )
    arrival_change_minutes = _minutes_between(arrival_scheduled, update.arrival_revised)
    if (
        arr_is_new
        and arrival_change_minutes is not None
        and abs(arrival_change_minutes) >= 15
    ):
        events.append(
            FlightEvent(
                event_flight_id,
                "schedule_change",
                _with_times(
                    "{} arrival schedule changed by {} minutes.".format(
                        update.flight_number, int(abs(arrival_change_minutes))
                    ),
                    times_phrase,
                ),
                True,
                timestamp,
            )
        )
    if update.arrival_revised is not None and update.arrival_revised not in announced_arr:
        announced_arr.append(update.arrival_revised)
    for field, label in (("departure_gate", "departure"), ("arrival_gate", "arrival")):
        previous = state.get(field)
        current = getattr(update, field)
        if current is not None and isinstance(previous, str) and previous != current:
            events.append(
                FlightEvent(
                    event_flight_id,
                    "gate_change",
                    _with_times(
                        "{} {} gate changed from {} to {}.".format(
                            update.flight_number, label, previous, current
                        ),
                        times_phrase,
                    ),
                    True,
                    timestamp,
                )
            )
    for field in (
        "status",
        "departure_scheduled",
        "departure_revised",
        "arrival_scheduled",
        "arrival_revised",
        "departure_terminal",
        "departure_gate",
        "arrival_terminal",
        "arrival_gate",
        "arrival_baggage_belt",
    ):
        value = getattr(update, field)
        if value is not None:
            state[field] = value
    state["flight_number"] = update.flight_number
    state["last_push_epoch"] = timestamp
    return events


def update_timestamp() -> float:
    return datetime.now().timestamp()


class Monitor:
    def __init__(self, state_path: str) -> None:
        self._state_path = state_path
        self._lock_path = state_path + ".lock"
        self._state = self._load_state()
        self._lock = threading.RLock()

    @contextlib.contextmanager
    def _cross_process_lock(self):
        """Serialize monitor.json access across processes and re-read state.

        The push receiver and the poll tick both open the same monitor.json in
        separate processes; without this each would write back its own stale
        in-memory copy and clobber the other.
        """
        if fcntl is None:  # pragma: no cover - non-POSIX fallback
            self._state = self._load_state()
            yield
            return
        directory = os.path.dirname(os.path.abspath(self._lock_path))
        os.makedirs(directory, exist_ok=True)
        handle = open(self._lock_path, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            self._state = self._load_state()
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def assess(
        self,
        record: FlightRecord,
        obs: Observation,
        airports: Dict[str, Airport],
        now_epoch: float,
    ) -> List[FlightEvent]:
        with self._lock, self._cross_process_lock():
            return self._assess(record, obs, airports, now_epoch)

    def _assess(
        self,
        record: FlightRecord,
        obs: Observation,
        airports: Dict[str, Airport],
        now_epoch: float,
    ) -> List[FlightEvent]:
        flight_number = "{}{}".format(record.leg.carrier, record.leg.number)
        state = self._state.get(record.flight_id)
        if state is None:
            pending = self._state.pop(flight_number, None)
            state = (
                pending
                if isinstance(pending, dict)
                else {
                    "phase": "scheduled",
                    "sent": [],
                    "delay_bucket": 0,
                    "last_position_epoch": None,
                    "last_progress": 0.0,
                }
            )
            self._state[record.flight_id] = state
        state["flight_id"] = record.flight_id
        state["flight_number"] = flight_number
        if record.leg.origin:
            state["origin"] = record.leg.origin
        if record.leg.dest:
            state["dest"] = record.leg.dest
        events: List[FlightEvent] = []
        if record.status in ("done", "cancelled") or state.get("phase") == "cancelled":
            state["phase"] = (
                record.status if record.status in ("done", "cancelled") else "cancelled"
            )
            self._write_state()
            return events
        phase = state.get("phase")
        if phase not in ("scheduled", "watch", "airborne", "halfway", "landed"):
            phase = "scheduled"
            state["phase"] = phase
        sent = state.get("sent")
        if not isinstance(sent, list):
            sent = []
            state["sent"] = sent
        departure = _epoch(record.leg.sched_dep_iso)
        if (
            phase == "scheduled"
            and departure is not None
            and now_epoch >= departure - WATCH_WINDOW_SECONDS
        ):
            phase = "watch"
            state["phase"] = phase
            state["watch_started_epoch"] = now_epoch
            _once(
                events,
                sent,
                record,
                "tracking_started",
                "Tracking started for {}.".format(_context(record)),
                False,
                now_epoch,
            )
            if record.backup_group:
                _once(
                    events,
                    sent,
                    record,
                    "backup_reminder",
                    "{} is a possible backup or alternative booking.".format(
                        _context(record)
                    ),
                    False,
                    now_epoch,
                )

        origin = airports.get(record.leg.origin) if record.leg.origin else None
        destination = airports.get(record.leg.dest) if record.leg.dest else None
        progress: Optional[float] = None
        if obs.position is not None:
            state["last_position_epoch"] = obs.position.ts_epoch
            if origin is not None and destination is not None:
                progress = progress_fraction(obs.position, origin, destination)
                state["last_progress"] = progress
            if _is_takeoff(obs.position) and phase == "watch":
                phase = "airborne"
                state["phase"] = phase
                local_time = _time_at(obs.position.ts_epoch, origin)
                suffix = " at {}".format(local_time) if local_time else ""
                _once(
                    events,
                    sent,
                    record,
                    "takeoff",
                    "{} is airborne{}.".format(_context(record), suffix),
                    True,
                    now_epoch,
                )
        if phase in ("airborne", "halfway") and progress is not None and progress >= 0.5:
            phase = "halfway"
            state["phase"] = phase
            if obs.position is not None:
                eta = (
                    eta_epoch(origin, destination, obs.position)
                    if origin is not None and destination is not None
                    else None
                )
                eta_time = _time_at(eta, destination) if eta is not None else ""
                suffix = " ETA {}.".format(eta_time) if eta_time else "."
                _once(
                    events,
                    sent,
                    record,
                    "halfway",
                    "{} is halfway to its destination{}".format(_context(record), suffix),
                    True,
                    now_epoch,
                )

        last_position = state.get("last_position_epoch")
        last_progress = state.get("last_progress")
        missing_long = (
            obs.position is None
            and isinstance(last_position, (int, float))
            and now_epoch - last_position > STALE_POSITION_SECONDS
        )
        near_destination = (
            obs.position is not None
            and destination is not None
            and obs.position.alt_ft is not None
            and obs.position.alt_ft < 3000
            and _distance(
                obs.position.lat, obs.position.lon, destination.lat, destination.lon
            )
            <= 100
        )
        inferred_landing = (
            missing_long
            and isinstance(last_progress, (int, float))
            and last_progress > 0.85
        )
        if phase in ("airborne", "halfway") and (near_destination or inferred_landing):
            phase = "landed"
            state["phase"] = phase
            # Persisted so the hourly sweep can promote landed->done even when
            # the landing was observed in another process.
            state["landed_epoch"] = now_epoch
            _once(
                events,
                sent,
                record,
                "landing",
                "{} has landed.".format(_context(record)),
                True,
                now_epoch,
            )

        if phase == "watch":
            bucket = _delay_bucket(departure, now_epoch, obs.origin_delay)
            previous = state.get("delay_bucket")
            previous = previous if isinstance(previous, int) else 0
            # Elapsed-time buckets alone produce false delay alerts for regional
            # or codeshare callsigns we can never match on ADS-B: no position
            # ever arrives, so "elapsed since scheduled departure" grows
            # unbounded. Only escalate when corroborated by a push
            # departure_revised or an FAA airport delay.
            faa_delay = isinstance(obs.origin_delay, dict) and obs.origin_delay.get(
                "type"
            ) in ("ground_delay", "ground_stop")
            corroborated = faa_delay or state.get("departure_revised") is not None
            if bucket > previous and corroborated:
                state["delay_bucket"] = bucket
                reason = _delay_reason(obs.origin_delay)
                detail = " due to {}".format(reason) if reason else ""
                events.append(
                    _event(
                        record,
                        "delay",
                        "{} is delayed by at least {} minutes{}.".format(
                            _context(record), bucket, detail
                        ),
                        True,
                        now_epoch,
                    )
                )
            elif bucket > previous and obs.position is None:
                _once(
                    events,
                    sent,
                    record,
                    "stale_data",
                    "No position data for {}; delay unconfirmed and awaiting "
                    "airline/FAA corroboration.".format(_context(record)),
                    False,
                    now_epoch,
                    "watch_no_position",
                )
        schedule_markers = [
            "schedule_change:{}".format(note)
            for note in record.notes
            if _is_schedule_change(note)
        ]
        unseen_schedule_markers = [
            marker for marker in schedule_markers if marker not in sent
        ]
        if unseen_schedule_markers:
            _once(
                events,
                sent,
                record,
                "schedule_change",
                "{} has a reported schedule change.".format(_context(record)),
                True,
                now_epoch,
                unseen_schedule_markers[0],
            )
            sent.extend(
                marker for marker in unseen_schedule_markers[1:] if marker not in sent
            )
        if phase in ("airborne", "halfway") and missing_long:
            _once(
                events,
                sent,
                record,
                "stale_data",
                "Position data for {} has been stale for over 15 minutes.".format(
                    _context(record)
                ),
                False,
                now_epoch,
            )
        destination_hold = isinstance(obs.dest_delay, dict) and obs.dest_delay.get(
            "type"
        ) in ("ground_delay", "ground_stop")
        if phase in ("airborne", "halfway") and destination_hold:
            _once(
                events,
                sent,
                record,
                "landing_hold",
                "{} may face an arrival hold due to a destination airport "
                "restriction.".format(_context(record)),
                True,
                now_epoch,
            )
        last_push = state.get("last_push_epoch")
        if phase == "watch" or phase in ("airborne", "halfway"):
            watch_started = state.get("watch_started_epoch")
            if phase == "watch" and not isinstance(watch_started, (int, float)):
                state["watch_started_epoch"] = now_epoch
                watch_started = now_epoch
            # Only meaningful once this flight has actually received a push.
            # In the default polling-only mode nothing is subscribed, so there
            # is no quiet push feed to warn about — warning anyway would fire a
            # false alert on every flight half an hour into its watch window.
            if (
                isinstance(last_push, (int, float))
                and now_epoch - last_push > PUSH_QUIET_SECONDS
            ):
                _once(
                    events,
                    sent,
                    record,
                    "push_stale",
                    "Push updates for {} have been quiet for over 30 minutes; "
                    "polling is active.".format(_context(record)),
                    False,
                    now_epoch,
                )
        self._write_state()
        return events

    def ingest_push(self, update: FlightUpdate, now_epoch: float) -> List[FlightEvent]:
        with self._lock, self._cross_process_lock():
            scheduled = update.departure_scheduled or update.departure_revised
            dated_id = (
                "{}-{}".format(update.flight_number, scheduled[:10])
                if scheduled is not None
                else None
            )
            if dated_id is not None:
                matching_state = self._state.setdefault(dated_id, {"flight_id": dated_id})
            else:
                # An undated push (no scheduled/revised time) cannot form a dated
                # key, so match an existing state by flight_number rather than
                # splitting off a bare-number key. Prefer the most recently
                # active match when several dates are tracked.
                matches = [
                    state
                    for state in self._state.values()
                    if state.get("flight_number") == update.flight_number
                ]
                matching_state = max(matches, key=_recency, default=None)
            state = (
                matching_state
                if matching_state is not None
                else self._state.setdefault(
                    update.flight_number, {"flight_id": update.flight_number}
                )
            )
            state["_now_epoch"] = now_epoch
            events = ingest_push(update, state)
            state.pop("_now_epoch", None)
            self._write_state()
            return events

    def ingest_push_for_bookings(
        self, update: FlightUpdate, bookings: "list[FlightRecord]", now_epoch: float
    ) -> List[FlightEvent]:
        """Diff one physical update once, then fan its events to every booking."""
        if not bookings:
            return []
        from .models import flight_ident

        instance_ids = {
            flight_ident(record.leg.carrier, record.leg.number, record.leg.date)
            for record in bookings
        }
        if len(instance_ids) != 1:
            raise ValueError("bookings must belong to one physical flight instance")
        instance_id = next(iter(instance_ids))
        leg = bookings[0].leg
        with self._lock, self._cross_process_lock():
            state = self._state.setdefault(instance_id, {"flight_id": instance_id})
            # Seed the route from the booking so a push-only deployment can name
            # the airport in its "New departure … (ASE local)" phrase before the
            # poll path has ever run for this flight.
            if leg.origin:
                state.setdefault("origin", leg.origin)
            if leg.dest:
                state.setdefault("dest", leg.dest)
            state["_now_epoch"] = now_epoch
            physical_events = ingest_push(update, state)
            state.pop("_now_epoch", None)
            self._write_state()
        return [
            FlightEvent(
                record.flight_id, event.kind, event.message, event.critical, event.at_epoch
            )
            for event in physical_events
            for record in sorted(bookings, key=lambda booking: booking.flight_id)
        ]

    def landed_awaiting_done(
        self, now_epoch: float, grace_seconds: float = LANDING_GRACE_SECONDS
    ) -> List[str]:
        """Flight ids that landed at least grace_seconds ago.

        The in-process landing->done timer in :mod:`clawflight.runner` is lost
        when the landing is observed in a different process than the sweep, so
        the landing epoch is persisted here for cross-process promotion.
        """
        with self._lock, self._cross_process_lock():
            ready: List[str] = []
            for flight_id, state in self._state.items():
                landed = state.get("landed_epoch")
                if (
                    state.get("phase") == "landed"
                    and isinstance(landed, (int, float))
                    and now_epoch - landed >= grace_seconds
                ):
                    ready.append(flight_id)
            return ready

    def arrival_baggage_belt(self, flight_id: str) -> Optional[str]:
        """Return the last vendor-observed belt for one dated flight, if any."""
        with self._lock, self._cross_process_lock():
            state = self._state.get(flight_id)
            return (
                _state_string(state, "arrival_baggage_belt")
                if isinstance(state, dict)
                else None
            )

    def forget(self, flight_id: str) -> bool:
        """Drop the tracking state for one flight.

        Paired with ``Registry.forget``: leaving the phase and "already sent"
        markers behind would silently suppress alerts if the same flight were
        added again later.
        """
        with self._lock, self._cross_process_lock():
            if flight_id not in self._state:
                return False
            del self._state[flight_id]
            self._write_state()
            return True

    def prune(
        self,
        known_flight_ids: "set[str]",
        now_epoch: float,
        max_age_seconds: float = 172800.0,
    ) -> List[str]:
        """Drop monitor state for flights absent from the registry and idle."""
        with self._lock, self._cross_process_lock():
            removed: List[str] = []
            for flight_id in list(self._state.keys()):
                if flight_id in known_flight_ids:
                    continue
                last_active = _recency(self._state[flight_id])
                if (
                    last_active == float("-inf")
                    or now_epoch - last_active > max_age_seconds
                ):
                    del self._state[flight_id]
                    removed.append(flight_id)
            if removed:
                self._write_state()
            return removed

    def recommend_poll_seconds(self, record: FlightRecord, now_epoch: float) -> int:
        if (
            record.status in ("done", "cancelled")
            or self._state.get(record.flight_id, {}).get("phase") == "cancelled"
        ):
            return 0
        if self._state.get(record.flight_id, {}).get("phase") in ("airborne", "halfway"):
            return 300
        departure = _epoch(record.leg.sched_dep_iso)
        if departure is None:
            return 900
        remaining = departure - now_epoch
        if remaining > 86400:
            return 3600
        if remaining > 21600:
            return 1800
        if remaining >= 3600:
            return 900
        return 300

    def state_snapshot(self) -> Dict[str, dict]:
        return {key: dict(value) for key, value in self._state.items()}

    def _load_state(self) -> Dict[str, dict]:
        try:
            with open(self._state_path, encoding="utf-8") as state_file:
                raw_state = json.load(state_file)
        except (
            FileNotFoundError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
        ):
            return {}
        if not isinstance(raw_state, dict):
            return {}
        return {
            key: value
            for key, value in raw_state.items()
            if isinstance(key, str) and isinstance(value, dict)
        }

    def _write_state(self) -> None:
        directory = os.path.dirname(os.path.abspath(self._state_path))
        os.makedirs(directory, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".clawflight-monitor-", dir=directory)
        with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
            json.dump(self._state, state_file, separators=(",", ":"), sort_keys=True)
        os.chmod(temporary, 0o600)
        os.replace(temporary, self._state_path)


def _once(
    events: List[FlightEvent],
    sent: list,
    record: FlightRecord,
    kind: str,
    message: str,
    critical: bool,
    at_epoch: float,
    marker: Optional[str] = None,
) -> None:
    sent_marker = marker or kind
    if sent_marker not in sent:
        events.append(_event(record, kind, message, critical, at_epoch))
        sent.append(sent_marker)


def _event(
    record: FlightRecord, kind: str, message: str, critical: bool, at_epoch: float
) -> FlightEvent:
    if len(message) > 200:
        message = message[:199].rstrip(". ") + "."
    return FlightEvent(record.flight_id, kind, message, critical, at_epoch)


def _epoch(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    if value.endswith("Z"):
        value = "{}+00:00".format(value[:-1])
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.timestamp() if parsed.tzinfo is not None else None


def _context(record: FlightRecord) -> str:
    return "{}{} ({}->{}, {})".format(
        record.leg.carrier,
        record.leg.number,
        record.leg.origin or "?",
        record.leg.dest or "?",
        record.person.name,
    )


def _is_takeoff(position: Position) -> bool:
    return position.alt_ft is not None and position.alt_ft >= 1000 and (
        (position.vert_rate_fpm is not None and position.vert_rate_fpm > 300)
        or (position.gs_kt is not None and position.gs_kt > 140)
    )


def _distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    return gc_km(lat1, lon1, lat2, lon2)


def _time_at(epoch: Optional[float], airport: Optional[Airport]) -> str:
    if epoch is None or airport is None:
        return ""
    try:
        current = datetime.fromtimestamp(epoch, ZoneInfo(airport.tz))
    except ZoneInfoNotFoundError:
        return ""
    hour, period = current.hour % 12 or 12, "AM" if current.hour < 12 else "PM"
    return "{}:{:02d} {} {}".format(
        hour, current.minute, period, current.tzname() or ""
    ).strip()


def _delay_bucket(
    departure: Optional[float], now_epoch: float, origin_delay: Optional[dict]
) -> int:
    faa_delay = isinstance(origin_delay, dict) and origin_delay.get("type") in (
        "ground_delay",
        "ground_stop",
    )
    elapsed = now_epoch - departure if departure is not None else 0
    if elapsed >= 10800:
        return 180
    if elapsed >= 5400:
        return 90
    if elapsed >= 2700:
        return 45
    return 20 if elapsed >= 1200 or faa_delay else 0


def _delay_reason(origin_delay: Optional[dict]) -> str:
    reason = origin_delay.get("reason") if isinstance(origin_delay, dict) else ""
    return reason if isinstance(reason, str) else ""


def _is_schedule_change(note: str) -> bool:
    normalized = note.casefold().replace("-", " ")
    return "schedule change" in normalized or "flight changed" in normalized


def _minutes_between(scheduled: Optional[str], revised: Optional[str]) -> Optional[float]:
    scheduled_epoch = _epoch(scheduled)
    revised_epoch = _epoch(revised)
    if scheduled_epoch is None or revised_epoch is None:
        return None
    return (revised_epoch - scheduled_epoch) / 60


def _push_delay_bucket(delay_minutes: Optional[float]) -> int:
    if delay_minutes is None:
        return 0
    if delay_minutes >= 180:
        return 180
    if delay_minutes >= 90:
        return 90
    if delay_minutes >= 45:
        return 45
    if delay_minutes >= 20:
        return 20
    return 0


def _revision_policy(
    update: FlightUpdate,
    state: Dict[str, object],
    now_epoch: float,
    delay_minutes: Optional[float],
) -> Tuple[bool, bool]:
    """Return whether to suppress a past revision or hedge an early one."""
    revised = update.departure_revised
    if revised is None or delay_minutes is None:
        return False, False

    # A revision after schedule is a delay even when its wall-clock value is
    # already past by the time the webhook reaches us.
    if delay_minutes > 0 or _has_departed(update, state):
        return False, False

    revised_epoch = _epoch(revised)
    if revised_epoch is not None and revised_epoch - now_epoch < -300:
        return True, False
    return False, delay_minutes < -45


def _has_departed(update: FlightUpdate, state: Dict[str, object]) -> bool:
    phase = _state_string(state, "phase")
    if phase is not None and phase.casefold() in ("airborne", "halfway", "landed", "done"):
        return True
    for status in (update.status, _state_string(state, "status")):
        if status is None:
            continue
        normalized = status.casefold().replace(" ", "").replace("-", "")
        if normalized in ("departed", "airborne", "enroute", "landed", "arrived"):
            return True
    return False


def _recency(state: Dict[str, object]) -> float:
    for key in ("last_push_epoch", "watch_started_epoch"):
        value = state.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return float("-inf")


def _state_string(state: Dict[str, object], key: str) -> Optional[str]:
    value = state.get(key)
    return value if isinstance(value, str) else None


def _announced_list(state: Dict[str, object], key: str) -> List[str]:
    value = state.get(key)
    if isinstance(value, list):
        return value
    announced = (
        [item for item in value if isinstance(item, str)]
        if isinstance(value, tuple)
        else []
    )
    state[key] = announced
    return announced


def _anomaly_revisions(state: Dict[str, object]) -> List[str]:
    value = state.get("data_anomaly_revisions")
    if isinstance(value, list):
        if all(isinstance(item, str) for item in value):
            state["data_anomaly_revisions"] = value
            return value
        strings = [item for item in value if isinstance(item, str)]
        state["data_anomaly_revisions"] = strings
        return strings
    announced = (
        [item for item in value if isinstance(item, str)]
        if isinstance(value, tuple)
        else []
    )
    state["data_anomaly_revisions"] = announced
    return announced


_EASTERN = "America/New_York"


def _local_and_reference(iso: Optional[str], code: Optional[str]) -> Optional[str]:
    """Render a revised time as ``HH:MM (CODE local) / HH:MM ET``.

    The ISO string already carries the airport-local offset, so local wall time
    needs no timezone table; the reference zone is derived by conversion.
    """
    if iso is None:
        return None
    normalized = "{}+00:00".format(iso[:-1]) if iso.endswith("Z") else iso
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    local = parsed.strftime("%H:%M")
    try:
        reference = parsed.astimezone(ZoneInfo(_EASTERN)).strftime("%H:%M")
    except ZoneInfoNotFoundError:
        reference = local
    label = "{} ({} local)".format(local, code) if code else local
    return "{} / {} ET".format(label, reference)


def _revised_times_phrase(
    update: FlightUpdate, origin: Optional[str], dest: Optional[str]
) -> str:
    parts = []
    departure = _local_and_reference(update.departure_revised, origin)
    if departure is not None:
        parts.append("New departure " + departure)
    arrival = _local_and_reference(update.arrival_revised, dest)
    if arrival is not None:
        parts.append("New arrival " + arrival)
    return " · ".join(parts)


def _with_times(message: str, phrase: str) -> str:
    return "{} {}".format(message, phrase) if phrase else message


def _hedged_early_message(
    flight_number: str,
    revised: Optional[str],
    scheduled: Optional[str],
    delay_minutes: float,
) -> str:
    return (
        "⚠️ The airline reports {} departing {} — {} earlier than the scheduled {}. "
        "Unconfirmed; the airline may have bad data."
    ).format(
        flight_number,
        _clock_time(revised),
        _duration(delay_minutes),
        _clock_time(scheduled),
    )


def _clock_time(value: Optional[str]) -> str:
    if value is None:
        return "unknown time"
    normalized = "{}+00:00".format(value[:-1]) if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return "unknown time"
    hour = parsed.hour % 12 or 12
    period = "AM" if parsed.hour < 12 else "PM"
    return "{}:{:02d} {}".format(hour, parsed.minute, period)


def _duration(minutes: float) -> str:
    total_seconds = max(1, round(abs(minutes) * 60))
    hours, remaining_seconds = divmod(total_seconds, 3600)
    whole_minutes, seconds = divmod(remaining_seconds, 60)
    parts = []
    if hours:
        parts.append("{}h".format(hours))
    if whole_minutes:
        parts.append("{}m".format(whole_minutes))
    if seconds:
        parts.append("{}s".format(seconds))
    return "".join(parts)


def _normal_revision_difference(minutes: float) -> str:
    seconds = max(1, round(abs(minutes) * 60))
    whole_minutes, remainder = divmod(seconds, 60)
    if remainder == 0:
        return "{} minutes".format(whole_minutes)
    if whole_minutes == 0:
        return "{} seconds".format(remainder)
    return "{} minutes {} seconds".format(whole_minutes, remainder)
