"""Parsers for calendar-event exports and airline confirmation email.

Two independent front doors feed the same :class:`ParsedFlight` shape:

* :func:`parse_calendar_events` reads the indented plain-text export that most
  calendar CLIs produce (day rules, one indented block per event, ``Notes:``
  continuation lines).
* :func:`parse_airline_email` reads the plain-text rendering of the airline
  receipt / trip-confirmation / schedule-change layouts.

Both are total functions over untrusted text: malformed input yields an empty
list rather than an exception.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from .airports import default_airports
from .models import FlightLeg


AIRLINE_NAME_TO_IATA = {
    "delta": "DL",
    "delta air lines": "DL",
    "american": "AA",
    "american airlines": "AA",
    "united": "UA",
    "united airlines": "UA",
    "jetblue": "B6",
    "jetblue airways": "B6",
    "southwest": "WN",
    "southwest airlines": "WN",
    "alaska": "AS",
    "alaska airlines": "AS",
    "spirit": "NK",
    "spirit airlines": "NK",
    "frontier": "F9",
    "frontier airlines": "F9",
    "british airways": "BA",
    "air france": "AF",
    "lufthansa": "LH",
    "emirates": "EK",
    "skywest airlines": "OO",
    "republic airways": "YX",
    "envoy air": "MQ",
    "endeavor air": "9E",
}
CITY_TO_IATA = {
    "aspen": "ASE",
    "dallas": "DFW",
    "new york": "JFK",
    "los angeles": "LAX",
    "kennedy": "JFK",
    "laguardia": "LGA",
    "newark": "EWR",
    "seattle": "SEA",
    "boston": "BOS",
    "chicago": "ORD",
    "denver": "DEN",
}
# Airline receipts print city names, not IATA codes, in their departure and
# arrival columns. Only mappings we can prove belong here: an unmapped city
# yields a leg with a missing airport rather than a guess.
AIRLINE_EMAIL_CITY_TO_IATA = {
    "NYC-KENNEDY": "JFK",
    "NYC-LAGUARDIA": "LGA",
    "KENNEDY INTL": "JFK",
    "SAN FRANCISCO": "SFO",
    "SAN FRANCISCO INTL": "SFO",
    "SAN JOSE": "SJC",
    "LOS ANGELES": "LAX",
    "SEATTLE-TACOMA": "SEA",
    "BOSTON": "BOS",
}
MAX_AIRLINE_EMAIL_BYTES = 256 * 1024

# Known IATA carrier designators. Used to reject prose matches like "US 100":
# a bare two-letter token followed by digits is only a flight when the token is
# a designator we actually recognise.
KNOWN_CARRIERS = frozenset({
    # US majors / low-cost
    "AA", "DL", "UA", "B6", "WN", "AS", "NK", "F9", "HA", "G4", "SY", "MX",
    # US regionals (codeshare operators printed on majors' tickets)
    "OO", "YX", "MQ", "OH", "YV", "QX", "ZW", "EV", "CP", "PT", "AX", "9K", "9E",
    # International carriers
    "BA", "AF", "LH", "VS", "EK", "KL", "IB", "AC", "WS", "AM", "AV", "LA",
    "QR", "SQ", "CX", "NH", "JL", "QF", "EI", "TP", "SN", "LX", "OS", "AY",
    "TK", "ET", "SA", "NZ", "VA", "FI", "DE",
})


def _resolve_carrier(token: str) -> "Optional[str]":
    """Resolve an airline name or validated IATA designator."""
    if not isinstance(token, str):
        return None
    normalized = re.sub(r"\s+", " ", token).strip()
    if not normalized:
        return None
    designator = normalized.upper()
    if designator in KNOWN_CARRIERS:
        return designator
    return AIRLINE_NAME_TO_IATA.get(normalized.casefold())


_EMAIL_CARRIER_PATTERN = "(?:{})".format(
    "|".join(
        re.escape(name)
        for name in sorted(AIRLINE_NAME_TO_IATA, key=lambda value: (-len(value), value))
    )
)
_EMAIL_CARRIER_TOKEN_PATTERN = "(?:{}|{})".format(
    _EMAIL_CARRIER_PATTERN,
    "|".join(re.escape(carrier) for carrier in sorted(KNOWN_CARRIERS)),
)
_EMAIL_OPERATED_SUFFIX = r"(?:\s+operated\s+by\s+.{1,100})?"


def _build_timezones() -> "dict[str, str]":
    return {iata: airport.tz for iata, airport in default_airports().items()}


AIRPORT_TIMEZONES = _build_timezones()


def airport_timezone(iata: Optional[str]) -> "Optional[str]":
    """IANA timezone for an IATA code, or None if the airport is unknown.

    Callers should treat None as a surfaced warning, never a silent skip: an
    airport with no timezone produces a leg with no ISO departure time, which
    in turn never enters the watch window.
    """
    return AIRPORT_TIMEZONES.get((iata or "").strip().upper()) or None


DAY_RE = re.compile(r"^── .*?, (?P<month>[A-Za-z]{3}) (?P<day>\d{1,2}), (?P<year>\d{4})")
FIELD_RE = re.compile(r"^  (?P<key>[A-Za-z]+):\s*(?P<value>.*)$")
TITLE_RE = re.compile(r"^  (?P<title>\S.*)$")
TIME_RE = re.compile(r"\b(\d{1,2}:\d{2}\s*(?:AM|PM))\b", re.IGNORECASE)
AIRLINE_RE = re.compile(
    r"\b(?:Flight|Airlines?|{})\b".format(_EMAIL_CARRIER_PATTERN),
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedFlight:
    leg: FlightLeg
    hints: dict


# --------------------------------------------------------------------------
# Airline email
# --------------------------------------------------------------------------


def parse_airline_email(text: str, default_year: int) -> "list[ParsedFlight]":
    """Parse the airline receipt and change-notice layouts."""
    if (
        not isinstance(text, str)
        or not text
        or len(text) > MAX_AIRLINE_EMAIL_BYTES
        or len(text.encode("utf-8", errors="replace")) > MAX_AIRLINE_EMAIL_BYTES
    ):
        return []
    try:
        if re.search(r"\*\*\s*Confirmation Number\s*\*\*", text, re.IGNORECASE):
            return _parse_receipt(text, default_year)
        if re.search(r"\bYour trip confirmation and receipt\b", text, re.IGNORECASE):
            return _parse_trip_confirmation(text)
        if (
            re.search(r"\bFlight Schedule Change\b", text, re.IGNORECASE)
            and re.search(r"\bYour New Flight Info\b", text, re.IGNORECASE)
        ):
            return _parse_schedule_change_email(text, default_year)
        return _parse_generic_email(text, default_year)
    except (OverflowError, TypeError, ValueError):
        # Mail bodies are untrusted input. A damaged date or a caller-supplied
        # year must not stop ingestion of the rest of the mailbox.
        return []
    return []


def _parse_receipt(text: str, default_year: int) -> "list[ParsedFlight]":
    confirmation = re.search(
        r"\*\*\s*Confirmation Number\s*\*\*\s*([A-Z0-9]{5,8})\b",
        text,
        re.IGNORECASE,
    )
    if confirmation is None:
        return []
    passenger = re.search(
        r"\*\*\s*Passenger Info\s*\*\*\s*Name:\s*([^\r\n]+)",
        text,
        re.IGNORECASE,
    )
    hints = {}
    if passenger:
        hints["passenger_name"] = re.sub(r"\s+", " ", passenger.group(1)).strip()

    seats = {
        (_resolve_carrier(match.group("carrier")), int(match.group("number"))):
        match.group("seat").upper()
        for match in re.finditer(
            r"(?im)^\s*(?P<carrier>{})\s+(?P<number>\d{{1,4}})\s+"
            r"(?P<seat>\d{{1,2}}[A-F])\s*$".format(
                _EMAIL_CARRIER_TOKEN_PATTERN
            ),
            text,
        )
    }
    day_headers = list(
        re.finditer(
            r"(?im)^\s*\*\*\s*[A-Z]{3},\s*(\d{1,2})([A-Z]{3})"
            r"\*+\s*DEPART\*+\s*ARRIVE\*+\s*$",
            text,
        )
    )
    parsed = []
    seen = set()
    for index, header in enumerate(day_headers):
        try:
            date = datetime.strptime(
                "{} {} {}".format(header.group(2), header.group(1), default_year),
                "%b %d %Y",
            ).date().isoformat()
        except (OverflowError, ValueError):
            continue
        end = day_headers[index + 1].start() if index + 1 < len(day_headers) else len(text)
        section = text[header.end() : end]
        flights = list(re.finditer(
            r"(?im)^\s*(?P<carrier>{})\s+(?P<number>\d{{1,4}}){}\s*$".format(
                _EMAIL_CARRIER_TOKEN_PATTERN,
                _EMAIL_OPERATED_SUFFIX,
            ),
            section,
        ))
        for flight_index, flight in enumerate(flights):
            carrier = _resolve_carrier(flight.group("carrier"))
            if carrier is None:
                continue
            number = int(flight.group("number"))
            key = (carrier, number, date)
            if key in seen:
                continue
            flight_end = (
                flights[flight_index + 1].start()
                if flight_index + 1 < len(flights)
                else len(section)
            )
            details = section[flight.end() : flight_end]
            lines = [line.strip() for line in details.splitlines() if line.strip()]
            origin_match = re.fullmatch(
                r"(?:Standby\s+)?(?P<origin>[A-Z][A-Z -]*?)", lines[0], re.IGNORECASE
            ) if lines else None
            dep_field, destination = (
                _receipt_destination_line(lines[1]) if len(lines) > 1 else (None, None)
            )
            origin = (
                _airline_email_city(origin_match.group("origin"))
                if origin_match else None
            )
            dest = _airline_email_city(destination) if destination else None
            dep_time = _clock_value(dep_field)
            arr_time = _clock_value(lines[2]) if len(lines) > 2 else None
            operating_carrier, operating_number = _operating_identity(
                section[flight.start():flight_end]
            )
            parsed.append(
                _airline_parsed_flight(
                    carrier, number, date, origin, dest,
                    dep_time, arr_time,
                    confirmation.group(1).upper(), seats.get((carrier, number)), hints,
                    operating_carrier, operating_number,
                )
            )
            seen.add(key)
    return parsed


def _parse_trip_confirmation(text: str) -> "list[ParsedFlight]":
    confirmation = re.search(
        r"Record locator:\s*\*+\s*([A-Z0-9]{5,8})\s*\*+",
        text,
        re.IGNORECASE,
    )
    if confirmation is None:
        return []
    greeting = re.search(
        r"\[Hello\s+(?:Mr|Mrs|Ms|Miss|Dr)\.?\s+([^!\]\r\n]+)!?\]",
        text,
        re.IGNORECASE,
    )
    hints = {}
    if greeting:
        hints["passenger_name"] = re.sub(r"\s+", " ", greeting.group(1)).strip()

    dates = list(
        re.finditer(
            r"(?im)^\s*(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),"
            r"\s+([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})\s*$",
            text,
        )
    )
    parsed = []
    for index, date_match in enumerate(dates):
        end = dates[index + 1].start() if index + 1 < len(dates) else len(text)
        block = text[date_match.end() : end]
        carrier_match = re.search(
            r"(?im)^\s*(?P<carrier>{})\s+(?P<number>\d{{1,4}}){}\s*$".format(
                _EMAIL_CARRIER_TOKEN_PATTERN,
                _EMAIL_OPERATED_SUFFIX,
            ),
            block,
        )
        if carrier_match is None:
            continue
        carrier = _resolve_carrier(carrier_match.group("carrier"))
        if carrier is None:
            continue
        prefix = block[: carrier_match.start()]
        airports = list(re.finditer(r"(?im)^\s*([A-Z]{3})\s*$", prefix))
        if len(airports) < 2:
            continue
        dep_time = _clock_in(prefix[airports[0].end() : airports[1].start()])
        arr_time = _clock_in(prefix[airports[1].end() :])
        try:
            date = datetime.strptime(
                "{} {} {}".format(
                    date_match.group(1), date_match.group(2), date_match.group(3)
                ),
                "%B %d %Y",
            ).date().isoformat()
        except ValueError:
            continue
        seat_match = re.search(
            r"(?im)^\s*Seats?:\s*(\d{1,2}[A-F])\s*$",
            block[carrier_match.end() :],
        )
        operating_carrier, operating_number = _operating_identity(block)
        parsed.append(
            _airline_parsed_flight(
                carrier, int(carrier_match.group("number")), date,
                airports[0].group(1), airports[1].group(1),
                dep_time, arr_time,
                confirmation.group(1).upper(),
                seat_match.group(1).upper() if seat_match else None,
                hints,
                operating_carrier, operating_number,
            )
        )
    return parsed


def _parse_schedule_change_email(text: str, default_year: int) -> "list[ParsedFlight]":
    confirmation = re.search(
        r"Trip Confirmation\s+\[#([A-Z0-9]{5,8})\]", text, re.IGNORECASE
    )
    new_info = re.search(r"\bYour New Flight Info\b", text, re.IGNORECASE)
    if confirmation is None or new_info is None:
        return []
    # The first block after the heading is the replacement itinerary. Later
    # blocks restate the original one and must never become a second flight.
    new_section = text[new_info.end() :]
    original_info = re.search(r"\bYour Original Flight Info\b", new_section, re.IGNORECASE)
    if original_info:
        new_section = new_section[: original_info.start()]
    heading = re.search(
        r"(?im)^\s*(?P<carrier>{})\s+(?P<number>\d{{1,4}}){}\s*$\s*"
        r"^\s*(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s*"
        r"(?P<month>[A-Z][a-z]+)\s+(?P<day>\d{{1,2}})\s*$".format(
            _EMAIL_CARRIER_TOKEN_PATTERN,
            _EMAIL_OPERATED_SUFFIX,
        ),
        new_section,
    )
    if heading is None:
        return []
    route_text = new_section[heading.end() :]
    cities = list(
        re.finditer(r"(?im)^\s*([A-Za-z][A-Za-z -]+?)\s*$", route_text)
    )
    if len(cities) < 2:
        return []
    dep_time = _clock_in(route_text[cities[0].end() : cities[1].start()])
    arr_time = _clock_in(route_text[cities[1].end() :])
    try:
        date = datetime.strptime(
            "{} {} {}".format(heading.group("month"), heading.group("day"), default_year),
            "%B %d %Y",
        ).date().isoformat()
    except (OverflowError, ValueError):
        return []
    carrier = _resolve_carrier(heading.group("carrier"))
    if carrier is None:
        return []
    operating_carrier, operating_number = _operating_identity(new_section)
    return [
        _airline_parsed_flight(
            carrier, int(heading.group("number")), date,
            _airline_email_city(cities[0].group(1)),
            _airline_email_city(cities[1].group(1)),
            dep_time, arr_time,
            confirmation.group(1).upper(), None,
            {"notes_excerpt": "Your flight changed"},
            operating_carrier, operating_number,
        )
    ]


def _parse_generic_email(text: str, default_year: int) -> "list[ParsedFlight]":
    """Parse strict line-oriented itinerary blocks not covered by known layouts."""
    lines = text.splitlines()
    flights = []
    for index, line in enumerate(lines):
        identity = _generic_flight_identity(line)
        if identity is not None:
            flights.append((index, identity))
    parsed = []
    seen = set()
    confirmation = _generic_confirmation(text)
    for position, (index, (carrier, number)) in enumerate(flights):
        lower = max(0, flights[position - 1][0] + 1 if position else index - 12)
        blank_lines = [
            line_index
            for line_index in range(lower, index)
            if not lines[line_index].strip()
        ]
        if blank_lines:
            lower = blank_lines[-1] + 1
        upper = min(
            len(lines),
            flights[position + 1][0]
            if position + 1 < len(flights)
            else index + 13,
        )
        candidates = [
            (abs(line_index - index), line_index, line_value)
            for line_index, line_value in enumerate(lines[lower:upper], lower)
        ]
        dates = [
            (distance, line_index, parsed_date)
            for distance, line_index, line_value in candidates
            if (parsed_date := _generic_date(line_value, default_year)) is not None
        ]
        routes = [
            (distance, line_index, parsed_route)
            for distance, line_index, line_value in candidates
            if line_index >= index
            if (parsed_route := _generic_route(line_value)) is not None
        ]
        date = min(dates)[2] if dates else None
        route = min(routes)[2] if routes else None
        if route is None:
            route = _generic_from_to(lines, index, upper, index)
        if date is None or route is None:
            continue
        key = (carrier, number, date, route)
        if key in seen:
            continue
        block = "\n".join(lines[index:upper])
        operating_carrier, operating_number = _operating_identity(block)
        parsed.append(
            _airline_parsed_flight(
                carrier,
                number,
                date,
                route[0],
                route[1],
                None,
                None,
                confirmation,
                None,
                {},
                operating_carrier,
                operating_number,
            )
        )
        seen.add(key)
    return parsed


def _generic_flight_identity(line: str) -> "Optional[tuple[str, int]]":
    if len(line) > 200:
        return None
    if re.fullmatch(
        r"\s*[A-Z0-9]{2}\s*-?\s*\d{1,4}\s+operated\s+by\s+.{1,100}\s*",
        line,
        re.IGNORECASE,
    ):
        return None
    suffix = r"(?:\s+operated\s+by\s+.{1,80})?"
    named = re.fullmatch(
        r"\s*(?:Flight(?:\s+number)?\s*:?\s*)?"
        r"(?P<carrier>{})(?:\s+(?:Flight\s*)?"
        r"(?:(?P<designator>[A-Z][A-Z0-9]|[0-9][A-Z])\s+)?)"
        r"(?P<number>\d{{1,4}}){}\s*".format(_EMAIL_CARRIER_PATTERN, suffix),
        line,
        re.IGNORECASE,
    )
    bare = re.fullmatch(
        r"\s*(?:Flight(?:\s+number)?\s*:?\s*)?"
        r"(?P<carrier>[A-Z0-9]{{2}})\s*-?\s*(?P<number>\d{{1,4}}){}\s*".format(suffix),
        line,
        re.IGNORECASE,
    )
    match = named or bare
    if match is None:
        return None
    carrier = _resolve_carrier(match.group("carrier"))
    designator = match.groupdict().get("designator")
    if designator and _resolve_carrier(designator) != carrier:
        return None
    return (carrier, int(match.group("number"))) if carrier else None


def _generic_date(line: str, default_year: int) -> "Optional[str]":
    if len(line) > 120:
        return None
    match = re.fullmatch(
        r"\s*(?:(?:Travel|Departure|Service)\s+)?Date\s*:\s*(.{1,40})\s*|\s*(.{1,40})\s*",
        line,
        re.IGNORECASE,
    )
    if match is None:
        return None
    value = (match.group(1) or match.group(2)).strip()
    value = re.sub(r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?[,]?\s+", "", value,
                   flags=re.IGNORECASE)
    for format_string in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%B %d", "%b %d"):
        try:
            parsed = datetime.strptime(value, format_string)
            year = parsed.year if "%Y" in format_string else default_year
            return parsed.date().replace(year=year).isoformat()
        except (OverflowError, ValueError):
            continue
    return None


def _generic_route(line: str) -> "Optional[tuple[Optional[str], Optional[str]]]":
    if len(line) > 180:
        return None
    match = re.fullmatch(
        r"\s*(?:Route\s*:\s*)?(?P<origin>[A-Z]{3})\s*(?:to|->|→|–)\s*"
        r"(?P<dest>[A-Z]{3})\s*",
        line,
        re.IGNORECASE,
    )
    return (match.group("origin").upper(), match.group("dest").upper()) if match else None


def _generic_from_to(lines, lower: int, upper: int, flight_index: int):
    endpoints = []
    for index in range(lower, upper):
        if len(lines[index]) > 100:
            continue
        match = re.fullmatch(
            r"\s*(From|Origin|To|Destination)\s*:\s*([A-Z]{3})\s*",
            lines[index],
            re.IGNORECASE,
        )
        if match:
            kind = "from" if match.group(1).casefold() in {"from", "origin"} else "to"
            endpoints.append((abs(index - flight_index), kind, match.group(2).upper()))
    origins = sorted(item for item in endpoints if item[1] == "from")
    destinations = sorted(item for item in endpoints if item[1] == "to")
    return (origins[0][2], destinations[0][2]) if origins and destinations else None


def _generic_confirmation(text: str) -> "Optional[str]":
    for line in text.splitlines():
        if len(line) > 120:
            continue
        match = re.fullmatch(
            r"\s*(?:Confirmation(?:\s+(?:code|number))?|Record locator|Booking reference)"
            r"\s*[:#]\s*([A-Z0-9]{5,8})\s*", line, re.IGNORECASE
        )
        if match:
            return match.group(1).upper()
    return None


def _operating_identity(text: str) -> "tuple[Optional[str], Optional[int]]":
    for line in text.splitlines():
        if len(line) > 240:
            continue
        match = re.fullmatch(
            r"\s*(?:(?P<prefix>.{{1,100}}?)\s+)?"
            r"Operated\s+by\s+(?P<carrier>{})(?:\s+as\s+.{{1,80}})?\s*".format(
                _EMAIL_CARRIER_TOKEN_PATTERN
            ),
            line,
            re.IGNORECASE,
        )
        if match is None:
            continue
        carrier = _resolve_carrier(match.group("carrier"))
        explicit_flight = re.fullmatch(
            r"\s*([A-Z0-9]{2})\s*-?\s*(\d{1,4})\s*",
            match.group("prefix") or "",
            re.IGNORECASE,
        )
        explicit = (
            _resolve_carrier(explicit_flight.group(1)) if explicit_flight else None
        )
        number = (
            int(explicit_flight.group(2))
            if explicit_flight and explicit == carrier and carrier
            else None
        )
        return carrier, number
    return None, None


def _clock_value(value: "Optional[str]") -> "Optional[str]":
    if value is None:
        return None
    match = TIME_RE.fullmatch(value.strip())
    return match.group(1) if match else None


def _receipt_destination_line(
    value: str,
) -> "tuple[Optional[str], Optional[str]]":
    """Split the known destination suffix without interpreting the preceding clock."""
    normalized = value.strip()
    for city in sorted(AIRLINE_EMAIL_CITY_TO_IATA, key=len, reverse=True):
        match = re.search(r"(?:^|\s)({})$".format(re.escape(city)), normalized, re.IGNORECASE)
        if match:
            preceding = normalized[: match.start()].strip()
            return preceding or None, match.group(1)
    return None, None


def _clock_in(value: str) -> "Optional[str]":
    match = TIME_RE.search(value)
    return match.group(1) if match else None


def _airline_email_city(value: str) -> "Optional[str]":
    normalized = re.sub(r"\s+", " ", value).strip().upper()
    return AIRLINE_EMAIL_CITY_TO_IATA.get(normalized)


def _airline_parsed_flight(
    carrier: str,
    number: int,
    date: str,
    origin: "Optional[str]",
    dest: "Optional[str]",
    dep_time: "Optional[str]",
    arr_time: "Optional[str]",
    conf_code: "Optional[str]",
    seat: "Optional[str]",
    hints: dict,
    operating_carrier: "Optional[str]" = None,
    operating_number: "Optional[int]" = None,
) -> ParsedFlight:
    return ParsedFlight(
        leg=FlightLeg(
            carrier=carrier,
            number=number,
            date=date,
            origin=origin,
            dest=dest,
            sched_dep_iso=_time_to_iso(date, dep_time, origin),
            # Arrival clocks remain on the service date; overnight rollover is deferred.
            sched_arr_iso=_time_to_iso(date, arr_time, dest),
            conf_code=conf_code,
            seat=seat,
            operating_carrier=operating_carrier,
            operating_number=operating_number,
        ),
        hints=dict(hints),
    )


# --------------------------------------------------------------------------
# Calendar events
# --------------------------------------------------------------------------


def parse_calendar_events(text: str, default_year: int) -> "list[ParsedFlight]":
    parsed = []
    for event_date, title, fields, notes in _event_blocks(text, default_year):
        designators = _designators(title, notes)
        if not designators or not AIRLINE_RE.search("{} {}".format(title, notes)):
            continue
        routes = _routes(title, fields.get("location", ""), len(designators))
        hints = _hints(title, fields, notes)
        for index, (carrier, number) in enumerate(designators):
            origin, dest = routes[index]
            dep, arr = _leg_times(notes, carrier, number, origin, dest)
            if dep is None or arr is None:
                fallback_dep, fallback_arr = _window_times(fields.get("window", ""))
                dep = dep or fallback_dep
                arr = arr or fallback_arr
            parsed.append(
                ParsedFlight(
                    leg=FlightLeg(
                        carrier=carrier,
                        number=number,
                        date=event_date,
                        origin=origin,
                        dest=dest,
                        sched_dep_iso=_time_to_iso(event_date, dep, origin),
                        sched_arr_iso=_time_to_iso(event_date, arr, dest),
                        conf_code=_confirmation_code("{} {}".format(title, notes)),
                        seat=_seat_for_leg(notes, carrier, number),
                    ),
                    hints=hints,
                )
            )
    return parsed


def parse_schedule_change(notes: str) -> "Optional[dict]":
    if not re.search(r"\byour flight changed\b", notes, re.IGNORECASE):
        return None
    schedule = re.search(
        r"\bNEW FLIGHT\b(?P<section>.*?)(?=\bORIGINAL FLIGHT\b|$)", notes, re.IGNORECASE
    )
    flight = next(
        (
            match
            for match in re.finditer(
                r"\b([A-Z]{2})\s*-?\s*(\d{1,4})\b",
                schedule.group("section") if schedule else "",
            )
            if match.group(1) not in {"AM", "PM"}
        ),
        None,
    )
    new_dep = _labelled_time(notes, "New depart time")
    new_arr = _labelled_time(notes, "New arrival time")
    original = re.search(r"\bORIGINAL FLIGHT\b(?P<section>.*)", notes, re.IGNORECASE)
    original_times = TIME_RE.findall(original.group("section")) if original else []
    if flight is None or new_dep is None or new_arr is None or len(original_times) < 2:
        return None
    return {
        "flight": "{}{}".format(flight.group(1), flight.group(2)),
        "new_dep": new_dep,
        "new_arr": new_arr,
        "original_dep": _normalize_time(original_times[0]),
        "original_arr": _normalize_time(original_times[1]),
    }


def parse_cancellation(notes: str) -> "Optional[str]":
    """Return the confirmation code an itinerary-cancellation notice refers to.

    Codes are identity keys over our own ingested stream: a notice that
    references an existing code with cancellation language should update THAT
    itinerary's status rather than spawn a new flight.
    """
    # Declarative statements about THIS booking only. Booking receipts carry
    # fare-rule boilerplate ("risk free cancellation period", "will result in
    # cancellation of your remaining reservation") that must never read as a
    # cancellation notice.
    declarative = (
        r"\b(?:has been|have been|was|were|is|are)\s+cancell?ed\b"
        r"|\b(?:flight|trip|itinerary|booking|reservation)\s+(?:has\s+been\s+|was\s+|is\s+)?cancell?ed\b"
        r"|\bcancell?ed\s+(?:flight|trip|itinerary|booking|reservation)\b"
        r"|\bcancellation\s+(?:confirmation|notice|confirmed)\b"
    )
    if not re.search(declarative, notes, re.IGNORECASE):
        return None
    return _confirmation_code(notes)


def parse_cancellations(text: str, default_year: int) -> "list[str]":
    """Confirmation codes referenced with cancellation language anywhere.

    Walks every raw event block: a standalone cancellation notice usually has
    no parseable route or times, so it never survives
    :func:`parse_calendar_events` and must be harvested from the raw blocks.
    """
    codes: "list[str]" = []
    for _event_date, title, _fields, notes in _event_blocks(text, default_year):
        code = parse_cancellation("{} {}".format(title or "", notes))
        if code and code not in codes:
            codes.append(code)
    return codes


def parse_passenger(notes: str) -> "Optional[str]":
    match = re.search(
        r"(?:Passenger Info\s+)?Name:\s*([A-Z][A-Z .'-]*?)"
        r"(?=\s+(?:SkyMiles|AAdvantage|MileagePlus|Loyalty|Ticket|#|FLIGHT|SEAT|Visit)\b|$)",
        notes,
    )
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else None


def _event_blocks(text: str, default_year: int):
    current_date = "{}-01-01".format(default_year)
    title = None
    fields = {}
    notes = []
    reading_notes = False

    def flush():
        if title is not None:
            yield current_date, title, fields, " ".join(notes).strip()

    for line in text.splitlines():
        day = DAY_RE.match(line)
        if day:
            yield from flush()
            current_date = datetime.strptime(
                "{} {} {}".format(day.group("month"), day.group("day"), day.group("year")),
                "%b %d %Y",
            ).date().isoformat()
            title, fields, notes, reading_notes = None, {}, [], False
            continue
        field = FIELD_RE.match(line)
        if field and title is not None:
            key, value = field.group("key").lower(), field.group("value")
            if key == "notes":
                reading_notes = True
            else:
                fields[key] = value
                reading_notes = False
            continue
        heading = TITLE_RE.match(line)
        if heading and not _is_time_window(heading.group("title")):
            yield from flush()
            title, fields, notes, reading_notes = heading.group("title"), {}, [], False
            continue
        if title is not None and _is_time_window(line.strip()):
            fields["window"] = line.strip()
            reading_notes = False
            continue
        if reading_notes and line.startswith("    "):
            notes.append(line.strip())
    yield from flush()


def _designators(title: str, notes: str) -> "list[tuple[str, int]]":
    explicit = _explicit_designators(title)
    if explicit:
        return explicit
    combined = "{} {}".format(title, notes)
    for name, carrier in AIRLINE_NAME_TO_IATA.items():
        airline = re.search(
            r"\b{}(?:\s+airlines?)?\b".format(name), combined, re.IGNORECASE
        )
        if airline is None:
            continue
        flight_numbers = re.search(
            r"\bFlight\s*:?[ ]*(\d{1,4}(?:\s*(?:→|->)\s*\d{1,4})*)",
            combined,
            re.IGNORECASE,
        )
        if flight_numbers:
            return [
                (_resolve_carrier(carrier), int(value))
                for value in re.findall(r"\d{1,4}", flight_numbers.group(1))
                if _resolve_carrier(carrier) is not None
            ]
        after_airline = combined[airline.end() : airline.end() + 80]
        number = re.search(
            r"\b(?:Flight\s*:?[ ]*)?(\d{1,4})\b", after_airline, re.IGNORECASE
        )
        if number:
            resolved = _resolve_carrier(carrier)
            return [(resolved, int(number.group(1)))] if resolved else []
    return _explicit_designators(notes)


def _explicit_designators(value: str) -> "list[tuple[str, int]]":
    return [
        (match.group(1), int(match.group(2)))
        for match in re.finditer(r"\b([A-Z]{2})\s*-?\s*(\d{1,4})\b", value)
        if match.group(1) in KNOWN_CARRIERS
    ]


def _routes(title: str, location: str, count: int) -> "list[tuple[Optional[str], Optional[str]]]":
    nodes = _route_nodes(location) or _route_nodes(title)
    if len(nodes) >= count + 1:
        return [(nodes[index], nodes[index + 1]) for index in range(count)]
    origin = nodes[0] if nodes else _city_in(location)
    dest = nodes[1] if len(nodes) > 1 else _city_after_to(location) or _city_after_to(title)
    return [(origin, dest)] * count


def _route_nodes(value: str) -> "list[str]":
    codes = re.findall(r"\b([A-Z]{3})\b", value)
    if len(codes) >= 2:
        return codes
    tokens = value.split()
    marker = next(
        (index for index, token in enumerate(tokens) if token.casefold() == "to"), -1
    )
    if marker > 0 and marker < len(tokens) - 1:
        origin = _endpoint_iata(" ".join(tokens[:marker]))
        dest = _endpoint_iata(" ".join(tokens[marker + 1 :]))
        return [code for code in (origin, dest) if code]
    return codes


def _endpoint_iata(value: str) -> "Optional[str]":
    codes = re.findall(r"\b([A-Z]{3})\b", value)
    return codes[0] if codes else _city_in(value)


def _city_in(value: str) -> "Optional[str]":
    lowered = value.lower()
    for city in sorted(CITY_TO_IATA, key=len, reverse=True):
        if city in lowered:
            return CITY_TO_IATA[city]
    return None


def _city_after_to(value: str) -> "Optional[str]":
    match = re.search(r"\bto\s+(.+)", value, re.IGNORECASE)
    return _city_in(match.group(1)) if match else None


def _leg_times(
    notes: str, carrier: str, number: int, origin: Optional[str], dest: Optional[str]
) -> "tuple[Optional[str], Optional[str]]":
    if origin is None or dest is None:
        return None, None
    time = r"\d{1,2}:\d{2}\s*(?:AM|PM)"
    match = re.search(
        r"\b{origin}\b(?:\s+[A-Za-z/]+){{0,5}}\s+(?P<dep>{time})\s+{carrier}\s*-?\s*{number}\b\s+"
        r"{dest}\b(?:\s+[A-Za-z/]+){{0,5}}\s+(?P<arr>{time})".format(
            origin=origin, dest=dest, carrier=carrier, number=number, time=time
        ),
        notes,
        re.IGNORECASE,
    )
    if match:
        return match.group("dep"), match.group("arr")
    departure = re.search(
        r"\b{origin}\b(?:\s+[A-Za-z/]+){{0,5}}\s+(?P<dep>{time})\s+{carrier}\s*-?\s*{number}\b".format(
            origin=origin, carrier=carrier, number=number, time=time
        ),
        notes,
        re.IGNORECASE,
    )
    if departure is None:
        return None, None
    arrival = re.search(
        r"\b{dest}\b(?:\s+[A-Za-z/]+){{0,5}}\s+(?P<arr>{time})".format(dest=dest, time=time),
        notes[departure.end() : departure.end() + 240],
        re.IGNORECASE,
    )
    return (departure.group("dep"), arrival.group("arr")) if arrival else (None, None)


def _window_times(window: str) -> "tuple[Optional[str], Optional[str]]":
    values = re.findall(r"\b\d{1,2}:\d{2}\b", window)
    return (values[0], values[1]) if len(values) == 2 else (None, None)


def _time_to_iso(date: str, value: Optional[str], airport: Optional[str]) -> "Optional[str]":
    timezone = airport_timezone(airport)
    if value is None or timezone is None:
        return None
    normalized = _normalize_time(value)
    for format_string in ("%Y-%m-%d %I:%M %p", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(
                "{} {}".format(date, normalized), format_string
            ).replace(tzinfo=ZoneInfo(timezone)).isoformat()
        except ValueError:
            continue
    return None


def _normalize_time(value: str) -> str:
    spaced = re.sub(r"(?i)(am|pm)\b", r" \1", value.strip())
    return re.sub(r"\s+", " ", spaced).upper()


def _confirmation_code(value: str) -> "Optional[str]":
    match = re.search(
        r"\bConf(?:#|:|irmation)(?:\s+code)?[#:\s]*([A-Z0-9]{5,8})\b", value, re.IGNORECASE
    )
    return match.group(1).upper() if match else None


def _seat_for_leg(notes: str, carrier: str, number: int) -> "Optional[str]":
    segment = re.search(
        r"\b{carrier}\s*-?\s*{number}\b(?P<tail>.*?)(?=\b[A-Z]{{2}}\s*-?\s*\d{{1,4}}\b|$)".format(
            carrier=carrier, number=number
        ),
        notes,
        re.IGNORECASE,
    )
    value = segment.group("tail") if segment else notes
    match = re.search(r"\bSeat:?\s*(\d{1,2}[A-F])\b", value, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    airline_name = next(
        (name for name, iata in AIRLINE_NAME_TO_IATA.items() if iata == carrier),
        carrier,
    )
    ticket_seat = re.search(
        r"\b(?:{carrier}|{airline_name})\s+{number}\s+(\d{{1,2}}[A-F])\b".format(
            carrier=carrier, airline_name=airline_name, number=number
        ),
        notes,
        re.IGNORECASE,
    )
    return ticket_seat.group(1).upper() if ticket_seat else None


def _hints(title: str, fields: dict, notes: str) -> dict:
    attendees = re.findall(r"[\w.+-]+@[\w.-]+", fields.get("attendees", ""))
    hints = {
        "title": title,
        "calendar": fields.get("calendar", ""),
        "attendees": attendees,
        "source_id": fields.get("id", ""),
        "location": fields.get("location", ""),
        "notes_excerpt": notes[:500],
    }
    passenger = parse_passenger(notes)
    if passenger:
        hints["passenger_name"] = passenger
    return hints


def _labelled_time(notes: str, label: str) -> "Optional[str]":
    match = re.search(
        r"{}:?\s*({})".format(label, TIME_RE.pattern), notes, re.IGNORECASE
    )
    return _normalize_time(match.group(1)) if match else None


def _is_time_window(value: str) -> bool:
    return bool(re.match(r"^\d{1,2}:\d{2}\s*[–-]\s*\d{1,2}:\d{2}$", value))
