"""Connection intelligence for multi-leg itineraries.

Detects tight or missed connections from the actual itinerary legs plus any
revised arrival/departure times the monitor has recorded. Alerts fire only when
a connection is genuinely at risk; no alternative flights are ever suggested,
because the engine has no evidence for them.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .models import FlightEvent, FlightRecord


MIN_CONNECTION_MINUTES = 45
TIGHT_CONNECTION_MINUTES = 75


def connection_alerts(
    records: List[FlightRecord],
    monitor_state: Dict[str, dict],
    now_epoch: float,
) -> List[FlightEvent]:
    """Check itinerary connections and return alerts for tight/missed ones.

    Legs are grouped by ``(person, confirmation code)``: a shared confirmation
    code is the only evidence that two legs are one itinerary.
    """
    events: List[FlightEvent] = []
    groups = _connection_groups(records)
    for legs in groups.values():
        if len(legs) < 2:
            continue
        sorted_legs = sorted(legs, key=lambda r: _departure_epoch(r, monitor_state))
        for index in range(len(sorted_legs) - 1):
            arriving = sorted_legs[index]
            departing = sorted_legs[index + 1]
            if arriving.leg.dest != departing.leg.origin:
                continue
            arr_epoch = _arrival_epoch(arriving, monitor_state)
            dep_epoch = _departure_epoch(departing, monitor_state)
            if arr_epoch is None or dep_epoch is None:
                continue
            gap_minutes = (dep_epoch - arr_epoch) / 60
            if gap_minutes < 0:
                message = (
                    "Missed connection at {}: {} arrives after {} departs "
                    "({}). Rebooking may be needed."
                ).format(
                    arriving.leg.dest or "?",
                    _flight_label(arriving),
                    _flight_label(departing),
                    arriving.person.name,
                )
                events.append(
                    FlightEvent(
                        arriving.flight_id, "connection_alert", message[:200], True, now_epoch
                    )
                )
            elif gap_minutes < MIN_CONNECTION_MINUTES:
                message = (
                    "Very tight connection at {}: only {} min between {} "
                    "arriving and {} departing ({})."
                ).format(
                    arriving.leg.dest or "?",
                    int(gap_minutes),
                    _flight_label(arriving),
                    _flight_label(departing),
                    arriving.person.name,
                )
                events.append(
                    FlightEvent(
                        arriving.flight_id, "connection_alert", message[:200], True, now_epoch
                    )
                )
            elif gap_minutes < TIGHT_CONNECTION_MINUTES:
                message = "Tight connection at {}: {} min between {} and {} ({}).".format(
                    arriving.leg.dest or "?",
                    int(gap_minutes),
                    _flight_label(arriving),
                    _flight_label(departing),
                    arriving.person.name,
                )
                events.append(
                    FlightEvent(
                        arriving.flight_id, "connection_alert", message[:200], False, now_epoch
                    )
                )
    return events


def _connection_groups(
    records: List[FlightRecord],
) -> Dict[Tuple[str, str], List[FlightRecord]]:
    groups: Dict[Tuple[str, str], List[FlightRecord]] = {}
    for record in records:
        conf = record.leg.conf_code
        if not conf:
            continue
        groups.setdefault((record.person.key, conf), []).append(record)
    return groups


def _flight_label(record: FlightRecord) -> str:
    return "{}{}".format(record.leg.carrier, record.leg.number)


def _parse_epoch(iso: Optional[str]) -> Optional[float]:
    if iso is None:
        return None
    normalized = "{}+00:00".format(iso[:-1]) if iso.endswith("Z") else iso
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed.timestamp() if parsed.tzinfo is not None else None


def _departure_epoch(record: FlightRecord, monitor_state: Dict[str, dict]) -> float:
    state = monitor_state.get(record.flight_id, {})
    revised = state.get("departure_revised")
    if revised:
        epoch = _parse_epoch(revised)
        if epoch is not None:
            return epoch
    epoch = _parse_epoch(record.leg.sched_dep_iso)
    return epoch if epoch is not None else float("inf")


def _arrival_epoch(record: FlightRecord, monitor_state: Dict[str, dict]) -> Optional[float]:
    state = monitor_state.get(record.flight_id, {})
    revised = state.get("arrival_revised")
    if revised:
        epoch = _parse_epoch(revised)
        if epoch is not None:
            return epoch
    return _parse_epoch(record.leg.sched_arr_iso)
