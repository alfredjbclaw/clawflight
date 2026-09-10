"""Non-mutating health and data-quality audit.

Inspects delivery failures, airport/timezone gaps, parsing confidence, and
state corruption, and produces a human-reviewable report without altering any
state. This is what ``clawflight doctor`` prints after its config findings.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .airports import load_airports
from .parse import airport_timezone


SEVERITIES = ("critical", "warning", "info")


@dataclass
class AuditFinding:
    category: str
    severity: str  # "critical" | "warning" | "info"
    message: str


class AuditReport:
    def __init__(self) -> None:
        self.findings: List[AuditFinding] = []
        self.summary: Dict[str, int] = {"critical": 0, "warning": 0, "info": 0}

    def add(self, category: str, severity: str, message: str) -> None:
        self.findings.append(AuditFinding(category, severity, message))
        self.summary[severity] = self.summary.get(severity, 0) + 1

    def text(self) -> str:
        lines = ["clawflight audit report", "=" * 40]
        lines.append(
            "Findings: {} critical, {} warning, {} info".format(
                self.summary.get("critical", 0),
                self.summary.get("warning", 0),
                self.summary.get("info", 0),
            )
        )
        lines.append("")
        for finding in self.findings:
            prefix = {"critical": "!!!", "warning": " ! ", "info": "   "}.get(
                finding.severity, "   "
            )
            lines.append("{} [{}] {}".format(prefix, finding.category, finding.message))
        return "\n".join(lines)


def run_audit(
    registry_path: Optional[str] = None,
    monitor_path: Optional[str] = None,
    outbox_path: Optional[str] = None,
    airports_csv_path: Optional[str] = None,
    events_log_path: Optional[str] = None,
) -> AuditReport:
    """Run a complete non-mutating audit and return the report."""
    report = AuditReport()
    if registry_path:
        _audit_registry(report, registry_path)
    if monitor_path:
        _audit_monitor(report, monitor_path)
    if outbox_path:
        _audit_outbox(report, outbox_path)
    if airports_csv_path:
        _audit_airports(report, airports_csv_path)
    if events_log_path:
        _audit_events_log(report, events_log_path)
    return report


def _audit_registry(report: AuditReport, path: str) -> None:
    data = _load_json(path)
    if data is None:
        report.add(
            "registry", "warning", "Registry file missing or unreadable: {}".format(path)
        )
        return
    flights = data.get("flights", {})
    if not isinstance(flights, dict):
        report.add("registry", "critical", "Registry 'flights' is not a dict")
        return
    report.add("registry", "info", "{} flight records".format(len(flights)))
    for flight_id, record in flights.items():
        if not isinstance(record, dict):
            continue
        leg = record.get("leg", {})
        if not isinstance(leg, dict):
            continue
        origin = leg.get("origin")
        dest = leg.get("dest")
        if origin and airport_timezone(origin) is None:
            report.add(
                "airport",
                "warning",
                "Unknown timezone: {} (flight {})".format(origin, flight_id),
            )
        if dest and airport_timezone(dest) is None:
            report.add(
                "airport",
                "warning",
                "Unknown timezone: {} (flight {})".format(dest, flight_id),
            )
        if not leg.get("sched_dep_iso") and origin:
            report.add(
                "parsing",
                "warning",
                "Missing departure time: {} (origin {})".format(flight_id, origin),
            )
        person = record.get("person", {})
        if isinstance(person, dict) and person.get("key") == "unknown":
            report.add("attribution", "info", "Unattributed flight: {}".format(flight_id))


def _audit_monitor(report: AuditReport, path: str) -> None:
    data = _load_json(path)
    if data is None:
        report.add("monitor", "warning", "Monitor state missing: {}".format(path))
        return
    report.add("monitor", "info", "{} tracked flights".format(len(data)))
    now = datetime.now(timezone.utc).timestamp()
    for flight_id, state in data.items():
        if not isinstance(state, dict):
            continue
        if state.get("phase") == "landed":
            landed_epoch = state.get("landed_epoch")
            if isinstance(landed_epoch, (int, float)) and now - landed_epoch > 7200:
                report.add(
                    "monitor",
                    "warning",
                    "{} landed >2h ago, not promoted to done".format(flight_id),
                )


def _audit_outbox(report: AuditReport, path: str) -> None:
    data = _load_json(path)
    if data is None:
        report.add("outbox", "info", "No outbox file")
        return
    deliveries = data.get("deliveries", {})
    if not isinstance(deliveries, dict):
        return
    failed = sum(
        1
        for entry in deliveries.values()
        if isinstance(entry, dict) and entry.get("state") == "failed"
    )
    pending = sum(
        1
        for entry in deliveries.values()
        if isinstance(entry, dict) and entry.get("state") == "pending"
    )
    report.add("outbox", "info", "{} outbox entries".format(len(deliveries)))
    if failed > 0:
        report.add("outbox", "critical", "{} delivery failures".format(failed))
    if pending > 0:
        report.add("outbox", "warning", "{} pending deliveries".format(pending))


def _audit_airports(report: AuditReport, csv_path: str) -> None:
    try:
        airports = load_airports(csv_path)
    except OSError:
        report.add("airport", "warning", "Airport CSV not found: {}".format(csv_path))
        return
    report.add("airport", "info", "{} airports loaded".format(len(airports)))
    if len(airports) < 50:
        report.add("airport", "warning", "Small airport database ({})".format(len(airports)))


def _audit_events_log(report: AuditReport, path: str) -> None:
    if not os.path.exists(path):
        report.add("events", "info", "No events log")
        return
    try:
        with open(path, encoding="utf-8") as source:
            lines = source.readlines()
    except OSError:
        report.add("events", "warning", "Could not read events log")
        return
    report.add("events", "info", "{} event log entries".format(len(lines)))


def _load_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as source:
            data = json.load(source)
        return data if isinstance(data, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
