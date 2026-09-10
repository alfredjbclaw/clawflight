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
import shlex
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from . import __version__
from .adapters.channel_openclaw import DEFAULT_BINARY, binary_available, poster_router
from .adapters.mailbox import messages_to_candidates, messages_to_parsed_flights
from .adapters.feed_adsb import PublicFeeds, offline_observer
from .adapters.mailbox_mbox import MboxAdapter
from .airports import AIRPORTS_CSV, default_airports
from .audit import run_audit
from .config import Config, ConfigError, load_config, validate, with_state_dir
from . import manage
from .models import FlightRecord
from .monitor import Monitor
from .notify import DeliveryOutbox, FakePoster
from .recipients import FollowStore, resolve_recipients
from .registry import Registry
from .runner import run_once


TICK_CRON = "*/2 * * * *"
SWEEP_CRON = "17 * * * *"

#: What the console script is called when it is installed on PATH.
DEFAULT_COMMAND_NAME = "clawflight"


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
    tick.add_argument(
        "--offline",
        action="store_true",
        help="do not contact the public feeds; assess from stored state only",
    )

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

    # -- manual management -------------------------------------------------
    # Without these a new install is unusable: the only way in was to hand-edit
    # JSON and stand up an IMAP mailbox before anything happened at all.

    person = subparsers.add_parser("person", help="manage who can be attributed")
    person_verbs = person.add_subparsers(dest="verb", required=True)
    person_add = person_verbs.add_parser("add", help="add or replace a person")
    person_add.add_argument("key", help="short key, e.g. sam")
    person_add.add_argument("--name", required=True, help="display name")
    person_add.add_argument(
        "--match", action="append", default=[],
        help="name as an airline prints it; repeatable or comma-separated",
    )
    person_add.add_argument(
        "--email", action="append", default=[],
        help="calendar attendee local-part; repeatable",
    )
    person_add.add_argument("--alias", action="append", default=[], help="possessive alias")
    person_remove = person_verbs.add_parser("remove", help="remove a person")
    person_remove.add_argument("key")
    person_verbs.add_parser("list", help="list configured people")

    recipient = subparsers.add_parser("recipient", help="manage who gets alerts")
    recipient_verbs = recipient.add_subparsers(dest="verb", required=True)
    recipient_add = recipient_verbs.add_parser("add", help="add or replace a recipient")
    recipient_add.add_argument("key")
    recipient_add.add_argument("--name", required=True)
    recipient_add.add_argument("--channel", required=True, help="telegram, imessage, …")
    recipient_add.add_argument("--to", required=True, help="channel target")
    recipient_add.add_argument("--thread-id", help="optional thread/topic id")
    recipient_add.add_argument(
        "--follow-all", action="store_true", help="receive every flight, not just their own"
    )
    recipient_add.add_argument(
        "--no-auto-follow-own", action="store_true", help="do not auto-follow their own flights"
    )
    recipient_add.add_argument("--inactive", action="store_true", help="add but do not deliver")
    recipient_remove = recipient_verbs.add_parser("remove", help="remove a recipient")
    recipient_remove.add_argument("key")
    recipient_verbs.add_parser("list", help="list configured recipients")

    flight = subparsers.add_parser("flight", help="add or remove tracked flights by hand")
    flight_verbs = flight.add_subparsers(dest="verb", required=True)
    flight_add = flight_verbs.add_parser("add", help="track a flight without any email")
    flight_add.add_argument("designator", help="e.g. DL767")
    flight_add.add_argument("--date", required=True, help="YYYY-MM-DD, local departure date")
    flight_add.add_argument("--from", dest="origin", help="origin IATA code")
    flight_add.add_argument("--to", dest="dest", help="destination IATA code")
    flight_add.add_argument("--depart", help="local departure time, HH:MM")
    flight_add.add_argument("--arrive", help="local arrival time, HH:MM")
    flight_add.add_argument("--person", help="person key to attribute it to")
    flight_add.add_argument("--conf", help="confirmation code")
    flight_add.add_argument("--seat", help="seat, e.g. 12A")
    flight_remove = flight_verbs.add_parser("remove", help="stop tracking a flight")
    flight_remove.add_argument("flight_id")
    flight_verbs.add_parser("list", help="list tracked flights")

    config_command = subparsers.add_parser("config", help="inspect and change settings")
    config_verbs = config_command.add_subparsers(dest="verb", required=True)
    config_verbs.add_parser("show", help="print the resolved configuration")
    config_verbs.add_parser("path", help="print the config file path")
    config_set = config_verbs.add_parser("set", help="change one setting")
    config_set.add_argument("setting", help="e.g. owner, mailbox.adapter")
    config_set.add_argument("value")
    config_verbs.add_parser("keys", help="list settable keys")

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
        "person": _cmd_person,
        "recipient": _cmd_recipient,
        "flight": _cmd_flight,
        "config": _cmd_config,
    }
    return handlers[args.command](args, config)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def self_command() -> str:
    """How to invoke this program from a cron job.

    A bare ``clawflight`` is only correct when the console script is genuinely
    on PATH. When clawflight is running from the bundled ClawHub skill — where
    there is no install step at all — a cron job calling ``clawflight`` would
    simply fail, so emit the absolute path that is actually running.
    """
    argv_zero = sys.argv[0] if sys.argv and sys.argv[0] else ""
    if not argv_zero or argv_zero.endswith(("cli.py", "-c", "-m")):
        return DEFAULT_COMMAND_NAME
    try:
        resolved = Path(argv_zero).resolve()
    except OSError:  # pragma: no cover - defensive
        return DEFAULT_COMMAND_NAME
    if not resolved.is_file():
        return DEFAULT_COMMAND_NAME
    on_path = shutil.which(resolved.name)
    if on_path and Path(on_path).resolve() == resolved:
        # The console script is installed and reachable by name.
        return resolved.name
    return str(resolved)


