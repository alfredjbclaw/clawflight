"""Live observations from the keyless public feeds.

adsb.lol for aircraft positions, the FAA NAS feed for airport conditions.
Neither needs an account, a key, or a card — that is the whole reason the
default configuration works with no signup.

The HTTP call is injected. The default uses ``urllib.request`` from the
standard library, so this adds no dependency; tests pass a fake and never open
a socket. Every failure degrades to "no observation" rather than raising: a
flight tracker that dies because a free feed had a bad minute is worse than one
that quietly keeps its last known state.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Callable, Dict, List, Optional
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from ..models import FlightRecord, Observation, polling_callsign
from ..status import parse_adsb, parse_faa_nas


_LOGGER = logging.getLogger(__name__)

ADSB_BASE_URL = "https://api.adsb.lol/v2"
FAA_STATUS_URL = "https://nasstatus.faa.gov/api/airport-status-information"

DEFAULT_TIMEOUT = 8.0
#: Airport conditions change on the order of minutes, and one sweep may cover
#: many flights through the same hub. Re-fetching per flight would be rude to a
#: free service and slower for no benefit.
FAA_CACHE_SECONDS = 300.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

USER_AGENT = "clawflight/0.1 (+https://github.com/alfredjbclaw/clawflight)"

#: An HTTP getter: url -> body text, or None when the fetch failed.
HttpGet = Callable[[str], Optional[str]]


def urllib_get(url: str, timeout: float = DEFAULT_TIMEOUT) -> Optional[str]:
    """Fetch a URL with the standard library. Returns None on any failure."""
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - https only
            return response.read(MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")
    except (URLError, OSError, ValueError) as exc:
        _LOGGER.warning("feed fetch failed for %s: %s", url, exc)
        return None


class PublicFeeds:
    """Build :class:`Observation` values from the keyless public feeds."""

    def __init__(
        self,
        http_get: Optional[HttpGet] = None,
        *,
        adsb_base_url: str = ADSB_BASE_URL,
        faa_status_url: str = FAA_STATUS_URL,
        clock: Callable[[], float] = time.time,
        faa_cache_seconds: float = FAA_CACHE_SECONDS,
    ) -> None:
        self._get = http_get or urllib_get
        self._adsb_base_url = adsb_base_url.rstrip("/")
        self._faa_status_url = faa_status_url
        self._clock = clock
        self._faa_cache_seconds = faa_cache_seconds
        self._faa_conditions: Dict[str, List[dict]] = {}
        self._faa_fetched_at: Optional[float] = None

    # -- positions ---------------------------------------------------------

    def position_for(self, record: FlightRecord):
        """Current position for a record's flight, or None.

        The callsign prefers the *operating* carrier: a codeshare flies under
        the operator's callsign, so asking for the marketed one finds nothing.
        """
        callsign = polling_callsign(record.leg)
        url = "{}/callsign/{}".format(self._adsb_base_url, quote(callsign, safe=""))
        body = self._get(url)
        if body is None:
            return None
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            _LOGGER.warning("feed returned non-JSON for %s", callsign)
            return None
        if not isinstance(payload, dict):
            return None
        return parse_adsb(payload, callsign)

    # -- airport conditions ------------------------------------------------

    def conditions(self) -> Dict[str, List[dict]]:
        """FAA airport conditions, cached briefly across a sweep."""
        now = self._clock()
        if (
            self._faa_fetched_at is not None
            and now - self._faa_fetched_at < self._faa_cache_seconds
        ):
            return self._faa_conditions
        body = self._get(self._faa_status_url)
        # A failed refresh keeps the previous answer rather than pretending
        # every airport suddenly became clear.
        if body is not None:
            self._faa_conditions = parse_faa_nas(body)
            self._faa_fetched_at = now
        elif self._faa_fetched_at is None:
            self._faa_fetched_at = now
        return self._faa_conditions

    def _first_condition(self, airport: Optional[str]) -> Optional[dict]:
        if not airport:
            return None
        entries = self.conditions().get(airport.upper())
        return entries[0] if entries else None

    # -- the fetcher run_once expects --------------------------------------

    def observe(self, record: FlightRecord) -> Observation:
        return Observation(
            flight_id=record.flight_id,
            position=self.position_for(record),
            origin_delay=self._first_condition(record.leg.origin),
            dest_delay=self._first_condition(record.leg.dest),
            fetched_at_epoch=self._clock(),
        )

    def __call__(self, record: FlightRecord) -> Observation:
        return self.observe(record)


def offline_observer(clock: Callable[[], float] = time.time):
    """A fetcher that contacts nothing.

    Everything that does not need a live position still works: the watch
    window, schedule-change notes from ingested email, connection analysis,
    push-corroborated delays, and the whole delivery path.
    """

    def observe(record: FlightRecord) -> Observation:
        return Observation(
            flight_id=record.flight_id,
            position=None,
            origin_delay=None,
            dest_delay=None,
            fetched_at_epoch=clock(),
        )

    return observe
