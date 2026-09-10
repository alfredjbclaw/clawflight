"""The keyless public feeds, driven entirely through a fake HTTP getter.

No socket is opened here. `tests/test_no_network` in CI runs the whole suite
with connect/bind blocked, so a real request would fail the build.
"""
from __future__ import annotations

import json

import pytest

from clawflight.adapters.feed_adsb import (
    FAA_CACHE_SECONDS,
    PublicFeeds,
    offline_observer,
    urllib_get,
)
from clawflight.models import FlightLeg, FlightRecord, PersonRef


FAA_XML = (
    "<AIRPORT_STATUS_INFORMATION><Delay_type><Ground_Delay_List><Ground_Delay>"
    "<ARPT>JFK</ARPT><Reason>weather</Reason><Avg>40 minutes</Avg>"
    "</Ground_Delay></Ground_Delay_List></Delay_type></AIRPORT_STATUS_INFORMATION>"
)


def _adsb(callsign: str = "DAL767") -> str:
    return json.dumps(
        {
            "now": 1789000000000,
            "ac": [
                {
                    "flight": callsign + " ",
                    "lat": 39.5,
                    "lon": -95.2,
                    "alt_baro": 35000,
                    "gs": 460.0,
                    "baro_rate": 0,
                }
            ],
        }
    )


def _record(**overrides) -> FlightRecord:
    fields = dict(
        carrier="DL", number=767, date="2026-09-12",
        origin="JFK", dest="LAX",
        sched_dep_iso="2026-09-12T16:55:00-04:00",
        sched_arr_iso="2026-09-12T20:20:00-07:00",
        conf_code="ABC123", seat="22E",
    )
    fields.update(overrides)
    leg = FlightLeg(**fields)
    return FlightRecord(
        flight_id="DL767-2026-09-12",
        leg=leg,
        person=PersonRef("alex", "Alex"),
        sources=("manual:2026-09-12",),
        backup_group=None,
        status="scheduled",
        notes=(),
    )


class _FakeHttp:
    def __init__(self, responses) -> None:
        self.responses = responses
        self.calls = []

    def __call__(self, url: str):
        self.calls.append(url)
        for fragment, body in self.responses.items():
            if fragment in url:
                return body() if callable(body) else body
        return None


def test_a_position_is_read_for_the_right_callsign() -> None:
    http = _FakeHttp({"/callsign/": _adsb()})

    position = PublicFeeds(http).position_for(_record())

    assert position is not None
    assert (position.lat, position.lon) == (39.5, -95.2)
    assert position.alt_ft == 35000.0
    assert "DAL767" in http.calls[0]


def test_the_operating_carrier_wins_the_callsign() -> None:
    # A codeshare flies under the operator's callsign; asking for the marketed
    # one finds nothing at all.
    http = _FakeHttp({"/callsign/": _adsb("SKW5678")})

    position = PublicFeeds(http).position_for(
        _record(operating_carrier="OO", operating_number=5678)
    )

    assert "OO5678" in http.calls[0]
    assert position is None or position.lat == 39.5


def test_airport_conditions_are_parsed_and_attached() -> None:
    http = _FakeHttp({"/callsign/": _adsb(), "nasstatus": FAA_XML})

    observation = PublicFeeds(http).observe(_record())

    assert observation.flight_id == "DL767-2026-09-12"
    assert observation.position is not None
    assert observation.origin_delay["type"] == "ground_delay"
    assert observation.origin_delay["reason"] == "weather"
    assert observation.dest_delay is None  # LAX is clear in this payload


def test_airport_conditions_are_cached_across_a_sweep() -> None:
    # One sweep can cover several flights through the same hub; re-fetching per
    # flight would hammer a free service for no new information.
    clock = [1000.0]
    http = _FakeHttp({"/callsign/": _adsb(), "nasstatus": FAA_XML})
    feeds = PublicFeeds(http, clock=lambda: clock[0])

    feeds.observe(_record())
    feeds.observe(_record())
    faa_calls = [url for url in http.calls if "nasstatus" in url]
    assert len(faa_calls) == 1

    clock[0] += FAA_CACHE_SECONDS + 1
    feeds.observe(_record())
    assert len([url for url in http.calls if "nasstatus" in url]) == 2


def test_a_failed_fetch_degrades_instead_of_raising() -> None:
    # A free feed having a bad minute must not take the tracker down.
    feeds = PublicFeeds(_FakeHttp({}))

    observation = feeds.observe(_record())

    assert observation.position is None
    assert observation.origin_delay is None
    assert observation.fetched_at_epoch > 0


def test_malformed_feed_payloads_are_ignored() -> None:
    for body in ("not json", "[]", '{"unexpected": true}', ""):
        feeds = PublicFeeds(_FakeHttp({"/callsign/": body}))
        assert feeds.position_for(_record()) is None


def test_a_failed_condition_refresh_keeps_the_previous_answer() -> None:
    # Reporting "no delays" because the fetch failed would be a lie that
    # suppresses a real ground-stop alert.
    clock = [1000.0]
    responses = {"nasstatus": FAA_XML}
    http = _FakeHttp(responses)
    feeds = PublicFeeds(http, clock=lambda: clock[0])

    assert "JFK" in feeds.conditions()

    responses.pop("nasstatus")
    clock[0] += FAA_CACHE_SECONDS + 1

    assert "JFK" in feeds.conditions()


def test_the_feed_object_is_usable_directly_as_a_fetcher() -> None:
    # run_once takes a callable; PublicFeeds is one.
    feeds = PublicFeeds(_FakeHttp({"/callsign/": _adsb()}))

    assert feeds(_record()).position is not None


def test_the_offline_observer_contacts_nothing() -> None:
    observe = offline_observer(clock=lambda: 42.0)

    observation = observe(_record())

    assert observation.position is None
    assert observation.fetched_at_epoch == 42.0


def test_the_default_getter_returns_none_rather_than_raising(monkeypatch) -> None:
    def explode(*args, **kwargs):
        raise OSError("network is unreachable")

    monkeypatch.setattr("clawflight.adapters.feed_adsb.urlopen", explode)

    assert urllib_get("https://example.test/thing") is None


def test_feed_urls_are_https_only() -> None:
    from clawflight.adapters import feed_adsb

    assert feed_adsb.ADSB_BASE_URL.startswith("https://")
    assert feed_adsb.FAA_STATUS_URL.startswith("https://")


def test_a_hostile_callsign_cannot_escape_the_url_path() -> None:
    http = _FakeHttp({})
    feeds = PublicFeeds(http)

    # Carrier and number are validated upstream, but the URL builder must not
    # be the thing that trusts them: quote() keeps any value inside one path
    # segment.
    feeds.position_for(_record(carrier="D/", number=1))

    assert http.calls, "no request was made"
    segment = http.calls[0].split("/callsign/")[1]
    assert "/" not in segment and "?" not in segment and " " not in segment
