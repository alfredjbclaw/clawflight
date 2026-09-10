"""Message composition and the durable delivery outbox.

Composition is transport-neutral: it produces plain text. Delivery is a
``Poster`` protocol with one method, so any channel adapter satisfies it. The
outbox persists every event before the first delivery attempt and only marks an
entry delivered when the poster acknowledges it.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Optional

try:
    from typing import Literal, Protocol
except ImportError:  # pragma: no cover - Python < 3.8
    from typing_extensions import Literal, Protocol  # type: ignore

from .models import FlightEvent, FlightRecord
from .status import flightaware_link, fr24_link


NotificationPriority = Literal["critical", "info"]

#: Recipient key used when a caller does not route per person. Config-driven
#: deployments always pass a real recipient key from ``recipients``.
DEFAULT_RECIPIENT = "owner"

CRITICAL_KINDS = frozenset(
    {
        "delay",
        "gate_change",
        "cancelled",
        "schedule_change",
        "diverted",
        "landing_hold",
        "connection_alert",
    }
)


class Poster(Protocol):
    def post(self, text: str) -> bool: ...


class FakePoster:
    """In-memory poster for tests and dry runs."""

    def __init__(self) -> None:
        self.calls: List[str] = []

    def post(self, text: str) -> bool:
        self.calls.append(text)
        return True


def classify(event: FlightEvent) -> "NotificationPriority":
    return "critical" if event.kind in CRITICAL_KINDS else "info"


def compose_post(
    event: FlightEvent,
    record: FlightRecord,
    arrival_ground_info: Optional[str] = None,
) -> str:
    # These are deliberately single, bounded lifecycle messages. The monitor
    # emits each only once, which keeps travel-day context useful rather than
    # turning it into another stream of status chatter.
    if event.kind == "tracking_started":
        return compose_trip_card([record])
    if event.kind == "landing":
        return compose_arrival_post(record, ground_info=arrival_ground_info)
    leg = record.leg
    route = "{} -> {}".format(leg.origin or "?", leg.dest or "?")
    prefix = "🚨 " if classify(event) == "critical" else "✈️ "
    lines = [
        prefix + " ".join(event.message.split()),
        "Service date: " + leg.date,
        "Route: " + route,
        "Traveler: " + record.person.name,
    ]
    if leg.conf_code:
        lines.append("Confirmation: " + leg.conf_code)
    lines.extend(
        (flightaware_link(leg.carrier, leg.number), fr24_link(leg.carrier, leg.number))
    )
    return "\n".join(lines)


def compose_trip_card(records: List[FlightRecord], weather: Optional[str] = None) -> str:
    """One bounded pre-flight summary for a travel day."""
    if not records:
        return ""
    date = records[0].leg.date
    traveler = records[0].person.name
    lines = ["✈️ Travel Day — {} ({})".format(date, traveler)]
    for record in records:
        leg = record.leg
        flight_str = "{}{}".format(leg.carrier, leg.number)
        route = "{} -> {}".format(leg.origin or "?", leg.dest or "?")
        dep = _short_time(leg.sched_dep_iso)
        arr = _short_time(leg.sched_arr_iso)
        time_str = "{} - {}".format(dep, arr) if dep and arr else dep or arr or ""
        line = "  {} {} {}".format(flight_str, route, time_str).rstrip()
        if leg.conf_code:
            line += " [{}]".format(leg.conf_code)
        if leg.seat:
            line += " Seat {}".format(leg.seat)
        lines.append(line)
    if weather:
        lines.append("Weather: {}".format(weather[:120]))
    return "\n".join(lines)


def compose_arrival_post(
    record: FlightRecord,
    weather: Optional[str] = None,
    ground_info: Optional[str] = None,
) -> str:
    """Post-arrival welcome message.

    No routing to a home address is ever included: that feature is deliberately
    out of scope, and any ground context must be supplied by the caller.
    """
    leg = record.leg
    flight_str = "{}{}".format(leg.carrier, leg.number)
    lines = [
        "🛬 {} has arrived at {} ({})".format(
            flight_str, leg.dest or "?", record.person.name
        ),
        "Service date: {}".format(leg.date),
    ]
    if weather:
        lines.append("Local weather: {}".format(weather[:120]))
    if ground_info:
        lines.append("Ground: {}".format(ground_info[:120]))
    return "\n".join(lines)


def _short_time(iso: Optional[str]) -> str:
    if iso is None:
        return ""
    try:
        parsed = datetime.fromisoformat(iso)
        hour = parsed.hour % 12 or 12
        period = "AM" if parsed.hour < 12 else "PM"
        return "{}:{:02d} {}".format(hour, parsed.minute, period)
    except (ValueError, AttributeError):
        return ""


class MessageCap:
    """Rate limiter for per-flight, per-day message counts."""

    def __init__(self, max_info: int = 6, max_critical: int = 20) -> None:
        self._max_info = max_info
        self._max_critical = max_critical
        self._counts: Dict[str, Dict[str, int]] = {}

    def allow(self, flight_id: str, priority: "NotificationPriority") -> bool:
        bucket = self._counts.setdefault(flight_id, {"info": 0, "critical": 0})
        limit = self._max_critical if priority == "critical" else self._max_info
        if bucket[priority] >= limit:
            return False
        bucket[priority] += 1
        return True

    def reset(self, flight_id: Optional[str] = None) -> None:
        if flight_id is None:
            self._counts.clear()
        else:
            self._counts.pop(flight_id, None)


DeliveryState = Literal["pending", "failed", "acknowledged"]


@dataclass(frozen=True)
class OutboxEntry:
    delivery_id: str
    event_key: str
    flight_id: str
    text: str
    state: "DeliveryState"
    attempts: int
    created_at_epoch: float
    updated_at_epoch: float
    recipient: str = DEFAULT_RECIPIENT
    last_error: Optional[str] = None

    @property
    def retryable(self) -> bool:
        return self.state in ("pending", "failed")


class DeliveryOutbox:
    """Durable delivery outbox with per-recipient acknowledgement tracking.

    Events are persisted to JSON before any delivery attempt. Only entries the
    poster acknowledges are marked delivered. Undelivered entries survive
    restarts and resume on the next drain call with exponential backoff.
    """

    _BACKOFF_BASE_S = 30.0
    _BACKOFF_MAX_S = 1800.0

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._entries: Dict[str, OutboxEntry] = {}
        self._load()

    def enqueue(
        self,
        event: FlightEvent,
        record: FlightRecord,
        event_key: Optional[str] = None,
        text: Optional[str] = None,
        recipient: str = DEFAULT_RECIPIENT,
    ) -> OutboxEntry:
        key = event_key or _event_key(event)
        delivery_id = hashlib.sha256(
            "{}\0{}\0{}".format(record.flight_id, key, recipient).encode()
        ).hexdigest()[:24]
        with self._lock:
            existing = self._entries.get(delivery_id)
            if existing is not None:
                return existing
            entry = OutboxEntry(
                delivery_id=delivery_id,
                event_key=key,
                flight_id=record.flight_id,
                text=text if text is not None else compose_post(event, record),
                state="pending",
                attempts=0,
                created_at_epoch=event.at_epoch,
                updated_at_epoch=event.at_epoch,
                recipient=recipient,
            )
            self._entries[delivery_id] = entry
            self._write()
            return entry

    def enqueue_for(
        self,
        event: FlightEvent,
        record: FlightRecord,
        recipients: Iterable[str],
        text: Optional[str] = None,
    ) -> List[OutboxEntry]:
        entries: List[OutboxEntry] = []
        seen = set()
        for recipient in recipients:
            entry = self.enqueue(event, record, text=text, recipient=recipient)
            if entry.delivery_id not in seen:
                entries.append(entry)
                seen.add(entry.delivery_id)
        return entries

    def pending(self) -> List[OutboxEntry]:
        return sorted(
            [entry for entry in self._entries.values() if entry.retryable],
            key=lambda entry: entry.created_at_epoch,
        )

    def acknowledge(self, delivery_id: str, at_epoch: float) -> None:
        with self._lock:
            entry = self._entries.get(delivery_id)
            if entry is None:
                return
            self._entries[delivery_id] = replace(
                entry, state="acknowledged", updated_at_epoch=at_epoch
            )
            self._write()

    def fail(self, delivery_id: str, error: str, at_epoch: float) -> None:
        with self._lock:
            entry = self._entries.get(delivery_id)
            if entry is None:
                return
            self._entries[delivery_id] = replace(
                entry, state="failed", updated_at_epoch=at_epoch, last_error=error[:500]
            )
            self._write()

    def prune(self, now_epoch: float, max_age_days: int = 14) -> List[str]:
        """Remove acknowledged deliveries older than the retention window."""
        cutoff = now_epoch - max_age_days * 86400
        with self._lock:
            removed = [
                delivery_id
                for delivery_id, entry in self._entries.items()
                if entry.state == "acknowledged" and entry.updated_at_epoch < cutoff
            ]
            for delivery_id in removed:
                del self._entries[delivery_id]
            if removed:
                self._write()
            return removed

    def deliver_pending(
        self,
        poster: "Poster",
        now_epoch: float,
        *,
        poster_for: Optional[Callable[[str], Optional["Poster"]]] = None,
    ) -> dict:
        delivered: List[str] = []
        failed: List[str] = []
        skipped: List[str] = []
        for entry in self.pending():
            with self._lock:
                current = self._entries.get(entry.delivery_id)
                if current is None or not current.retryable:
                    continue
                # Exponential backoff: skip entries whose retry window has not
                # elapsed. The first attempt (attempts == 0) is always immediate.
                if current.attempts > 0:
                    backoff = min(
                        self._BACKOFF_BASE_S * (2 ** (current.attempts - 1)),
                        self._BACKOFF_MAX_S,
                    )
                    if now_epoch - current.updated_at_epoch < backoff:
                        skipped.append(current.delivery_id)
                        continue
                self._entries[entry.delivery_id] = replace(
                    current, attempts=current.attempts + 1, updated_at_epoch=now_epoch
                )
                self._write()
            try:
                entry_poster = (
                    poster_for(entry.recipient) if poster_for is not None else poster
                )
                if entry_poster is None:
                    self.fail(entry.delivery_id, "no poster for recipient", now_epoch)
                    failed.append(entry.delivery_id)
                    continue
                acknowledged = entry_poster.post(entry.text)
            except Exception as exc:  # noqa: BLE001 - adapters may raise anything
                self.fail(entry.delivery_id, str(exc), now_epoch)
                failed.append(entry.delivery_id)
                continue
            if acknowledged:
                self.acknowledge(entry.delivery_id, now_epoch)
                delivered.append(entry.delivery_id)
            else:
                self.fail(entry.delivery_id, "poster did not acknowledge", now_epoch)
                failed.append(entry.delivery_id)
        return {"delivered": delivered, "failed": failed, "skipped": skipped}

    def entries(self) -> List[OutboxEntry]:
        return list(self._entries.values())

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as source:
                payload = json.load(source)
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        deliveries = payload.get("deliveries")
        if not isinstance(deliveries, dict):
            return
        for delivery_id, data in deliveries.items():
            if not isinstance(data, dict):
                continue
            required = ("delivery_id", "event_key", "flight_id", "text")
            if any(field not in data for field in required):
                continue
            try:
                self._entries[delivery_id] = OutboxEntry(
                    delivery_id=data["delivery_id"],
                    event_key=data["event_key"],
                    flight_id=data["flight_id"],
                    text=data["text"],
                    state=data.get("state", "pending"),
                    attempts=data.get("attempts", 0),
                    created_at_epoch=data.get("created_at_epoch", 0.0),
                    updated_at_epoch=data.get("updated_at_epoch", 0.0),
                    recipient=data.get("recipient", DEFAULT_RECIPIENT),
                    last_error=data.get("last_error"),
                )
            except (TypeError, ValueError):
                continue

    def _write(self) -> None:
        directory = os.path.dirname(os.path.abspath(self._path))
        os.makedirs(directory, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".clawflight-outbox-", dir=directory)
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            json.dump(
                {
                    "deliveries": {
                        delivery_id: asdict(entry)
                        for delivery_id, entry in self._entries.items()
                    }
                },
                destination,
                separators=(",", ":"),
                sort_keys=True,
            )
        os.chmod(temporary, 0o600)
        os.replace(temporary, self._path)


Outbox = DeliveryOutbox


def _event_key(event: FlightEvent) -> str:
    return hashlib.sha256(
        "{}\0{}\0{}".format(
            event.flight_id, event.kind, " ".join(event.message.split())
        ).encode()
    ).hexdigest()