def cron_recipes(config: Config) -> List[str]:
    """The two OpenClaw cron jobs a working install needs.

    Command payloads run with no model call, so an idle tick costs zero tokens.
    """
    command = shlex.quote(self_command())
    config_flag = (
        " --config {}".format(shlex.quote(str(config.source_path)))
        if config.source_path
        else ""
    )
    return [
        _recipe("clawflight-tick", TICK_CRON, "{}{} tick".format(command, config_flag)),
        _recipe("clawflight-sweep", SWEEP_CRON, "{}{} sweep".format(command, config_flag)),
    ]


def _recipe(name: str, schedule: str, inner_command: str) -> str:
    """One ``openclaw cron create`` line.

    ``--command`` carries a whole shell command line as a single argument, so
    it is quoted as a unit rather than wrapped in hand-written double quotes —
    a bundled skill can live under a path containing a space or a quote.
    """
    return (
        "openclaw cron create --name {} --cron {} --command {} "
        "--session isolated --delivery none".format(
            name, shlex.quote(schedule), shlex.quote(inner_command)
        )
    )


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
            # shlex, not split(): a bundled skill path may contain spaces.
            subprocess_runner(shlex.split(recipe), 60.0)
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
        offline_observer() if args.offline else PublicFeeds(),
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


# --------------------------------------------------------------------------
# Manual management
# --------------------------------------------------------------------------


def _mutate(args: argparse.Namespace, config: Config, apply):
    """Load the config document, apply a change, write it back if it changed."""
    path = manage.config_path_for(config)
    document, had_comments = manage.read_document(path)
    changed = apply(document)
    if changed:
        manage.write_document(path, document)
    notes = []
    if changed and had_comments:
        notes.append(
            "note: comments in {} were dropped when it was rewritten".format(path)
        )
    return path, changed, notes


def _manage_result(
    args: argparse.Namespace, path: Path, changed: bool, summary: str, notes
) -> int:
    payload = {"ok": True, "changed": changed, "config_path": str(path), "summary": summary}
    if notes:
        payload["notes"] = notes
    lines = [summary if changed else "{} (no change)".format(summary)]
    lines.extend(notes)
    _emit(args, payload, "\n".join(lines))
    return 0


def _manage_error(args: argparse.Namespace, exc: Exception) -> int:
    _emit(args, {"ok": False, "error": str(exc)}, "error: {}".format(exc))
    return 2


def _cmd_person(args: argparse.Namespace, config: Config) -> int:
    if args.verb == "list":
        rows = [
            {
                "key": person.key,
                "display": person.display,
                "match_substrings": list(person.match_substrings),
                "match_email_localparts": list(person.match_email_localparts),
                "is_owner": person.key == config.owner,
            }
            for person in config.people
        ]
        if args.json:
            _emit(args, {"people": rows}, "")
        elif not rows:
            print("No people configured. Add one:")
            print("  clawflight person add sam --name Sam --match 'sam kestrel'")
        else:
            for row in rows:
                marker = " (owner)" if row["is_owner"] else ""
                print("{:<12} {}{}".format(row["key"], row["display"], marker))
                if row["match_substrings"]:
                    print("             names: {}".format(", ".join(row["match_substrings"])))
        return 0

    try:
        if args.verb == "add":
            path, changed, notes = _mutate(
                args, config,
                lambda document: manage.add_person(
                    document, args.key, args.name, args.match, args.email, args.alias
                ),
            )
            summary = "person {} ({})".format(args.key, args.name)
        else:
            path, changed, notes = _mutate(
                args, config, lambda document: manage.remove_person(document, args.key)
            )
            summary = "removed person {}".format(args.key)
    except manage.ManageError as exc:
        return _manage_error(args, exc)
    return _manage_result(args, path, changed, summary, notes)


