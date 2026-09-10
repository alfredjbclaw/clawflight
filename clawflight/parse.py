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
    "american": "AA",
    "united": "UA",
    "jetblue": "B6",
    "southwest": "WN",
    "alaska": "AS",
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
    r"\b(Flight|Airlines?|Delta|American|United|JetBlue|Southwest|Alaska)\b",
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
        int(match.group(1)): match.group(2).upper()
        for match in re.finditer(
            r"(?im)^\s*DELTA\s+(\d{1,4})\s+(\d{1,2}[A-F])\s*$", text
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
        flights = list(re.finditer(r"(?im)^\s*DELTA\s+(\d{1,4})\s*$", section))
        for flight_index, flight in enumerate(flights):
            number = int(flight.group(1))
            key = ("DL", number, date)
            if key in seen:
                continue
            flight_end = (
                flights[flight_index + 1].start()
                if flight_index + 1 < len(flights)
                else len(section)
            )
            details = section[flight.end() : flight_end]
            route = re.search(
                r"(?im)^\s*(?:Standby\s+)?([A-Z][A-Z -]*?)\s*$\s*"
                r"^\s*\d{1,2}:\d{2}\s*(?:AM|PM)\s+([A-Z][A-Z -]*?)\s*$\s*"
                r"^\s*\d{1,2}:\d{2}\s*(?:AM|PM)\s*$",
                details,
            )
            origin = _airline_email_city(route.group(1)) if route else None
            dest = _airline_email_city(route.group(2)) if route else None
            parsed.append(
                _airline_parsed_flight(
                    "DL", number, date, origin, dest,
                    confirmation.group(1).upper(), seats.get(number), hints,
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
        carrier = re.search(r"(?im)^\s*(American Airlines)\s+(\d{1,4})\s*$", block)
        if carrier is None:
            continue
        prefix = block[: carrier.start()]
        airports = re.findall(r"(?im)^\s*([A-Z]{3})\s*$", prefix)
        times = TIME_RE.findall(prefix)
        if len(airports) < 2 or len(times) < 2:
            continue
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
            block[carrier.end() :],
        )
        parsed.append(
            _airline_parsed_flight(
                "AA", int(carrier.group(2)), date, airports[0], airports[1],
                confirmation.group(1).upper(),
                seat_match.group(1).upper() if seat_match else None,
                hints,
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
    block = re.search(
        r"(?im)^\s*(Delta)\s+(\d{1,4})\s*$\s*"
        r"^\s*(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s*([A-Z][a-z]+)\s+(\d{1,2})\s*$\s*"
        r"^\s*([A-Za-z][A-Za-z -]+?)\s*$\s*"
        r"^\s*(\d{1,2}:\d{2}\s*(?:AM|PM))\s*$\s*"
        r"^\s*([A-Za-z][A-Za-z -]+?)\s*$\s*"
        r"^\s*(\d{1,2}:\d{2}\s*(?:AM|PM))\s*$",
        text[new_info.end() :],
    )
    if block is None:
        return []
    try:
        date = datetime.strptime(
            "{} {} {}".format(block.group(3), block.group(4), default_year),
            "%B %d %Y",
        ).date().isoformat()
    except (OverflowError, ValueError):
        return []
    return [
        _airline_parsed_flight(
            "DL", int(block.group(2)), date,
            _airline_email_city(block.group(5)),
            _airline_email_city(block.group(7)),
            confirmation.group(1).upper(), None,
            {"notes_excerpt": "Your flight changed"},
        )
    ]


def _airline_email_city(value: str) -> "Optional[str]":
    normalized = re.sub(r"\s+", " ", value).strip().upper()
    return AIRLINE_EMAIL_CITY_TO_IATA.get(normalized)


def _airline_parsed_flight(
    carrier: str,
    number: int,
    date: str,
    origin: "Optional[str]",
    dest: "Optional[str]",
    conf_code: "Optional[str]",
    seat: "Optional[str]",
    hints: dict,
) -> ParsedFlight:
    return ParsedFlight(
        leg=FlightLeg(
            carrier=carrier,
            number=number,
            date=date,
            origin=origin,
            dest=dest,
            sched_dep_iso=None,
            sched_arr_iso=None,
            conf_code=conf_code,
            seat=seat,
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
                (carrier, int(value))
                for value in re.findall(r"\d{1,4}", flight_numbers.group(1))
            ]
        after_airline = combined[airline.end() : airline.end() + 80]
        number = re.search(
            r"\b(?:Flight\s*:?[ ]*)?(\d{1,4})\b", after_airline, re.IGNORECASE
        )
        if number:
            return [(carrier, int(number.group(1)))]
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
