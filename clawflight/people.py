"""Config-driven attribution: which configured person is a flight for?

The private ancestor of this module hardcoded one family. Here the table is
data: an empty :class:`PersonTable` attributes nothing, and every match rule is
supplied by the user's ``people`` config. Evidence is ranked so that a weaker
later source can never overwrite a stronger earlier one:

===== ============================================================
rank   evidence
===== ============================================================
4      a person named outright, e.g. ``flight add --person sam``
3      an explicit passenger / traveler name on a booking
2      a possessive calendar title ("Alex's flight to Denver")
1      a known attendee address on the calendar event
0      nothing
===== ============================================================

Rank 4 exists because a human saying "this is Sam's flight" is better evidence
than anything inferred from text, and must not be overwritten by a later email
whose passenger name happens to match somebody else.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .models import PersonRef


UNKNOWN_PERSON = PersonRef(key="unknown", name="Unknown")

_MAX_KEY_CHARS = 64
_MAX_DISPLAY_CHARS = 64
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class Person:
    """One configured traveler and the evidence that identifies them."""

    key: str
    display: str
    match_substrings: Tuple[str, ...] = ()
    match_email_localparts: Tuple[str, ...] = ()
    possessive_aliases: Tuple[str, ...] = ()

    @property
    def ref(self) -> PersonRef:
        return PersonRef(key=self.key, name=self.display)


@dataclass(frozen=True)
class PersonTable:
    """An ordered attribution table. The empty table attributes nothing."""

    people: Tuple[Person, ...] = field(default_factory=tuple)

    def __iter__(self):
        return iter(self.people)

    def __len__(self) -> int:
        return len(self.people)

    def __bool__(self) -> bool:
        return bool(self.people)

    def get(self, key: str) -> Optional[Person]:
        for person in self.people:
            if person.key == key:
                return person
        return None

    @classmethod
    def from_entries(cls, entries: Optional[Iterable[object]]) -> "PersonTable":
        """Build a table from parsed config entries, skipping invalid rows.

        A row is valid when it carries a bounded lowercase ``key`` and a
        non-empty ``display``. Invalid rows are dropped rather than raising, so
        one typo in a config file cannot stop the whole tracker.
        """
        people: List[Person] = []
        seen = set()
        for entry in entries or ():
            person = _person_from_entry(entry)
            if person is None or person.key in seen:
                continue
            people.append(person)
            seen.add(person.key)
        return cls(tuple(people))

    # -- evidence lookups ---------------------------------------------------

    def from_text(self, value: str) -> Optional[PersonRef]:
        """Match a passenger/traveler name against configured name variants."""
        if not isinstance(value, str):
            return None
        lowered = value.casefold()
        for person in self.people:
            if any(substring in lowered for substring in person.match_substrings):
                return person.ref
        return None

    def from_title(self, title: str) -> Optional[PersonRef]:
        """Match a possessive calendar title such as ``Alex's flight home``."""
        if not isinstance(title, str):
            return None
        lowered = title.casefold()
        for person in self.people:
            if any(
                "{}'s".format(name) in lowered or "{}’s".format(name) in lowered
                for name in _possessive_names(person)
            ):
                return person.ref
        return None

    def from_attendee(self, value: str) -> Optional[PersonRef]:
        """Match an attendee address against configured local-parts.

        The local-part must match exactly once separators are stripped. A
        prefix match would let ``alexis@`` attribute to ``alex``.
        """
        if not isinstance(value, str):
            return None
        lowered = value.casefold()
        at_index = lowered.find("@")
        if at_index < 0:
            return None
        local_part = lowered[:at_index].split("<")[-1].strip()
        normalized = re.sub(r"[.\-_+]", "", local_part)
        if not normalized:
            return None
        for person in self.people:
            if normalized in person.match_email_localparts:
                return person.ref
        return None

    # -- hint-level helpers -------------------------------------------------

    def from_key(self, key: str) -> Optional[PersonRef]:
        """Resolve a person named outright by their configured key."""
        if not isinstance(key, str):
            return None
        person = self.get(key.strip().casefold())
        return person.ref if person is not None else None

    def person_from_hints(self, hints: dict) -> PersonRef:
        explicit = self.from_key(hints.get("person_key"))
        if explicit is not None:
            return explicit
        passenger = hints.get("passenger_name")
        if isinstance(passenger, str):
            matched = self.from_text(passenger)
            if matched is not None:
                return matched
        title = hints.get("title")
        if isinstance(title, str):
            matched = self.from_title(title)
            if matched is not None:
                return matched
        attendees = hints.get("attendees")
        if isinstance(attendees, list):
            for attendee in attendees:
                matched = self.from_attendee(attendee)
                if matched is not None:
                    return matched
        return UNKNOWN_PERSON

    def rank(self, hints: dict) -> int:
        if self.from_key(hints.get("person_key")) is not None:
            return 4
        passenger = hints.get("passenger_name")
        if isinstance(passenger, str) and self.from_text(passenger) is not None:
            return 3
        title = hints.get("title")
        if isinstance(title, str) and self.from_title(title) is not None:
            return 2
        attendees = hints.get("attendees")
        if isinstance(attendees, list) and any(
            self.from_attendee(attendee) is not None for attendee in attendees
        ):
            return 1
        return 0


def _possessive_names(person: Person) -> Tuple[str, ...]:
    """Names a possessive title may use: the key, first names, and aliases."""
    names = [person.key]
    names.extend(
        substring.split()[0] for substring in person.match_substrings if substring.split()
    )
    names.extend(person.possessive_aliases)
    names.append(person.display.casefold())
    return tuple(dict.fromkeys(name for name in names if name))


def _person_from_entry(entry: object) -> Optional[Person]:
    if not isinstance(entry, dict):
        return None
    key = entry.get("key")
    display = entry.get("display") or entry.get("name")
    if not isinstance(key, str) or not isinstance(display, str):
        return None
    key = key.strip().casefold()
    display = display.strip()
    if not _KEY_RE.match(key) or key == "unknown":
        return None
    if not display or len(display) > _MAX_DISPLAY_CHARS or len(key) > _MAX_KEY_CHARS:
        return None
    return Person(
        key=key,
        display=display,
        match_substrings=_casefolded(entry.get("match_substrings")),
        match_email_localparts=tuple(
            re.sub(r"[.\-_+]", "", value) for value in _casefolded(entry.get("match_email_localparts"))
        ),
        possessive_aliases=_casefolded(entry.get("possessive_aliases")),
    )


def _casefolded(value: object) -> Tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    cleaned = [
        item.strip().casefold()
        for item in value
        if isinstance(item, str) and item.strip()
    ]
    return tuple(dict.fromkeys(cleaned))


def to_entries(table: PersonTable) -> List[Dict[str, Sequence[str]]]:
    """Round-trip a table back to config-shaped dictionaries."""
    return [
        {
            "key": person.key,
            "display": person.display,
            "match_substrings": list(person.match_substrings),
            "match_email_localparts": list(person.match_email_localparts),
            "possessive_aliases": list(person.possessive_aliases),
        }
        for person in table
    ]
