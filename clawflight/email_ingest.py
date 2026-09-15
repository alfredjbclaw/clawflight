"""Pure parsing of trusted, explicitly labelled flight-confirmation email.

The objects in this module deliberately contain no message text. They are safe
to put in a durable queue while the (potentially sensitive) source message
remains in the user's own mailbox. Provenance is a bounded identifier plus a
digest, never a body excerpt.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from email.utils import parseaddr
from typing import FrozenSet, Iterable, Optional, Tuple, Union

from .parse import KNOWN_CARRIERS, _airline_email_city, _time_to_iso


logger = logging.getLogger(__name__)


MAX_BODY_CHARS = 256 * 1024
MAX_CANDIDATES = 8
MAX_IDENTITY_CHARS = 512
MAX_EMAIL_CHARS = 254

_ADDRESS_RE = re.compile(r"^[^\s@<>]+@([^\s@<>]+)$")
_FLIGHT_RE = re.compile(
    r"(?im)^[ \t]*(?:flight(?:[ \t]+(?:number|#))?[ \t]*:[ \t]*)?"
    r"(?P<carrier>[A-Z0-9]{2})(?:[ \t]*-[ \t]*|[ \t]+)?"
    r"(?P<number>[0-9]{1,4})[ \t]*\r?$"
)
_FIELD_PATTERNS = {
    "date": re.compile(
        r"(?im)^\s*(?:date|flight\s+date|service\s+date|departure\s+date)\s*:\s*"
        r"(?P<value>[^\r\n]+?)\s*$"
    ),
    "origin": re.compile(
        r"(?im)^\s*(?:from|origin|departure\s+airport)\s*:\s*(?P<value>[^\r\n]+?)\s*$"
    ),
    "destination": re.compile(
        r"(?im)^\s*(?:to|destination|arrival\s+airport)\s*:\s*(?P<value>[^\r\n]+?)\s*$"
    ),
    "departure_time": re.compile(
        r"(?im)^\s*(?:departure|depart)\s+time\s*:\s*(?P<value>[^\r\n]+?)\s*$"
    ),
    "arrival_time": re.compile(
        r"(?im)^\s*arrival\s+time\s*:\s*(?P<value>[^\r\n]+?)\s*$"
    ),
    "route": re.compile(r"(?im)^\s*route\s*:\s*(?P<value>[^\r\n]+?)\s*$"),
    "confirmation": re.compile(
        r"(?im)^\s*(?:confirmation(?:\s+(?:code|number)|\s*#)?|booking\s+reference"
        r"|record\s+locator|conf#)\s*:\s*(?P<value>[A-Z0-9]{5,8})\s*$"
    ),
    "traveler": re.compile(
        r"(?im)^\s*(?:traveler|traveller|passenger|passenger\s+name)\s*:\s*"
        r"(?P<value>[^\r\n]+?)\s*$"
    ),
}
_ROUTE_RE = re.compile(
    r"^\s*(?P<origin>[A-Z]{3})\s*(?:to|->|\N{RIGHTWARDS ARROW}|-)\s*(?P<destination>[A-Z]{3})\s*$",
    re.I,
)
_AIRPORT_RE = re.compile(r"(?:^|\()\s*(?P<code>[A-Z]{3})\s*(?:\)|$)")
_CONFIRMATION_RE = re.compile(r"^[A-Z0-9]{5,8}$")


def _domain(value: str) -> str:
    candidate = value.strip().rstrip(".").casefold()
    if not candidate or "@" in candidate or any(char.isspace() for char in candidate):
        raise ValueError("trusted base domains must be domain names")
    try:
        labels = candidate.encode("idna").decode("ascii").split(".")
    except UnicodeError as exc:
        raise ValueError("trusted base domains must be valid domain names") from exc
    if any(
        not label or len(label) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
        for label in labels
    ):
        raise ValueError("trusted base domains must be valid domain names")
    return ".".join(labels)


def _address(value: str) -> Optional[str]:
    """Return the canonical addr-spec from an address or display-address."""
    if (
        not isinstance(value, str)
        or len(value) > MAX_EMAIL_CHARS + 256
        or "\r" in value
        or "\n" in value
    ):
        return None
    _display_name, parsed = parseaddr(value)
    match = _ADDRESS_RE.fullmatch(parsed)
    if match is None:
        return None
    local, raw_domain = parsed.rsplit("@", 1)
    try:
        domain = _domain(raw_domain)
    except ValueError:
        return None
    address = "{}@{}".format(local.casefold(), domain)
    return address if len(local) <= 64 and len(address) <= MAX_EMAIL_CHARS else None


@dataclass(frozen=True, init=False)
class TrustedSenderPolicy:
    """An allow-list with separate exact-address and base-domain entries.

    A base domain matches itself and true DNS subdomains only. In particular
    ``example.com`` does not match ``example.com.evil.test``.
    """

    exact_addresses: FrozenSet[str]
    base_domains: FrozenSet[str]

    def __init__(
        self,
        exact_addresses: Iterable[str] = (),
        base_domains: Iterable[str] = (),
    ) -> None:
        addresses = []
        for value in exact_addresses:
            normalized = _address(value)
            if normalized is None:
                raise ValueError("trusted exact senders must be email addresses")
            addresses.append(normalized)
        object.__setattr__(self, "exact_addresses", frozenset(addresses))
        object.__setattr__(
            self, "base_domains", frozenset(_domain(value) for value in base_domains)
        )

    @classmethod
    def from_sources(cls, sources: Iterable[str]) -> "TrustedSenderPolicy":
        """Build a policy from a set containing addr-specs and base domains."""
        addresses, domains = [], []
        for source in sources:
            (addresses if "@" in source else domains).append(source)
        return cls(addresses, domains)

    def trusts(self, sender: str) -> bool:
        address = _address(sender)
        if address is None:
            return False
        if address in self.exact_addresses:
            return True
        sender_domain = address.rsplit("@", 1)[1]
        return any(
            sender_domain == base or sender_domain.endswith("." + base)
            for base in self.base_domains
        )


def is_trusted_sender(
    sender: str, trusted_sources: Union[TrustedSenderPolicy, Iterable[str]]
) -> bool:
    """Check *sender* against an explicit caller-owned policy or source set."""
    policy = (
        trusted_sources
        if isinstance(trusted_sources, TrustedSenderPolicy)
        else TrustedSenderPolicy.from_sources(trusted_sources)
    )
    return policy.trusts(sender)


@dataclass(frozen=True)
class EmailEvidence:
    """Bounded durable provenance; raw body content is intentionally absent."""

    source_id: str
    sender_identity: str
    organizer_identity: Optional[str]
    observed_at: datetime
    source_kind: str
    digest: str

    @property
    def identity(self) -> Tuple[str, str]:
        """The idempotency key for a particular version of a source."""
        return self.source_id, self.digest


@dataclass(frozen=True)
class EmailItineraryCandidate:
    carrier: str
    number: int
    service_date: str
    origin: str
    destination: str
    confirmation_code: str
    traveler: Optional[str]
    evidence: EmailEvidence
    sched_dep_iso: Optional[str] = None
    sched_arr_iso: Optional[str] = None


@dataclass(frozen=True)
class SkippedEmailLeg:
    """Bounded detail about a labelled leg that could not be ingested."""

    carrier: str
    number: int
    reason: str


class EmailIngestionResult(tuple):
    """Candidate tuple plus non-sensitive reasons for legs skipped within it."""

    skipped_legs: Tuple[SkippedEmailLeg, ...]

    def __new__(
        cls,
        candidates: Iterable[EmailItineraryCandidate] = (),
        skipped_legs: Iterable[SkippedEmailLeg] = (),
    ) -> "EmailIngestionResult":
        result = super().__new__(cls, candidates)
        result.skipped_legs = tuple(skipped_legs)
        return result

    @property
    def candidates(self) -> Tuple[EmailItineraryCandidate, ...]:
        return tuple(self)

    @property
    def skipped_reasons(self) -> Tuple[str, ...]:
        return tuple(leg.reason for leg in self.skipped_legs)


def _one_field(text: str, field: str) -> Optional[str]:
    values = {match.group("value").strip() for match in _FIELD_PATTERNS[field].finditer(text)}
    return next(iter(values)) if len(values) == 1 else None


def _service_date(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    raw = value.strip()
    slash = re.fullmatch(r"(?P<first>\d{1,2})/(?P<second>\d{1,2})/(?P<year>\d{4})", raw)
    if slash:
        first = int(slash.group("first"))
        second = int(slash.group("second"))
        if first <= 12 and second <= 12:
            logger.warning("assuming US month-first date for ambiguous value %r", raw)
            format_string = "%m/%d/%Y"
        elif first > 12:
            format_string = "%d/%m/%Y"
        else:
            format_string = "%m/%d/%Y"
        try:
            return datetime.strptime(raw, format_string).date().isoformat()
        except ValueError:
            return None
    for format_string in (
        "%Y-%m-%d",
        "%B %d, %Y",
        "%b %d, %Y",
        "%d %b %Y",
        "%d %B %Y",
        "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(raw, format_string).date().isoformat()
        except ValueError:
            continue
    return None


def _airport(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = value.strip()
    match = _AIRPORT_RE.search(normalized)
    if match:
        return match.group("code").upper()
    resolved = _airline_email_city(normalized)
    if resolved is None:
        logger.warning("unresolved airport or city value %r", normalized[:128])
    return resolved


def _route(segment: str) -> Tuple[Optional[str], Optional[str]]:
    origin = _airport(_one_field(segment, "origin"))
    destination = _airport(_one_field(segment, "destination"))
    if origin and destination:
        return origin, destination
    route = _one_field(segment, "route")
    match = _ROUTE_RE.fullmatch(route or "")
    if match:
        return match.group("origin").upper(), match.group("destination").upper()
    return None, None


def _bounded_identity(value: str, label: str) -> str:
    normalized = re.sub(r"\s+", " ", value).strip()
    if (
        not normalized
        or len(normalized) > MAX_IDENTITY_CHARS
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ValueError("{} must be a non-empty bounded identity".format(label))
    return normalized


def _bounded_source_value(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError("{} must be a string".format(label))
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > MAX_IDENTITY_CHARS
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ValueError("{} must be a non-empty bounded value".format(label))
    return normalized


def _organizer(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    address = _address(value)
    return address or _bounded_identity(value, "organizer")


def _body_digest(body: str) -> str:
    # Normalize transport line endings but preserve all message content, so a
    # substantive edit always changes the digest while mail-client newline
    # rewrites do not.
    canonical = body.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def ingest_email(
    *,
    source_id: str,
    sender: str,
    observed_at: datetime,
    body: str,
    trusted_sources: Union[TrustedSenderPolicy, Iterable[str]],
    organizer: Optional[str] = None,
    source_kind: str = "email",
) -> EmailIngestionResult:
    """Turn one trusted labelled confirmation message into bounded candidates.

    Invalid, untrusted, ambiguous, or incomplete messages return an empty
    tuple. Programmer errors in durable provenance arguments raise ValueError.
    """
    policy = (
        trusted_sources
        if isinstance(trusted_sources, TrustedSenderPolicy)
        else TrustedSenderPolicy.from_sources(trusted_sources)
    )
    sender_identity = _address(sender)
    if sender_identity is None or not policy.trusts(sender):
        return EmailIngestionResult()
    if not isinstance(body, str) or len(body) > MAX_BODY_CHARS:
        return EmailIngestionResult()
    stable_source_id = _bounded_source_value(source_id, "source_id")
    stable_source_kind = _bounded_source_value(source_kind, "source_kind")
    if not isinstance(observed_at, datetime):
        raise ValueError("observed_at must be a caller-supplied datetime")

    flights = list(_FLIGHT_RE.finditer(body))
    if not flights or len(flights) > MAX_CANDIDATES:
        return EmailIngestionResult()

    global_confirmation = _one_field(body, "confirmation")
    global_traveler = _one_field(body, "traveler")
    evidence = EmailEvidence(
        source_id=stable_source_id,
        sender_identity=sender_identity,
        organizer_identity=_organizer(organizer),
        observed_at=observed_at,
        source_kind=stable_source_kind,
        digest=_body_digest(body),
    )
    candidates = []
    skipped_legs = []
    for index, flight in enumerate(flights):
        carrier = flight.group("carrier").upper()
        if carrier not in KNOWN_CARRIERS:
            skipped_legs.append(
                SkippedEmailLeg(carrier, int(flight.group("number")), "unknown carrier")
            )
            continue
        end = flights[index + 1].start() if index + 1 < len(flights) else len(body)
        segment = body[flight.start() : end]
        service_date = _service_date(_one_field(segment, "date"))
        origin, destination = _route(segment)
        confirmation = _one_field(segment, "confirmation") or global_confirmation
        traveler = _one_field(segment, "traveler") or global_traveler
        if (
            service_date is None
            or origin is None
            or destination is None
            or origin == destination
            or confirmation is None
            or _CONFIRMATION_RE.fullmatch(confirmation.upper()) is None
        ):
            missing = []
            if service_date is None:
                missing.append("service date")
            if origin is None or destination is None or origin == destination:
                missing.append("route")
            if confirmation is None or _CONFIRMATION_RE.fullmatch(confirmation.upper()) is None:
                missing.append("confirmation code")
            skipped_legs.append(
                SkippedEmailLeg(
                    carrier,
                    int(flight.group("number")),
                    "invalid {}".format(", ".join(missing)),
                )
            )
            continue
        if traveler and len(traveler) > MAX_IDENTITY_CHARS:
            skipped_legs.append(
                SkippedEmailLeg(carrier, int(flight.group("number")), "invalid traveler")
            )
            continue
        try:
            candidate = EmailItineraryCandidate(
                carrier=carrier,
                number=int(flight.group("number")),
                service_date=service_date,
                origin=origin,
                destination=destination,
                confirmation_code=confirmation.upper(),
                traveler=_bounded_identity(traveler, "traveler") if traveler else None,
                evidence=evidence,
                sched_dep_iso=_time_to_iso(
                    service_date, _one_field(segment, "departure_time"), origin
                ),
                # Arrival clocks remain on the service date; overnight rollover is deferred.
                sched_arr_iso=_time_to_iso(
                    service_date, _one_field(segment, "arrival_time"), destination
                ),
            )
        except (OverflowError, TypeError, ValueError):
            skipped_legs.append(
                SkippedEmailLeg(carrier, int(flight.group("number")), "malformed leg")
            )
            continue
        candidates.append(candidate)
    return EmailIngestionResult(candidates, skipped_legs)


# Descriptive aliases keep the primitive pleasant for callers that do not care
# whether the provenance originally arrived as email or another message source.
Evidence = EmailEvidence
ItineraryCandidate = EmailItineraryCandidate
ingest_trusted_email = ingest_email