def _cmd_recipient(args: argparse.Namespace, config: Config) -> int:
    if args.verb == "list":
        rows = [
            {
                "key": recipient.key,
                "name": recipient.name,
                "channel": recipient.channel_id,
                "to": recipient.target,
                "follow_all": recipient.follow_all,
                "auto_follow_own": recipient.auto_follow_own,
                "deliverable": recipient.deliverable,
            }
            for recipient in config.recipients.all_active()
        ]
        if args.json:
            _emit(args, {"recipients": rows}, "")
        elif not rows:
            print("No recipients configured. Nothing would be delivered. Add one:")
            print(
                "  clawflight recipient add sam --name Sam "
                "--channel telegram --to '-1001234567890'"
            )
        else:
            for row in rows:
                scope = "all flights" if row["follow_all"] else "own flights"
                print(
                    "{:<12} {:<14} {} -> {}  [{}]".format(
                        row["key"], row["name"], row["channel"], row["to"], scope
                    )
                )
        return 0

    try:
        if args.verb == "add":
            path, changed, notes = _mutate(
                args, config,
                lambda document: manage.add_recipient(
                    document,
                    args.key,
                    args.name,
                    args.channel,
                    args.to,
                    follow_all=args.follow_all,
                    auto_follow_own=not args.no_auto_follow_own,
                    active=not args.inactive,
                    thread_id=args.thread_id,
                ),
            )
            summary = "recipient {} on {} -> {}".format(args.key, args.channel, args.to)
        else:
            path, changed, notes = _mutate(
                args, config, lambda document: manage.remove_recipient(document, args.key)
            )
            summary = "removed recipient {}".format(args.key)
    except manage.ManageError as exc:
        return _manage_error(args, exc)
    return _manage_result(args, path, changed, summary, notes)


def _cmd_flight(args: argparse.Namespace, config: Config) -> int:
    if args.verb == "list":
        return _cmd_status(args, config)

    config.ensure_state_dir()
    registry = Registry(str(config.registry_path), config.people)

    if args.verb == "remove":
        record = registry.get(args.flight_id)
        if record is None:
            return _manage_error(
                args, manage.ManageError("no tracked flight {!r}".format(args.flight_id))
            )
        removed = registry.forget(args.flight_id)
        Monitor(str(config.monitor_path)).forget(args.flight_id)
        payload = {"ok": True, "removed": removed, "flight_id": args.flight_id}
        _emit(args, payload, "removed {}".format(args.flight_id))
        return 0

    try:
        flight = manage.build_flight(
            args.designator,
            args.date,
            origin=args.origin,
            dest=args.dest,
            depart=args.depart,
            arrive=args.arrive,
            conf_code=args.conf,
            seat=args.seat,
            person=args.person,
            people=config.people,
        )
    except manage.ManageError as exc:
        return _manage_error(args, exc)

    report = registry.merge([flight])
    flight_ids = report["created"] + report["updated"]
    record = registry.get(flight_ids[0]) if flight_ids else None
    payload = {
        "ok": True,
        "created": report["created"],
        "updated": report["updated"],
        "backup_groups": report["backup_groups"],
        "traveler": record.person.name if record else None,
        "watched": bool(record and record.leg.sched_dep_iso),
    }
    lines = []
    for flight_id in report["created"]:
        lines.append("added {}".format(flight_id))
    for flight_id in report["updated"]:
        lines.append("updated {}".format(flight_id))
    if not lines:
        lines.append("no change")
    if record is not None:
        lines.append("traveler: {}".format(record.person.name))
        if not record.leg.sched_dep_iso:
            # Without a departure instant the watch window can never open, so
            # say so now rather than leaving someone waiting for alerts.
            lines.append(
                "warning: no departure time, so this flight will never be watched. "
                "Re-add with --from <IATA> --depart HH:MM."
            )
        if report["backup_groups"]:
            lines.append("flagged as a possible backup/duplicate booking")
    _emit(args, payload, "\n".join(lines))
    return 0


def _cmd_config(args: argparse.Namespace, config: Config) -> int:
    path = manage.config_path_for(config)

    if args.verb == "path":
        _emit(args, {"config_path": str(path), "exists": path.exists()}, str(path))
        return 0

    if args.verb == "keys":
        keys = sorted(manage.SETTABLE)
        _emit(args, {"settable": keys}, "\n".join(keys))
        return 0

    if args.verb == "show":
        payload = config.to_dict()
        payload["config_path"] = str(path)
        payload["config_exists"] = path.exists()
        if args.json:
            _emit(args, payload, "")
        else:
            print(json.dumps(payload, indent=2, sort_keys=False))
        return 0

    try:
        path, changed, notes = _mutate(
            args, config, lambda document: manage.set_setting(document, args.setting, args.value)
        )
    except manage.ManageError as exc:
        return _manage_error(args, exc)
    return _manage_result(args, path, changed, "{} = {}".format(args.setting, args.value), notes)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
