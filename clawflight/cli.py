"""``clawflight`` command line: setup, tick, sweep, status, follow, mute, doctor.

Design notes that matter operationally:

* ``tick`` exits immediately when no flight is inside its watch window, so a
  two-minute OpenClaw cron costs nothing while nobody is flying.
* Every verb takes ``--config`` and ``--state-dir`` so a test or a second
  household can run fully isolated.
* ``setup`` prints the ``openclaw cron create`` commands rather than running
  them; ``--apply`` is opt-in and still shows what it will run.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence

from . import __version__
from .adapters.channel_openclaw import DEFAULT_BINARY, binary_available, poster_router
from .adapters.mailbox import messages_to_candidates, messages_to_parsed_flights
from .adapters.mailbox_mbox import MboxAdapter
from .airports import AIRPORTS_CSV, default_airports
from .audit import run_audit
from .config import Config, ConfigError, load_config, validate, with_state_dir
from .models import FlightRecord, Observation
from .monitor import Monitor
from .notify import DeliveryOutbox, FakePoster
from .recipients import FollowStore, resolve_recipients
from .registry import Registry
from .runner import run_once


TICK_CRON = "*/2 * * * *"
SWEEP_CRON = "17 * * * *"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clawflight",
        description="Family flight alerts from a forwarding address.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", help="path to clawflight.json")
    parser.add_argument("--state-dir", help="override the resolved state directory")
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON output"
    )
    subparsers = parser.add_subparsers(dest="command")

    setup = subparsers.add_parser("setup", help="print (or run) the cron recipes")
    setup.add_argument(
        "--apply", action="store_true", help="run the printed openclaw cron commands"
    )

    tick = subparsers.add_parser("tick", help="one poll pass over watched flights")
    tick.add_argument(
        "--dry-run", action="store_true", help="never deliver; use an in-memory poster"
    )
    tick.add_argument("--now", type=float, help="override the current epoch (testing)")

    sweep = subparsers.add_parser("sweep", help="ingest mail, prune, drain the outbox")
    sweep.add_argument(
        "--dry-run", action="store_true", help="never deliver; use an in-memory poster"
    )
    sweep.add_argument(
        "--mailbox", help="override the mailbox source, e.g. mbox:fixtures/inbox.mbox"
    )
    sweep.add_argument("--now", type=float, help="override the current epoch (testing)")

    subparsers.add_parser("status", help="show tracked flights")

    follow = subparsers.add_parser("follow", help="follow a flight for a recipient")
    follow.add_argument("flight_id")
    follow.add_argument("--recipient", help="recipient key (defaults to owner)")

    unfollow = subparsers.add_parser("unfollow", help="stop following a flight")
    unfollow.add_argument("flight_id")
    unfollow.add_argument("--recipient")

    mute = subparsers.add_parser("mute", help="mute a flight for a recipient")
    mute.add_argument("flight_id")
    mute.add_argument("--recipient")

    unmute = subparsers.add_parser("unmute", help="unmute a flight")
    unmute.add_argument("flight_id")
    unmute.add_argument("--recipient")

    subparsers.add_parser("doctor", help="validate config and audit state")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        _emit(args, {"ok": False, "error": str(exc)}, "config error: {}".format(exc))
        return 2
    if args.state_dir:
        config = with_state_dir(config, args.state_dir)

    handlers: Dict[str, Callable[[argparse.Namespace, Config], int]] = {
        "setup": _cmd_setup,
        "tick": _cmd_tick,
        "sweep": _cmd_sweep,
        "status": _cmd_status,
        "follow": _cmd_follow,
        "unfollow": _cmd_follow,
        "mute": _cmd_follow,
        "unmute": _cmd_follow,
        "doctor": _cmd_doctor,
    }
    return handlers[args.command](args, config)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cron_recipes(config: Config) -> List[str]:
    """The two OpenClaw cron jobs a working install needs.

    Command payloads run with no model call, so an idle tick costs zero tokens.
    """
    config_flag = (
        ' --config "{}"'.format(config.source_path) if config.source_path else ""
    )
    return [
        'openclaw cron create --name clawflight-tick --cron "{}" '
        '--command "clawflight{} tick" --session isolated --delivery none'.format(
            TICK_CRON, config_flag
        ),
        'openclaw cron create --name clawflight-sweep --cron "{}" '
        '--command "clawflight{} sweep" --session isolated --delivery none'.format(
            SWEEP_CRON, config_flag
        ),
    ]


def _cmd_setup(args: argparse.Namespace, config: Config) -> int:
    config.ensure_state_dir()
    recipes = cron_recipes(config)
    findings = validate(config)
    errors = [finding for finding in findings if finding.severity == "error"]
    payload = {
        "state_dir": str(config.state_dir),
        "config_path": str(config.source_path) if config.source_path else None,
        "cron": recipes,
        "applied": False,
        "findings": [
            {"severity": finding.severity, "message": finding.message}
            for finding in findings
        ],
    }
    lines = [
        "state dir: {}".format(config.state_dir),
        "",
        "Create these two OpenClaw cron jobs:",
    ]
    lines.extend("  {}".format(recipe) for recipe in recipes)
    if errors:
        lines.append("")
        lines.append("Config is not ready yet:")
        lines.extend("  ! {}".format(finding.message) for finding in errors)
    if args.apply:
        if errors:
            lines.append("")
            lines.append("Refusing to apply while configuration has errors.")
            _emit(args, payload, "\n".join(lines))
            return 1
        from .adapters.channel_openclaw import subprocess_runner

        for recipe in recipes:
            subprocess_runner(recipe.split(), 60.0)
        payload["applied"] = True
        lines.append("")
        lines.append("Applied.")
    _emit(args, payload, "\n".join(lines))
    return 1 if errors else 0


def _cmd_tick(args: argparse.Namespace, config: Config) -> int:
    now = args.now if args.now is not None else _now()
    config.ensure_state_dir()
    registry = Registry(str(config.registry_path), config.people)
    upcoming = registry.upcoming(now, config.horizon_days)
    if not _any_in_watch_window(upcoming, now):
        # The whole point of a 2-minute cadence: leave immediately when nothing
        # is live, so the cron job is free while nobody is flying.
        _emit(args, {"checked": 0, "idle": True, "events": []}, "idle")
        return 0

    monitor = Monitor(str(config.monitor_path))
    outbox = DeliveryOutbox(str(config.outbox_path))
    store = FollowStore(str(config.follows_path))
    poster = FakePoster()
    router = None if args.dry_run else poster_router(config.recipients)

    def recipients_for(record: FlightRecord) -> List[str]:
        return [
            recipient.key
            for recipient in resolve_recipients(record, config.recipients, store)
        ]

    report = run_once(
        _offline_fetcher(),
        registry,
        monitor,
        default_airports(),
        poster,
        now_epoch=now,
        horizon_days=config.horizon_days,
        outbox=None if args.dry_run else outbox,
        recipients_for=None if args.dry_run else recipients_for,
        poster_for=router,
    )
    report["idle"] = False
    _emit(args, report, json.dumps(report, sort_keys=True))
    return 0


def _cmd_sweep(args: argparse.Namespace, config: Config) -> int:
    now = args.now if args.now is not None else _now()
    config.ensure_state_dir()
    registry = Registry(str(config.registry_path), config.people)
    monitor = Monitor(str(config.monitor_path))
    outbox = DeliveryOutbox(str(config.outbox_path))

    adapter = _mailbox_adapter(args.mailbox, config)
    ingested = {"candidates": 0, "legs": 0, "skipped": 0, "created": [], "updated": []}
    if adapter is not None:
        messages = adapter.fetch(config.mailbox.max_messages)
        trusted = config.mailbox.trusted_senders
        candidates, skipped = messages_to_candidates(messages, trusted)
        parsed = messages_to_parsed_flights(
            messages, trusted, _year_of(now)
        )
        ingested["candidates"] = len(candidates)
        ingested["legs"] = len(parsed)
        ingested["skipped"] = len(skipped)
        if candidates:
            report = registry.merge_email_candidates(candidates)
            ingested["created"].extend(report["created"])
            ingested["updated"].extend(report["updated"])
        if parsed:
            report = registry.merge(parsed)
            ingested["created"].extend(report["created"])
            ingested["updated"].extend(report["updated"])

    promoted = []
    for flight_id in monitor.landed_awaiting_done(now):
        registry.set_status(flight_id, "done")
        promoted.append(flight_id)
    pruned_records = registry.prune_done(now)
    known = {record.flight_id for record in registry.all_records()}
    pruned_state = monitor.prune(known, now)
    pruned_outbox = outbox.prune(now)

    router = None if args.dry_run else poster_router(config.recipients)
    delivery = (
        {"delivered": [], "failed": [], "skipped": []}
        if args.dry_run
        else outbox.deliver_pending(FakePoster(), now, poster_for=router)
    )

    payload = {
        "ingested": ingested,
        "promoted": promoted,
        "pruned": {
            "records": pruned_records,
            "monitor": pruned_state,
            "outbox": pruned_outbox,
        },
        "delivery": delivery,
    }
    _emit(args, payload, json.dumps(payload, sort_keys=True))
    return 0


def _cmd_status(args: argparse.Namespace, config: Config) -> int:
    registry = Registry(str(config.registry_path), config.people)
    monitor_state = Monitor(str(config.monitor_path)).state_snapshot()
    rows = []
    for record in registry.all_records():
        rows.append(
            {
                "flight_id": record.flight_id,
                "flight": "{}{}".format(record.leg.carrier, record.leg.number),
                "date": record.leg.date,
                "route": "{}->{}".format(
                    record.leg.origin or "?", record.leg.dest or "?"
                ),
                "traveler": record.person.name,
                "status": record.status,
                "phase": monitor_state.get(record.flight_id, {}).get("phase", "-"),
                "backup_group": record.backup_group,
            }
        )
    if args.json:
        _emit(args, {"flights": rows}, "")
        return 0
    if not rows:
        print("No flights tracked yet.")
        return 0
    for row in rows:
        print(
            "{flight_id:<28} {flight:<8} {route:<12} {traveler:<14} "
            "{status:<10} {phase}".format(**row)
        )
    return 0


def _cmd_follow(args: argparse.Namespace, config: Config) -> int:
    recipient = args.recipient or config.owner
    if not recipient:
        _emit(
            args,
            {"ok": False, "error": "no recipient given and no owner configured"},
            "no recipient given and no owner configured",
        )
        return 2
    store = FollowStore(str(config.follows_path))
    getattr(store, args.command)(recipient, args.flight_id)
    payload = {
        "ok": True,
        "action": args.command,
        "recipient": recipient,
        "flight_id": args.flight_id,
        "followed": store.followed(recipient),
        "muted": store.muted(recipient),
    }
    _emit(
        args,
        payload,
        "{} {} for {}".format(args.command, args.flight_id, recipient),
    )
    return 0


def _cmd_doctor(args: argparse.Namespace, config: Config) -> int:
    findings = validate(config)
    report = run_audit(
        registry_path=str(config.registry_path),
        monitor_path=str(config.monitor_path),
        outbox_path=str(config.outbox_path),
        airports_csv_path=str(AIRPORTS_CSV),
    )
    cli_present = binary_available(DEFAULT_BINARY)
    errors = [finding for finding in findings if finding.severity == "error"]
    payload = {
        "ok": not errors,
        "config_path": str(config.source_path) if config.source_path else None,
        "state_dir": str(config.state_dir),
        "outbound_cli": {"binary": DEFAULT_BINARY, "available": cli_present},
        "config_findings": [
            {"severity": finding.severity, "message": finding.message}
            for finding in findings
        ],
        "audit_summary": report.summary,
        "audit_findings": [
            {
                "category": finding.category,
                "severity": finding.severity,
                "message": finding.message,
            }
            for finding in report.findings
        ],
    }
    lines = [
        "clawflight {}".format(__version__),
        "config:    {}".format(config.source_path or "(defaults)"),
        "state dir: {}".format(config.state_dir),
        "outbound:  {} {}".format(
            DEFAULT_BINARY, "found" if cli_present else "NOT FOUND on PATH"
        ),
        "",
        "Configuration",
    ]
    for finding in findings:
        prefix = {"error": "!!!", "warning": " ! ", "info": "   "}[finding.severity]
        lines.append("{} {}".format(prefix, finding.message))
    lines.append("")
    lines.append(report.text())
    _emit(args, payload, "\n".join(lines))
    return 1 if errors else 0


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _year_of(now_epoch: float) -> int:
    return datetime.fromtimestamp(now_epoch, timezone.utc).year


def _any_in_watch_window(records: List[FlightRecord], now_epoch: float) -> bool:
    from .monitor import WATCH_WINDOW_SECONDS, _epoch

    for record in records:
        departure = _epoch(record.leg.sched_dep_iso)
        if departure is None:
            # No known departure time: keep checking rather than silently
            # dropping a booking we could not fully parse.
            return True
        if departure - WATCH_WINDOW_SECONDS <= now_epoch <= departure + 2 * 3600:
            return True
    return False


def _offline_fetcher() -> Callable[[FlightRecord], Observation]:
    """Return a fetcher that yields position-free observations.

    Live feed access is deliberately not built into the CLI: the HTTP client is
    a caller-supplied seam (``run_once(fetcher, ...)``) so this package stays
    import-safe, offline, and free of a network dependency. A live fetcher
    builds its adsb.lol query from
    :func:`~clawflight.models.polling_callsign` and its FAA lookups from
    :func:`~clawflight.status.parse_faa_nas`; see ``docs/adapters.md``.

    With no positions the poll path still drives the watch window, schedule
    change notes, connection alerts, and push-corroborated delays.
    """

    def fetch(record: FlightRecord) -> Observation:
        return Observation(
            flight_id=record.flight_id,
            position=None,
            origin_delay=None,
            dest_delay=None,
            fetched_at_epoch=_now(),
        )

    return fetch


def _mailbox_adapter(override: Optional[str], config: Config):
    if override:
        scheme, _, value = override.partition(":")
        if scheme == "mbox" and value:
            return MboxAdapter(value)
        if scheme == "none":
            return None
        raise ConfigError("unsupported --mailbox override: {}".format(override))
    if config.mailbox.adapter == "mbox" and config.mailbox.path:
        return MboxAdapter(config.mailbox.path)
    if config.mailbox.adapter == "imap":
        from .adapters.mailbox_imap import ImapAdapter

        return ImapAdapter(
            config.mailbox.host,
            config.mailbox.username,
            password_env=config.mailbox.password_env,
            port=config.mailbox.port,
            ssl=config.mailbox.ssl,
            folder=config.mailbox.folder,
        )
    return None


def _emit(args: argparse.Namespace, payload: dict, text: str) -> None:
    if getattr(args, "json", False):
        sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    elif text:
        sys.stdout.write(text + "\n")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
