"""Read-modify-write helpers for config and the flight registry.

Everything here backs a ``clawflight`` subcommand, so a person — or the agent
acting for them — can set the whole thing up without opening a JSON file.

Two rules the callers rely on:

* **Nothing is written unless it changed.** Every mutator returns whether it
  actually altered anything, so a repeated "add Sam" is a no-op rather than a
  spurious rewrite.
* **Writes are atomic and private**, matching the rest of the state directory:
  temp file, ``0600``, ``os.replace``.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import CONFIG_FILENAME, ConfigError, loads
from .models import FlightLeg
from .parse import ParsedFlight, airport_timezone
from .people import PersonTable

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore


FLIGHT_RE = re.compile(r"^\s*([A-Z0-9]{2})\s*-?\s*(\d{1,4})\s*$", re.IGNORECASE)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
AIRPORT_RE = re.compile(r"^[A-Za-z]{3}$")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

MANUAL_SOURCE = "manual"


class ManageError(ValueError):
    """A user-facing problem with a manage command. The message is shown as-is."""


# --------------------------------------------------------------------------
# Config document
# --------------------------------------------------------------------------


def config_path_for(config) -> Path:
    """Where a mutation should be written for this config."""
    if config.source_path is not None:
        return Path(config.source_path)
    return config.state_dir / CONFIG_FILENAME


def read_document(path: Path) -> Tuple[Dict[str, Any], bool]:
    """Return the config as a plain dict, plus whether comments will be lost.

    The loader tolerates ``//`` and ``/* */`` comments, but writing back
    produces strict JSON. Callers surface that once rather than silently
    discarding somebody's notes.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError):
        return {}, False
    except OSError as exc:
        raise ManageError("could not read {}: {}".format(path, exc))
    try:
        document = loads(text)
    except ConfigError as exc:
        raise ManageError(str(exc))
    had_comments = bool(re.search(r"(?m)(^|\s)//|/\*", text))
    return document, had_comments


