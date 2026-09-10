"""One monitoring pass: fetch, assess, compose, enqueue, deliver."""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Dict, List, Optional

from .connections import connection_alerts
from .models import Airport, FlightRecord, Observation
from .notify import DeliveryOutbox, Poster, classify, compose_post

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .monitor import Monitor
    from .registry import Registry


LANDING_TO_DONE_SECONDS = 1_800

#: Landing timestamps observed in THIS process. The monitor also persists a
#: landing epoch so a sweep in another process can promote landed -> done.
_LANDING_EPOCHS: Dict[str, float] = {}


def run_once(
    fetcher: Callable[[FlightRecord], Observation],
    registry: "Registry",
    monitor: "Monitor",
    airports: Dict[str, Airport],
    poster: Poster,
    now_epoch: float,
    horizon_days: int = 3,
    outbox: Optional[DeliveryOutbox] = None,
    arrival_ground_info: Optional[Callable[[FlightRecord], Optional[str]]] = None,
    recipients_for: Optional[Callable[[FlightRecord], List[str]]] = None,
    poster_for: Optional[Callable[[str], Optional[Poster]]] = None,
) -> dict:
    """Assess every upcoming flight once and return a bounded report.

    A failure fetching one flight never stops the others: it is recorded in
    ``errors`` and the pass continues.
    """
    records = registry.upcoming(now_epoch, horizon_days)
    report = {
        "checked": 0,
        "events": [],
        "posts": 0,
        "errors": [],
        "next_poll_seconds": 900,
    }
    poll_seconds: List[int] = []

    for record in records:
        report["checked"] += 1
        try:
            observation = fetcher(record)
            events = monitor.assess(record, observation, airports, now_epoch)
            for event in sorted(events, key=lambda event: classify(event) != "critical"):
                report["events"].append(
                    {"kind": event.kind, "flight_id": event.flight_id}
                )
                ground_info = (
                    arrival_ground_info(record)
                    if event.kind == "landing" and arrival_ground_info
                    else None
                )
                text = compose_post(event, record, ground_info)
                if outbox is not None:
                    _enqueue(outbox, event, record, text, recipients_for)
                elif poster.post(text):
                    report["posts"] += 1
                if event.kind == "landing":
                    registry.set_status(record.flight_id, "landed")
                    _LANDING_EPOCHS[record.flight_id] = event.at_epoch
            landing_epoch = _LANDING_EPOCHS.get(record.flight_id)
            if (
                record.status == "landed"
                and landing_epoch is not None
                and now_epoch >= landing_epoch + LANDING_TO_DONE_SECONDS
            ):
                registry.set_status(record.flight_id, "done")
                del _LANDING_EPOCHS[record.flight_id]
            poll_seconds.append(monitor.recommend_poll_seconds(record, now_epoch))
        except Exception as exc:  # noqa: BLE001 - one flight must not stop the pass
            report["errors"].append("{}: {}".format(record.flight_id, exc))

    # Connection alerts look across all records after the individual assessments.
    try:
        state = monitor.state_snapshot() if hasattr(monitor, "state_snapshot") else {}
        for event in connection_alerts(records, state, now_epoch):
            report["events"].append({"kind": event.kind, "flight_id": event.flight_id})
            record = next(
                (item for item in records if item.flight_id == event.flight_id), None
            )
            if record is None:
                continue
            text = compose_post(event, record)
            if outbox is not None:
                _enqueue(outbox, event, record, text, recipients_for)
            elif poster.post(text):
                report["posts"] += 1
    except Exception as exc:  # noqa: BLE001
        report["errors"].append("connection_alerts: {}".format(exc))

    if poll_seconds:
        report["next_poll_seconds"] = min(poll_seconds)
    if outbox is not None:
        delivery = outbox.deliver_pending(poster, now_epoch, poster_for=poster_for)
        report["posts"] += len(delivery["delivered"])
    return report


def _enqueue(
    outbox: DeliveryOutbox,
    event,
    record: FlightRecord,
    text: str,
    recipients_for: Optional[Callable[[FlightRecord], List[str]]],
) -> None:
    if recipients_for is None:
        outbox.enqueue(event, record, text=text)
        return
    recipients = recipients_for(record)
    if recipients:
        outbox.enqueue_for(event, record, recipients, text=text)
