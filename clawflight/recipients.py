"""Recipient configuration and per-flight subscription routing.

A recipient carries a channel-agnostic destination: ``{"channel": "telegram",
"to": "-100…:topic:42"}``. Anything ``openclaw message send`` accepts is a valid
target, so the same config works for Telegram, iMessage, WhatsApp, Discord,
Slack, Signal, Matrix, and every channel plugin.

The default configuration is EMPTY. A fresh install notifies nobody until the
user names recipients.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional


@dataclass(frozen=True)
class Recipient:
    key: str
    name: str
    channel: Optional[Mapping[str, str]] = None
    active: bool = True
    auto_follow_own: bool = True
    follow_all: bool = False

    @property
    def channel_id(self) -> Optional[str]:
        return (self.channel or {}).get("channel")

    @property
    def target(self) -> Optional[str]:
        return (self.channel or {}).get("to")

    @property
    def deliverable(self) -> bool:
        return bool(self.channel_id and self.target)


class RecipientConfig:
    """Lookup of who should receive notifications for a given person key."""

    def __init__(self, path: Optional[str] = None) -> None:
        self._recipients: Dict[str, Recipient] = {}
        if path is not None and os.path.exists(path):
            self._load(path)

    @classmethod
    def from_entries(cls, entries: Optional[object]) -> "RecipientConfig":
        config = cls()
        config._ingest(entries)
        return config

    def for_person(self, person_key: str) -> List[Recipient]:
        recipient = self._recipients.get(person_key)
        return [recipient] if recipient is not None and recipient.active else []

    def all_active(self) -> List[Recipient]:
        return [
            recipient for recipient in self._recipients.values() if recipient.active
        ]

    def get(self, key: str) -> Optional[Recipient]:
        return self._recipients.get(key)

    def keys(self) -> List[str]:
        return list(self._recipients)

    def __len__(self) -> int:
        return len(self._recipients)

    def _load(self, path: str) -> None:
        try:
            with open(path, encoding="utf-8") as source:
                raw = json.load(source)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        self._ingest(raw.get("recipients"))

    def _ingest(self, entries: object) -> None:
        if not isinstance(entries, list):
            return
        for entry in entries:
            recipient = _recipient_from_entry(entry)
            if recipient is not None:
                self._recipients[recipient.key] = recipient


def _recipient_from_entry(entry: object) -> Optional[Recipient]:
    if not isinstance(entry, dict):
        return None
    key = entry.get("key")
    name = entry.get("name") or entry.get("display")
    if not isinstance(key, str) or not isinstance(name, str) or not key.strip():
        return None
    defaults = entry.get("defaults")
    if not isinstance(defaults, dict):
        defaults = {}
    channel = entry.get("channel")
    if not isinstance(channel, dict):
        channel = None
    else:
        channel = {
            str(field_name): str(value)
            for field_name, value in channel.items()
            if isinstance(value, (str, int))
        }
    return Recipient(
        key=key.strip(),
        name=name,
        channel=channel,
        active=bool(entry.get("active", defaults.get("active", True))),
        auto_follow_own=bool(
            entry.get("auto_follow_own", defaults.get("auto_follow_own", True))
        ),
        follow_all=bool(entry.get("follow_all", defaults.get("follow_all", False))),
    )


class FollowStore:
    """Durable per-recipient explicit follow and mute lists."""

    def __init__(self, path: str) -> None:
        self.path = os.fspath(path)

    def follow(self, key: str, flight_id: str) -> None:
        self._add(key, "followed", flight_id)

    def unfollow(self, key: str, flight_id: str) -> None:
        self._remove(key, "followed", flight_id)

    def mute(self, key: str, flight_id: str) -> None:
        self._add(key, "muted", flight_id)

    def unmute(self, key: str, flight_id: str) -> None:
        self._remove(key, "muted", flight_id)

    def followed(self, key: str) -> List[str]:
        return list(self._recipient(self._read(), key)["followed"])

    def muted(self, key: str) -> List[str]:
        return list(self._recipient(self._read(), key)["muted"])

    def _add(self, key: str, field_name: str, flight_id: str) -> None:
        data = self._read()
        values = self._recipient(data, key)[field_name]
        if flight_id not in values:
            values.append(flight_id)
            self._write(data)

    def _remove(self, key: str, field_name: str, flight_id: str) -> None:
        data = self._read()
        values = self._recipient(data, key)[field_name]
        if flight_id in values:
            values.remove(flight_id)
            self._write(data)

    def _read(self) -> Dict[str, object]:
        try:
            with open(self.path, encoding="utf-8") as source:
                raw = json.load(source)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {"recipients": {}}
        if not isinstance(raw, dict) or not isinstance(raw.get("recipients"), dict):
            return {"recipients": {}}

        recipients: Dict[str, object] = {}
        for key, entry in raw["recipients"].items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                continue
            recipients[key] = {
                "followed": _string_list(entry.get("followed")),
                "muted": _string_list(entry.get("muted")),
            }
        return {"recipients": recipients}

    @staticmethod
    def _recipient(data: Dict[str, object], key: str) -> Dict[str, List[str]]:
        recipients = data["recipients"]
        assert isinstance(recipients, dict)
        entry = recipients.get(key)
        if not isinstance(entry, dict):
            entry = {"followed": [], "muted": []}
            recipients[key] = entry
        return entry

    def _write(self, data: Dict[str, object]) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=".clawflight-follows-", dir=directory
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
                json.dump(data, destination, sort_keys=True)
                destination.flush()
                os.fsync(destination.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self.path)
        except BaseException:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
            raise


def resolve_recipients(
    record: object, config: RecipientConfig, store: FollowStore
) -> List[Recipient]:
    """Return active recipients subscribed to a flight record."""
    flight_id = record.flight_id
    person_key = record.person.key
    resolved: List[Recipient] = []
    seen = set()
    for recipient in config.all_active():
        if recipient.key in seen or flight_id in store.muted(recipient.key):
            continue
        if (
            recipient.follow_all
            or (recipient.auto_follow_own and person_key == recipient.key)
            or flight_id in store.followed(recipient.key)
        ):
            resolved.append(recipient)
            seen.add(recipient.key)
    return resolved


def _string_list(value: object) -> List[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item for item in value if isinstance(item, str)))
