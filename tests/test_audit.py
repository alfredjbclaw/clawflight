"""The non-mutating health audit behind ``clawflight doctor``."""
import json
from pathlib import Path

from clawflight.airports import AIRPORTS_CSV
from clawflight.audit import AuditReport, run_audit


def _registry(**leg_overrides) -> dict:
    leg = {
        "carrier": "AA",
        "number": 4912,
        "date": "2026-07-11",
        "origin": "ASE",
        "dest": "DFW",
        "sched_dep_iso": "2026-07-11T12:51:00-06:00",
        "sched_arr_iso": "2026-07-11T16:10:00-05:00",
        "conf_code": "FAKE01",
        "seat": "10C",
    }
    leg.update(leg_overrides)
    return {
        "flights": {
            "AA4912-2026-07-11": {
                "flight_id": "AA4912-2026-07-11",
                "leg": leg,
                "person": {"key": "alex", "name": "Alex"},
                "status": "scheduled",
                "sources": [],
                "notes": [],
            }
        }
    }


def test_report_text_summarises_by_severity() -> None:
    report = AuditReport()
    report.add("test", "critical", "Something broke")
    report.add("test", "info", "All good")

    text = report.text()

    assert "1 critical" in text
    assert "1 info" in text
    assert "Something broke" in text


def test_a_healthy_registry_produces_only_informational_findings(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(_registry()))

    report = run_audit(registry_path=str(path))

    assert any("1 flight records" in f.message for f in report.findings)
    assert report.summary["critical"] == 0
    assert report.summary["warning"] == 0


def test_unknown_airports_and_missing_times_raise_warnings(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(_registry(origin="ZZZ", dest="YYY", sched_dep_iso=None)))

    report = run_audit(registry_path=str(path))

    warnings = [f for f in report.findings if f.severity == "warning"]
    assert any("ZZZ" in f.message and f.category == "airport" for f in warnings)
    assert any("YYY" in f.message and f.category == "airport" for f in warnings)
    assert any(f.category == "parsing" for f in warnings)


def test_unattributed_flights_are_reported_as_information(tmp_path: Path) -> None:
    payload = _registry()
    payload["flights"]["AA4912-2026-07-11"]["person"] = {"key": "unknown", "name": "Unknown"}
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(payload))

    report = run_audit(registry_path=str(path))

    assert any(f.category == "attribution" and f.severity == "info" for f in report.findings)


def test_a_malformed_registry_is_reported_rather_than_crashing(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"flights": "not-a-dict"}))

    report = run_audit(registry_path=str(path))

    assert any(f.severity == "critical" for f in report.findings)
    assert any("missing" in f.message.lower() for f in run_audit(
        registry_path="/nonexistent/registry.json"
    ).findings)


def test_a_flight_stuck_in_landed_is_flagged(tmp_path: Path) -> None:
    path = tmp_path / "monitor.json"
    path.write_text(
        json.dumps({"AA4912-2026-07-11": {"phase": "landed", "landed_epoch": 0.0}})
    )

    report = run_audit(monitor_path=str(path))

    assert any("not promoted to done" in f.message for f in report.findings)


def test_outbox_failures_and_backlog_are_surfaced(tmp_path: Path) -> None:
    path = tmp_path / "outbox.json"
    path.write_text(
        json.dumps(
            {
                "deliveries": {
                    "a": {"state": "failed", "delivery_id": "a", "text": "one"},
                    "b": {"state": "pending", "delivery_id": "b", "text": "two"},
                    "c": {"state": "acknowledged", "delivery_id": "c", "text": "three"},
                }
            }
        )
    )

    report = run_audit(outbox_path=str(path))

    assert any(
        "1 delivery failure" in f.message and f.severity == "critical" for f in report.findings
    )
    assert any("1 pending" in f.message and f.severity == "warning" for f in report.findings)
    assert any("No outbox file" in f.message for f in run_audit(
        outbox_path="/nonexistent/outbox.json"
    ).findings)


def test_the_packaged_airport_table_passes_the_size_check() -> None:
    report = run_audit(airports_csv_path=str(AIRPORTS_CSV))

    assert any("airports loaded" in f.message for f in report.findings)
    assert not any("Small airport database" in f.message for f in report.findings)


def test_a_small_or_missing_airport_table_is_flagged(tmp_path: Path) -> None:
    path = tmp_path / "airports.csv"
    path.write_text("iata,name,lat,lon,tz\nJFK,Kennedy,40.6,-73.7,America/New_York\n")

    small = run_audit(airports_csv_path=str(path))
    missing = run_audit(airports_csv_path=str(tmp_path / "nope.csv"))

    assert any("Small airport database" in f.message for f in small.findings)
    assert any("not found" in f.message for f in missing.findings)


def test_events_log_findings_cover_present_and_absent_logs(tmp_path: Path) -> None:
    path = tmp_path / "events.log"
    path.write_text("one\ntwo\n")

    present = run_audit(events_log_path=str(path))
    absent = run_audit(events_log_path=str(tmp_path / "nope.log"))

    assert any("2 event log entries" in f.message for f in present.findings)
    assert any("No events log" in f.message for f in absent.findings)


def test_the_audit_never_writes_to_the_files_it_reads(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    monitor_path = tmp_path / "monitor.json"
    registry_path.write_text(json.dumps(_registry()))
    monitor_path.write_text(json.dumps({"AA4912-2026-07-11": {"phase": "watch", "sent": []}}))
    before = (registry_path.read_text(), monitor_path.read_text())

    report = run_audit(registry_path=str(registry_path), monitor_path=str(monitor_path))

    assert (registry_path.read_text(), monitor_path.read_text()) == before
    assert report.summary["critical"] == 0


def test_an_audit_with_no_paths_reports_nothing() -> None:
    report = run_audit()

    assert report.findings == []
    assert report.summary == {"critical": 0, "warning": 0, "info": 0}
