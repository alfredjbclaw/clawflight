"""Airport table loading and great-circle geometry.

The airport table ships inside the package (``clawflight/data/airports.csv``)
so importing :mod:`clawflight.parse` never depends on a test fixture path.
Callers may still point :func:`load_airports` at their own CSV.
"""
from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Dict, Optional

from .models import Airport, Position


DATA_DIR = Path(__file__).resolve().parent / "data"
AIRPORTS_CSV = DATA_DIR / "airports.csv"

_DEFAULT_CACHE: Dict[str, Airport] = {}


def load_airports(csv_path: str) -> Dict[str, Airport]:
    airports: Dict[str, Airport] = {}
    with open(csv_path, encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            iata = (row.get("iata") or "").strip().upper()
            name = (row.get("name") or "").strip()
            tz = (row.get("tz") or "").strip()
            try:
                lat = float(row.get("lat") or "")
                lon = float(row.get("lon") or "")
            except ValueError:
                continue
            if iata and name and tz:
                airports[iata] = Airport(iata=iata, name=name, lat=lat, lon=lon, tz=tz)
    return airports


def default_airports() -> Dict[str, Airport]:
    """The packaged airport table, loaded once and returned as a fresh dict."""
    if not _DEFAULT_CACHE:
        try:
            _DEFAULT_CACHE.update(load_airports(str(AIRPORTS_CSV)))
        except OSError:
            return {}
    return dict(_DEFAULT_CACHE)


def gc_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    latitude_delta = math.radians(lat2 - lat1)
    longitude_delta = math.radians(lon2 - lon1)
    first_latitude = math.radians(lat1)
    second_latitude = math.radians(lat2)
    haversine = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(first_latitude)
        * math.cos(second_latitude)
        * math.sin(longitude_delta / 2) ** 2
    )
    return 6371.0088 * 2 * math.asin(math.sqrt(haversine))


def great_circle_nm(first: Airport, second: Airport) -> float:
    return gc_km(first.lat, first.lon, second.lat, second.lon) / 1.852


def progress_fraction(pos: Position, origin: Airport, dest: Airport) -> float:
    total_km = gc_km(origin.lat, origin.lon, dest.lat, dest.lon)
    if total_km <= 0:
        return 0.0
    remaining_km = gc_km(pos.lat, pos.lon, dest.lat, dest.lon)
    return min(1.0, max(0.0, 1.0 - remaining_km / total_km))


def eta_epoch(origin: Airport, dest: Airport, pos: Position) -> Optional[float]:
    del origin
    if pos.gs_kt is None or pos.gs_kt <= 50:
        return None
    remaining_km = gc_km(pos.lat, pos.lon, dest.lat, dest.lon)
    return pos.ts_epoch + remaining_km / (pos.gs_kt * 1.852) * 3600
