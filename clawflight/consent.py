"""Durable, transport-neutral consent policy for a complete itinerary.

The ledger deliberately knows nothing about message transports or vendor feeds.
A caller supplies schedule times and the current time, and receives prompt work
or a delivery classification in return.

Who counts as the owner is configuration, not code: pass ``owner_key`` when
constructing the ledger. The owner gets an early T-48 prompt; everyone else is
prompted once at T-24.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None


OPT_IN = "opt_in"
OPT_OUT = "opt_out"
STAGE_48H = "t48"
STAGE_24H = "t24"

_DECISIONS = (OPT_IN, OPT_OUT)
_MAX_KEY_CHARS = 160
_MAX_TRAVELER_CHARS = 64
_MAX_LEG_KEY_CHARS = 96
_MAX_LEGS = 32
_CONFIRMATION_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,31}$")


def itinerary_key(
    confirmation_code: Optional[str], fallback_key: Optional[str] = None
) -> str:
    """Return the shared ledger key for a confirmation or a caller-safe fallback.

    Confirmation codes are canonicalized so every leg in a booking maps to the
    same key. A fallback is namespaced to prevent it colliding with a
    confirmation code; its contents are caller-owned and should be a
    non-sensitive stable identifier.
    """
    if isinstance(confirmation_code, str) and confirmation_code.strip():
        code = confirmation_code.strip().upper()
        if not _CONFIRMATION_RE.fullmatch(code):
            raise ValueError("confirmation_code must be a bounded code")
        return "confirmation:" + code
    if fallback_key is None:
        raise ValueError("fallback_key is required without a confirmation code")
    return "fallback:" + _bounded_text(fallback_key, "fallback_key", _MAX_KEY_CHARS - 9)


@dataclass(frozen=True)
class ConsentPrompt:
    itinerary_key: str
    traveler_key: str
    stage: str
    due_at_epoch: float


@dataclass(frozen=True)
class ItineraryConsent:
    itinerary_key: str
    traveler_key: Optional[str]
    decision: Optional[str]
    decided_at_epoch: Optional[float]
    first_departure_epoch: Optional[float]
    final_arrival_epoch: float
    expires_at_epoch: float
    prompt_timestamps: Tuple[Tuple[str, float], ...]


@dataclass(frozen=True)
class DeliveryAccess:
    """Consent classification for a later delivery adapter.

    Baseline discovery and material alerts are deliberately always eligible.
    Only the deeper travel-day stream is gated by an unexpired opt-in.
    """

    baseline_alerts: bool
    deep_travel_day_events: bool
    decision: Optional[str]
    expires_at_epoch: Optional[float]
    expired: bool


class ConsentLedger:
    """Atomically persisted itinerary consent and prompt scheduling state."""

    def __init__(self, path: str, owner_key: str = "") -> None:
        self._path = path
        self._owner_key = (owner_key or "").strip().casefold()
        self._entries = self._load()

    @property
    def owner_key(self) -> str:
        return self._owner_key

    def prompts_due(
        self,
        *,
        itinerary_key: str,
        traveler_key: str,
        first_departure_epoch: float,
        final_arrival_epoch: float,
        now_epoch: float,
        expires_at_epoch: Optional[float] = None,
        leg_key: Optional[str] = None,
    ) -> Tuple[ConsentPrompt, ...]:
        """Claim and return prompt stages due at ``now_epoch``.

        Claiming stores each returned stage's timestamp before returning, making
        repeated scheduler runs idempotent. The T-48 stage is superseded once
        the T-24 window begins, so a late first run never emits two prompts
        together. Calls upsert one leg's schedule; supply ``leg_key`` when a
        reschedule may change that leg's departure time.
        """
        key = _bounded_text(itinerary_key, "itinerary_key", _MAX_KEY_CHARS)
        traveler = _bounded_text(
            traveler_key, "traveler_key", _MAX_TRAVELER_CHARS
        ).casefold()
        departure = _epoch(first_departure_epoch, "first_departure_epoch")
        arrival = _epoch(final_arrival_epoch, "final_arrival_epoch")
        now = _epoch(now_epoch, "now_epoch")
        explicit_expiry = _optional_epoch(expires_at_epoch, "expires_at_epoch")
        stable_leg_key = (
            _schedule_key(departure)
            if leg_key is None
            else _bounded_text(leg_key, "leg_key", _MAX_LEG_KEY_CHARS)
        )
        if arrival < departure:
            raise ValueError(
                "final_arrival_epoch must not precede first_departure_epoch"
            )

        # Refresh under the same inter-process lock used for the write. Atomic
        # replacement protects the JSON file itself; this lock additionally makes
        # the read/check/claim sequence atomic across separately loaded ledgers.
        with self._locked_refresh(exclusive=True):
            entry, changed = self._ensure_entry(
                key, traveler, departure, arrival, explicit_expiry, stable_leg_key
            )
            itinerary_departure = entry["first_departure_epoch"]
            effective_expiry = _effective_expiry(entry)
            decision_is_active = (
                entry.get("decision") in _DECISIONS and now < effective_expiry
            )
            prompts = entry["prompts"]
            due = []
            remaining = itinerary_departure - now
            if not decision_is_active and 0 < remaining <= 24 * 3600:
                if STAGE_24H not in prompts:
                    due.append(
                        ConsentPrompt(
                            key, traveler, STAGE_24H, itinerary_departure - 24 * 3600
                        )
                    )
            elif (
                not decision_is_active
                and self._owner_key
                and traveler == self._owner_key
                and 24 * 3600 < remaining <= 48 * 3600
                and STAGE_48H not in prompts
            ):
                due.append(
                    ConsentPrompt(
                        key, traveler, STAGE_48H, itinerary_departure - 48 * 3600
                    )
                )

            for prompt in due:
                prompts[prompt.stage] = now
                changed = True
            if changed:
                self._write()
        return tuple(due)

    def record_decision(
        self,
        *,
        itinerary_key: str,
        decision: str,
        now_epoch: float,
        final_arrival_epoch: float,
        expires_at_epoch: Optional[float] = None,
    ) -> ItineraryConsent:
        """Persist an opt-in or opt-out through arrival or an earlier expiry."""
        key = _bounded_text(itinerary_key, "itinerary_key", _MAX_KEY_CHARS)
        if decision not in _DECISIONS:
            raise ValueError("decision must be opt_in or opt_out")
        now = _epoch(now_epoch, "now_epoch")
        arrival = _epoch(final_arrival_epoch, "final_arrival_epoch")
        explicit_expiry = _optional_epoch(expires_at_epoch, "expires_at_epoch")
        with self._locked_refresh(exclusive=True):
            entry = self._entries.get(key)
            if entry is None:
                entry = {
                    "traveler_key": None,
                    "first_departure_epoch": None,
                    "final_arrival_epoch": arrival,
                    "explicit_expiry_epoch": explicit_expiry,
                    "decision": None,
                    "decided_at_epoch": None,
                    "prompts": {},
                    "legs": {},
                }
                self._entries[key] = entry
            else:
                first_departure = entry.get("first_departure_epoch")
                if first_departure is not None and arrival < first_departure:
                    raise ValueError(
                        "final_arrival_epoch must not precede first_departure_epoch"
                    )
                legs = entry.get("legs", {})
                if legs and arrival < max(
                    leg["departure_epoch"] for leg in legs.values()
                ):
                    raise ValueError(
                        "final_arrival_epoch must not precede a leg departure"
                    )
                _set_authoritative_arrival(entry, arrival)
                # Expiry belongs to this decision, not to the itinerary forever.
                # Omitting it restores the final-arrival default; a supplied
                # value replaces (and may extend) the prior explicit expiry.
                entry["explicit_expiry_epoch"] = explicit_expiry
            entry["decision"] = decision
            entry["decided_at_epoch"] = now
            self._write()
            return _state(key, entry)

    def get(self, itinerary_key: str) -> Optional[ItineraryConsent]:
        """Return persisted state, including historical (possibly expired) decisions."""
        key = _bounded_text(itinerary_key, "itinerary_key", _MAX_KEY_CHARS)
        with self._locked_refresh(exclusive=False):
            entry = self._entries.get(key)
            return None if entry is None else _state(key, entry)

    def delivery_access(self, itinerary_key: str, *, now_epoch: float) -> DeliveryAccess:
        """Classify baseline and deep-event eligibility without side effects."""
        key = _bounded_text(itinerary_key, "itinerary_key", _MAX_KEY_CHARS)
        now = _epoch(now_epoch, "now_epoch")
        with self._locked_refresh(exclusive=False):
            entry = self._entries.get(key)
            if entry is None:
                return DeliveryAccess(True, False, None, None, False)
            expiry = _effective_expiry(entry)
            stored_decision = entry.get("decision")
            expired = stored_decision in _DECISIONS and now >= expiry
            effective_decision = None if expired else stored_decision
            return DeliveryAccess(
                baseline_alerts=True,
                deep_travel_day_events=effective_decision == OPT_IN,
                decision=effective_decision,
                expires_at_epoch=expiry,
                expired=expired,
            )

    @contextmanager
    def _locked_refresh(self, *, exclusive: bool):
        directory = os.path.dirname(os.path.abspath(self._path))
        os.makedirs(directory, exist_ok=True)
        if fcntl is None:  # pragma: no cover - non-POSIX fallback
            self._entries = self._load()
            yield
            return
        lock_path = self._path + ".lock"
        with open(lock_path, "a+b") as lock_file:
            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(lock_file.fileno(), operation)
            try:
                self._entries = self._load()
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _ensure_entry(
        self,
        key: str,
        traveler: str,
        departure: float,
        arrival: float,
        explicit_expiry: Optional[float],
        leg_key: str,
    ) -> Tuple[dict, bool]:
        entry = self._entries.get(key)
        if entry is None:
            entry = {
                "traveler_key": traveler,
                "first_departure_epoch": departure,
                "final_arrival_epoch": arrival,
                "explicit_expiry_epoch": explicit_expiry,
                "decision": None,
                "decided_at_epoch": None,
                "prompts": {},
                "legs": {
                    leg_key: {"departure_epoch": departure, "arrival_epoch": arrival}
                },
            }
            self._entries[key] = entry
            return entry, True
        changed = False
        if entry.get("traveler_key") is None:
            entry["traveler_key"] = traveler
            changed = True
        elif entry["traveler_key"] != traveler:
            raise ValueError("an itinerary cannot have multiple traveler keys")
        legs = entry.setdefault("legs", {})
        if not legs and entry.get("first_departure_epoch") is not None:
            stored_departure = entry["first_departure_epoch"]
            legs[_schedule_key(stored_departure)] = {
                "departure_epoch": stored_departure,
                "arrival_epoch": entry["final_arrival_epoch"],
            }
            changed = True
        scheduled_leg = {"departure_epoch": departure, "arrival_epoch": arrival}
        if leg_key not in legs and len(legs) >= _MAX_LEGS:
            raise ValueError(
                "an itinerary cannot contain more than {} legs".format(_MAX_LEGS)
            )
        if legs.get(leg_key) != scheduled_leg:
            legs[leg_key] = scheduled_leg
            changed = True
        stored_departure = entry.get("first_departure_epoch")
        stored_arrival = entry["final_arrival_epoch"]
        _recompute_schedule(entry)
        if (
            stored_departure != entry["first_departure_epoch"]
            or stored_arrival != entry["final_arrival_epoch"]
        ):
            changed = True
        if explicit_expiry is not None:
            stored_expiry = entry.get("explicit_expiry_epoch")
            bounded_expiry = (
                explicit_expiry
                if stored_expiry is None
                else min(stored_expiry, explicit_expiry)
            )
            if stored_expiry != bounded_expiry:
                entry["explicit_expiry_epoch"] = bounded_expiry
                changed = True
        return entry, changed

    def _load(self) -> Dict[str, dict]:
        try:
            with open(self._path, "r", encoding="utf-8") as ledger_file:
                payload = json.load(ledger_file)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict) or payload.get("version") != 1:
            return {}
        serialized = payload.get("itineraries")
        if not isinstance(serialized, dict):
            return {}
        entries = {}
        for key, value in serialized.items():
            entry = _valid_entry(key, value)
            if entry is not None:
                entries[key] = entry
        return entries

    def _write(self) -> None:
        directory = os.path.dirname(os.path.abspath(self._path))
        os.makedirs(directory, exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=".clawflight-consent-", suffix=".json", dir=directory
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as ledger_file:
                json.dump(
                    {"version": 1, "itineraries": self._entries},
                    ledger_file,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                ledger_file.flush()
                os.fsync(ledger_file.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self._path)
        except BaseException:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
            raise


def _bounded_text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError("{} must be a string".format(label))
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ValueError("{} must be non-empty and bounded".format(label))
    return normalized


def _epoch(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("{} must be a finite epoch".format(label))
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError("{} must be a finite epoch".format(label))
    return converted


def _optional_epoch(value: object, label: str) -> Optional[float]:
    return None if value is None else _epoch(value, label)


def _effective_expiry(entry: dict) -> float:
    explicit = entry.get("explicit_expiry_epoch")
    arrival = entry["final_arrival_epoch"]
    return arrival if explicit is None else min(arrival, explicit)


def _schedule_key(departure: float) -> str:
    return "departure:{:.6f}".format(departure)


def _recompute_schedule(entry: dict) -> None:
    legs = entry.get("legs", {})
    if not legs:
        return
    entry["first_departure_epoch"] = min(
        leg["departure_epoch"] for leg in legs.values()
    )
    entry["final_arrival_epoch"] = max(leg["arrival_epoch"] for leg in legs.values())


def _set_authoritative_arrival(entry: dict, arrival: float) -> None:
    """Apply a current itinerary-level final arrival to the aggregated schedule."""
    legs = entry.get("legs", {})
    if not legs:
        entry["final_arrival_epoch"] = arrival
        return
    terminal_key = max(
        legs,
        key=lambda key: (legs[key]["arrival_epoch"], legs[key]["departure_epoch"], key),
    )
    legs[terminal_key]["arrival_epoch"] = arrival
    # The supplied value is the authoritative itinerary bound. Capping any stale
    # per-leg value above it prevents an older aggregate from re-extending the
    # decision when the ledger is reloaded.
    for leg in legs.values():
        if leg["arrival_epoch"] > arrival:
            leg["arrival_epoch"] = arrival
    _recompute_schedule(entry)


def _state(key: str, entry: dict) -> ItineraryConsent:
    return ItineraryConsent(
        itinerary_key=key,
        traveler_key=entry.get("traveler_key"),
        decision=entry.get("decision"),
        decided_at_epoch=entry.get("decided_at_epoch"),
        first_departure_epoch=entry.get("first_departure_epoch"),
        final_arrival_epoch=entry["final_arrival_epoch"],
        expires_at_epoch=_effective_expiry(entry),
        prompt_timestamps=tuple(sorted(entry["prompts"].items())),
    )


def _valid_entry(key: object, value: object) -> Optional[dict]:
    try:
        stable_key = _bounded_text(key, "itinerary_key", _MAX_KEY_CHARS)
        if stable_key != key or not isinstance(value, dict):
            return None
        traveler = value.get("traveler_key")
        if traveler is not None:
            traveler = _bounded_text(
                traveler, "traveler_key", _MAX_TRAVELER_CHARS
            ).casefold()
        departure = _optional_epoch(
            value.get("first_departure_epoch"), "first_departure_epoch"
        )
        arrival = _epoch(value.get("final_arrival_epoch"), "final_arrival_epoch")
        explicit = _optional_epoch(
            value.get("explicit_expiry_epoch"), "explicit_expiry_epoch"
        )
        decision = value.get("decision")
        if decision is not None and decision not in _DECISIONS:
            return None
        decided = _optional_epoch(value.get("decided_at_epoch"), "decided_at_epoch")
        if (decision is None) != (decided is None):
            return None
        prompts_value = value.get("prompts", {})
        if not isinstance(prompts_value, dict) or set(prompts_value) - {
            STAGE_48H,
            STAGE_24H,
        }:
            return None
        prompts = {
            stage: _epoch(timestamp, "prompt timestamp")
            for stage, timestamp in prompts_value.items()
        }
        legs_value = value.get("legs", {})
        if not isinstance(legs_value, dict) or len(legs_value) > _MAX_LEGS:
            return None
        legs = {}
        for leg_key, leg_value in legs_value.items():
            stable_leg_key = _bounded_text(leg_key, "leg_key", _MAX_LEG_KEY_CHARS)
            if stable_leg_key != leg_key or not isinstance(leg_value, dict):
                return None
            leg_departure = _epoch(leg_value.get("departure_epoch"), "leg departure_epoch")
            leg_arrival = _epoch(leg_value.get("arrival_epoch"), "leg arrival_epoch")
            if leg_arrival < leg_departure:
                return None
            legs[leg_key] = {
                "departure_epoch": leg_departure,
                "arrival_epoch": leg_arrival,
            }
        if legs:
            departure = min(leg["departure_epoch"] for leg in legs.values())
            arrival = max(leg["arrival_epoch"] for leg in legs.values())
        if departure is not None and arrival < departure:
            return None
    except (ValueError, TypeError):
        return None
    return {
        "traveler_key": traveler,
        "first_departure_epoch": departure,
        "final_arrival_epoch": arrival,
        "explicit_expiry_epoch": explicit,
        "decision": decision,
        "decided_at_epoch": decided,
        "prompts": prompts,
        "legs": legs,
    }
