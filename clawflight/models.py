"""Immutable value types shared by every stage of the pipeline."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


MILESTONES = ("takeoff", "halfway", "landing")
EVENT_KINDS = (
    "takeoff",
    "halfway",
    "landing",
    "delay",
    "gate_change",
    "schedule_change",
    "cancelled",
    "diverted",
    "stale_data",
    "backup_reminder",
    "tracking_started",
    "landing_hold",
    "push_stale",
    "trip_card",
    "arrival",
    "connection_alert",
)
AIRLINE_ICAO = {
    "AA": "AAL",
    "DL": "DAL",
    "UA": "UAL",
    "B6": "JBU",
    "WN": "SWA",
    "AS": "ASA",
    "NK": "NKS",
    "F9": "FFT",
    "HA": "HAL",
    "G4": "AAY",
    "BA": "BAW",
    "AF": "AFR",
    "LH": "DLH",
    "VS": "VIR",
    "EK": "UAE",
}


@dataclass(frozen=True)
class Airport:
    iata: str
    name: str
    lat: float
    lon: float
    tz: str


@dataclass(frozen=True)
class FlightLeg:
    carrier: str
    number: int
    date: str
    origin: Optional[str]
    dest: Optional[str]
    sched_dep_iso: Optional[str]
    sched_arr_iso: Optional[str]
    conf_code: Optional[str]
    seat: Optional[str]
    operating_carrier: Optional[str] = None
    operating_number: Optional[int] = None


@dataclass(frozen=True)
class PersonRef:
    key: str
    name: str


@dataclass(frozen=True)
class FlightRecord:
    flight_id: str
    leg: FlightLeg
    person: PersonRef
    sources: Tuple[str, ...]
    backup_group: Optional[str]
    status: str
    notes: Tuple[str, ...]


@dataclass(frozen=True)
class Position:
    lat: float
    lon: float
    alt_ft: Optional[float]
    gs_kt: Optional[float]
    vert_rate_fpm: Optional[float]
    ts_epoch: float


@dataclass(frozen=True)
class Observation:
    flight_id: str
    position: Optional[Position]
    origin_delay: Optional[dict]
    dest_delay: Optional[dict]
    fetched_at_epoch: float


@dataclass(frozen=True)
class FlightEvent:
    flight_id: str
    kind: str
    message: str
    critical: bool
    at_epoch: float


@dataclass(frozen=True)
class FlightUpdate:
    flight_number: str
    status: Optional[str]
    departure_scheduled: Optional[str]
    departure_revised: Optional[str]
    arrival_scheduled: Optional[str]
    arrival_revised: Optional[str]
    departure_terminal: Optional[str]
    departure_gate: Optional[str]
    arrival_terminal: Optional[str]
    arrival_gate: Optional[str]
    service_date: Optional[str] = None
    origin: Optional[str] = None
    dest: Optional[str] = None
    operating_carrier: Optional[str] = None
    operating_number: Optional[int] = None
    arrival_baggage_belt: Optional[str] = None


def icao_callsign(carrier: str, number: int) -> str:
    return "{}{}".format(AIRLINE_ICAO.get(carrier, carrier), number)


def polling_callsign(leg: "FlightLeg") -> str:
    """ICAO callsign using operating carrier/number when available for ADS-B.

    Both operating_carrier and operating_number must be present and valid
    to use operating identity; partial data falls back to the marketed flight.
    """
    if (
        isinstance(leg.operating_carrier, str)
        and leg.operating_carrier.strip()
        and isinstance(leg.operating_number, int)
        and leg.operating_number > 0
    ):
        return icao_callsign(leg.operating_carrier.strip().upper(), leg.operating_number)
    return icao_callsign(leg.carrier, leg.number)


def flight_ident(carrier: str, number: int, date: str) -> str:
    return "{}{}-{}".format(carrier, number, date)