def write_document(path: Path, document: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".clawflight-config-", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _entries(document: Dict[str, Any], section: str) -> List[dict]:
    values = document.get(section)
    if not isinstance(values, list):
        values = []
        document[section] = values
    return [value for value in values if isinstance(value, dict)]


def _replace_section(document: Dict[str, Any], section: str, values: List[dict]) -> None:
    document[section] = values


# --------------------------------------------------------------------------
# People
# --------------------------------------------------------------------------


def add_person(
    document: Dict[str, Any],
    key: str,
    display: str,
    match_substrings: Optional[List[str]] = None,
    match_email_localparts: Optional[List[str]] = None,
    possessive_aliases: Optional[List[str]] = None,
) -> bool:
    key = _valid_key(key, "person key")
    if not display or not display.strip():
        raise ManageError("a person needs a display name (--name)")

    entry = {
        "key": key,
        "display": display.strip(),
        "match_substrings": _clean_list(match_substrings)
        or [display.strip().casefold()],
        "match_email_localparts": _clean_list(match_email_localparts),
    }
    aliases = _clean_list(possessive_aliases)
    if aliases:
        entry["possessive_aliases"] = aliases

    people = _entries(document, "people")
    for index, existing in enumerate(people):
        if existing.get("key") == key:
            if existing == entry:
                return False
            people[index] = entry
            _replace_section(document, "people", people)
            return True
    people.append(entry)
    _replace_section(document, "people", people)
    return True


def remove_person(document: Dict[str, Any], key: str) -> bool:
    key = _valid_key(key, "person key")
    people = _entries(document, "people")
    remaining = [entry for entry in people if entry.get("key") != key]
    if len(remaining) == len(people):
        raise ManageError("no person with key {!r}".format(key))
    _replace_section(document, "people", remaining)
    if document.get("owner") == key:
        # Leaving a dangling owner would make `doctor` fail with a confusing
        # error about a person that no longer exists.
        document.pop("owner", None)
    return True


# --------------------------------------------------------------------------
# Recipients
# --------------------------------------------------------------------------


def add_recipient(
    document: Dict[str, Any],
    key: str,
    name: str,
    channel: str,
    to: str,
    follow_all: bool = False,
    auto_follow_own: bool = True,
    active: bool = True,
    thread_id: Optional[str] = None,
) -> bool:
    key = _valid_key(key, "recipient key")
    if not name or not name.strip():
        raise ManageError("a recipient needs a name (--name)")
    if not channel or not channel.strip():
        raise ManageError("a recipient needs a channel (--channel)")
    if not to or not to.strip():
        raise ManageError("a recipient needs a target (--to)")

    destination = {"channel": channel.strip(), "to": to.strip()}
    if thread_id:
        destination["thread_id"] = str(thread_id).strip()
    entry = {
        "key": key,
        "name": name.strip(),
        "active": bool(active),
        "auto_follow_own": bool(auto_follow_own),
        "follow_all": bool(follow_all),
        "channel": destination,
    }

    recipients = _entries(document, "recipients")
    for index, existing in enumerate(recipients):
        if existing.get("key") == key:
            if existing == entry:
                return False
            recipients[index] = entry
            _replace_section(document, "recipients", recipients)
            return True
    recipients.append(entry)
    _replace_section(document, "recipients", recipients)
    return True


def remove_recipient(document: Dict[str, Any], key: str) -> bool:
    key = _valid_key(key, "recipient key")
    recipients = _entries(document, "recipients")
    remaining = [entry for entry in recipients if entry.get("key") != key]
    if len(remaining) == len(recipients):
        raise ManageError("no recipient with key {!r}".format(key))
    _replace_section(document, "recipients", remaining)
    return True


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

#: Settings reachable with ``clawflight config set``. Deliberately a short
#: allow-list: `people` and `recipients` have their own commands, and a typo in
#: a free-form dotted path would silently create a key nothing reads.
SETTABLE = {
    "owner": ("owner", str),
    "horizon_days": ("horizon_days", int),
    "state_dir": ("state_dir", str),
    "mailbox.adapter": ("mailbox.adapter", str),
    "mailbox.host": ("mailbox.host", str),
    "mailbox.port": ("mailbox.port", int),
    "mailbox.ssl": ("mailbox.ssl", bool),
    "mailbox.username": ("mailbox.username", str),
    "mailbox.password_env": ("mailbox.password_env", str),
    "mailbox.folder": ("mailbox.folder", str),
    "mailbox.path": ("mailbox.path", str),
    "mailbox.trusted_senders": ("mailbox.trusted_senders", list),
    "push.enabled": ("push.enabled", bool),
    "push.webhook_url": ("push.webhook_url", str),
    "push.rapidapi_key_env": ("push.rapidapi_key_env", str),
    "push.webhook_secret_env": ("push.webhook_secret_env", str),
    "push.receiver_port": ("push.receiver_port", int),
}

#: Settings whose value is a credential rather than a variable *name*.
_CREDENTIAL_SHAPED = re.compile(r"(?i)(password|secret|api[_-]?key|token)$")


def set_setting(document: Dict[str, Any], path: str, raw: str) -> bool:
    # Checked before the unknown-key test on purpose: somebody typing
    # `config set mailbox.password` needs to be told the secret model, not that
    # they guessed a key name wrong.
    if _CREDENTIAL_SHAPED.search(path) and not path.endswith("_env"):
        raise ManageError(
            "{} would hold a credential value. clawflight stores only the NAME "
            "of an environment variable — set {}_env to the variable name and "
            "put the secret in the environment.".format(path, path)
        )
    if path not in SETTABLE:
        raise ManageError(
            "unknown setting {!r}. Settable: {}".format(
                path, ", ".join(sorted(SETTABLE))
            )
        )
    _dotted, kind = SETTABLE[path]
    value = _coerce(raw, kind, path)

    target = document
    parts = path.split(".")
    for part in parts[:-1]:
        nested = target.get(part)
        if not isinstance(nested, dict):
            nested = {}
            target[part] = nested
        target = nested
    if target.get(parts[-1]) == value:
        return False
    target[parts[-1]] = value
    return True


def _coerce(raw: str, kind, path: str):
    if kind is bool:
        lowered = raw.strip().casefold()
        if lowered in ("true", "yes", "on", "1"):
            return True
        if lowered in ("false", "no", "off", "0"):
            return False
        raise ManageError("{} must be true or false, got {!r}".format(path, raw))
    if kind is int:
        try:
            return int(raw)
        except ValueError:
            raise ManageError("{} must be a whole number, got {!r}".format(path, raw))
    if kind is list:
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw


# --------------------------------------------------------------------------
# Flights
# --------------------------------------------------------------------------


def parse_flight_designator(value: str) -> Tuple[str, int]:
    match = FLIGHT_RE.match(value or "")
    if match is None:
        raise ManageError(
            "flight must look like DL767 or 'DL 767', got {!r}".format(value)
        )
    return match.group(1).upper(), int(match.group(2))


def build_flight(
    designator: str,
    date: str,
    origin: Optional[str] = None,
    dest: Optional[str] = None,
    depart: Optional[str] = None,
    arrive: Optional[str] = None,
    conf_code: Optional[str] = None,
    seat: Optional[str] = None,
    person: Optional[str] = None,
    people: Optional[PersonTable] = None,
) -> ParsedFlight:
    """Turn command-line arguments into a mergeable flight.

    Local clock times are resolved against the airport's timezone, exactly as
    the email and calendar parsers do, so a manually added flight enters the
    watch window on the same terms as an ingested one.
    """
    carrier, number = parse_flight_designator(designator)
    if not DATE_RE.match(date or ""):
        raise ManageError("--date must be YYYY-MM-DD, got {!r}".format(date))
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise ManageError("--date is not a real date: {!r}".format(date))

    origin = _airport(origin, "--from")
    dest = _airport(dest, "--to")
    if origin and dest and origin == dest:
        raise ManageError("--from and --to cannot be the same airport")

    hints: Dict[str, Any] = {"source_id": "{}:{}".format(MANUAL_SOURCE, date)}
    if person:
        person = person.strip().casefold()
        if people is not None and people.get(person) is None:
            known = ", ".join(item.key for item in people) or "none configured"
            raise ManageError(
                "unknown person {!r}. Configured people: {}".format(person, known)
            )
        # Name the person outright. Matching by display name would fail —
        # match_substrings holds how an *airline* spells them, which is rarely
        # a substring of the display name — and a human saying whose flight it
        # is outranks anything inferred anyway.
        hints["person_key"] = person

    return ParsedFlight(
        leg=FlightLeg(
            carrier=carrier,
            number=number,
            date=date,
            origin=origin,
            dest=dest,
            sched_dep_iso=_local_iso(date, depart, origin, "--depart"),
            sched_arr_iso=_local_iso(date, arrive, dest, "--arrive"),
            conf_code=conf_code.strip().upper() if conf_code else None,
            seat=seat.strip().upper() if seat else None,
        ),
        hints=hints,
    )


def _airport(value: Optional[str], flag: str) -> Optional[str]:
    if value is None or not value.strip():
        return None
    code = value.strip().upper()
    if not AIRPORT_RE.match(code):
        raise ManageError("{} must be a 3-letter IATA code, got {!r}".format(flag, value))
    return code


def _local_iso(
    date: str, clock: Optional[str], airport: Optional[str], flag: str
) -> Optional[str]:
    if clock is None or not clock.strip():
        return None
    match = TIME_RE.match(clock.strip())
    if match is None:
        raise ManageError("{} must be HH:MM (24-hour), got {!r}".format(flag, clock))
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ManageError("{} is not a real time: {!r}".format(flag, clock))
    if airport is None:
        raise ManageError(
            "{} needs the matching airport too, so the local time can be resolved "
            "to a real instant".format(flag)
        )
    zone = airport_timezone(airport)
    if zone is None:
        raise ManageError(
            "no timezone known for {}; supply a different airport or omit {}".format(
                airport, flag
            )
        )
    stamp = datetime.strptime("{} {:02d}:{:02d}".format(date, hour, minute), "%Y-%m-%d %H:%M")
    return stamp.replace(tzinfo=ZoneInfo(zone)).isoformat()


def _valid_key(value: str, label: str) -> str:
    key = (value or "").strip().casefold()
    if not KEY_RE.match(key):
        raise ManageError(
            "{} must be lowercase letters, digits, - or _, got {!r}".format(label, value)
        )
    if key == "unknown":
        raise ManageError("{!r} is reserved".format(key))
    return key


def _clean_list(values: Optional[List[str]]) -> List[str]:
    if not values:
        return []
    cleaned = []
    for value in values:
        for part in str(value).split(","):
            part = part.strip().casefold()
            if part and part not in cleaned:
                cleaned.append(part)
    return cleaned
