"""Parsers for the three keyless public feeds.

adsb.lol positions, adsbdb routes, and the FAA NAS airport-status document.
Every function is total over untrusted payloads: malformed input returns None
or an empty mapping.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree

from .models import Position, icao_callsign


def parse_adsb(payload: dict, callsign: str) -> Optional[Position]:
    aircraft = payload.get("ac", [])
    timestamp = payload.get("now")
    if not isinstance(aircraft, list) or not isinstance(timestamp, (int, float)):
        return None

    normalized_callsign = callsign.strip().casefold()
    for candidate in aircraft:
        if not isinstance(candidate, dict):
            continue
        flight = candidate.get("flight")
        if not isinstance(flight, str) or flight.strip().casefold() != normalized_callsign:
            continue
        latitude = candidate.get("lat")
        longitude = candidate.get("lon")
        if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
            return None

        altitude = candidate.get("alt_baro")
        alt_ft = float(altitude) if isinstance(altitude, (int, float)) else None
        speed = candidate.get("gs")
        vertical_rate = candidate.get("baro_rate")
        return Position(
            lat=float(latitude),
            lon=float(longitude),
            alt_ft=alt_ft,
            gs_kt=float(speed) if isinstance(speed, (int, float)) else None,
            vert_rate_fpm=(
                float(vertical_rate) if isinstance(vertical_rate, (int, float)) else None
            ),
            ts_epoch=float(timestamp) / 1000,
        )
    return None


def parse_adsbdb_route(payload: dict) -> Optional[dict]:
    response = payload.get("response")
    if not isinstance(response, dict):
        return None
    route = response.get("flightroute")
    if not isinstance(route, dict):
        return None
    origin = route.get("origin")
    destination = route.get("destination")
    if not isinstance(origin, dict) or not isinstance(destination, dict):
        return None
    return {
        "origin_iata": origin.get("iata_code"),
        "dest_iata": destination.get("iata_code"),
        "origin_lat": origin.get("latitude"),
        "origin_lon": origin.get("longitude"),
        "dest_lat": destination.get("latitude"),
        "dest_lon": destination.get("longitude"),
    }


def parse_faa_nas(xml_text: str) -> Dict[str, List[dict]]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return {}

    conditions: Dict[str, List[dict]] = {}
    for entry in root.iter("Ground_Delay"):
        _add_condition(
            conditions,
            _child_text(entry, "ARPT"),
            "ground_delay",
            _child_text(entry, "Reason"),
            _detail(entry, ("Avg", "Max")),
        )
    for entry in root.iter("Delay"):
        arrival_departure = entry.find("Arrival_Departure")
        if arrival_departure is None:
            continue
        delay_type = arrival_departure.get("Type", "")
        delay_detail = _detail(arrival_departure, ("Min", "Max"))
        trend = _child_text(arrival_departure, "Trend")
        detail = "{}: {}".format(delay_type, delay_detail) if delay_type else delay_detail
        if trend:
            detail = "{}; Trend: {}".format(detail, trend)
        _add_condition(
            conditions,
            _child_text(entry, "ARPT"),
            "arrival_departure_delay",
            _child_text(entry, "Reason"),
            detail,
        )
    for entry in root.iter("Airport"):
        _add_condition(
            conditions,
            _child_text(entry, "ARPT"),
            "closure",
            _child_text(entry, "Reason"),
            _detail(entry, ("Start", "Reopen")),
        )
    for entry in root.iter("Ground_Stop"):
        _add_condition(
            conditions,
            _child_text(entry, "ARPT"),
            "ground_stop",
            _child_text(entry, "Reason"),
            _detail(entry, ("Start", "End_Time", "End")),
        )
    return conditions


def flightaware_link(carrier: str, number: int) -> str:
    return "https://www.flightaware.com/live/flight/{}".format(icao_callsign(carrier, number))


def fr24_link(carrier: str, number: int) -> str:
    return "https://www.flightradar24.com/data/flights/{}{}".format(carrier, number).lower()


def _add_condition(
    conditions: Dict[str, List[dict]],
    airport: str,
    kind: str,
    reason: str,
    detail: str,
) -> None:
    if airport:
        conditions.setdefault(airport, []).append(
            {"type": kind, "reason": reason, "detail": detail}
        )


def _child_text(element: "ElementTree.Element", name: str) -> str:
    child = element.find(name)
    return child.text.strip() if child is not None and child.text else ""


def _detail(element: "ElementTree.Element", names: Tuple[str, ...]) -> str:
    details = []
    for name in names:
        value = _child_text(element, name)
        if value:
            details.append("{}: {}".format(name.replace("_", " "), value))
    return "; ".join(details)
