import json

from clawflight.status import (
    flightaware_link,
    fr24_link,
    parse_adsb,
    parse_adsbdb_route,
    parse_faa_nas,
)


def test_parse_adsb_matches_trimmed_callsign_case_insensitively(fixtures) -> None:
    payload = json.loads((fixtures / "adsb_airborne.json").read_text())

    position = parse_adsb(payload, "aal4912")

    assert position is not None
    assert position.lat == 41.123016
    assert position.lon == -75.242493
    assert position.alt_ft == 23000.0
    assert position.gs_kt == 448.6
    assert position.vert_rate_fpm == 0.0
    assert position.ts_epoch == 1783739519.501


def test_parse_adsb_returns_none_when_callsign_is_absent(fixtures) -> None:
    payload = json.loads((fixtures / "adsb_airborne.json").read_text())

    assert parse_adsb(payload, "AAL9999") is None
    assert parse_adsb({"ac": [], "now": 1783739519501}, "AAL4912") is None


def test_parse_adsb_treats_ground_altitude_as_unknown(fixtures) -> None:
    # Given: an aircraft reporting the literal string "ground" for altitude.
    payload = json.loads((fixtures / "adsb_airborne.json").read_text())

    position = parse_adsb(payload, "DAL767")

    # Then: altitude is unknown rather than coerced to a number.
    assert position is not None
    assert position.alt_ft is None
    assert position.gs_kt == 12.0
    assert position.vert_rate_fpm is None


def test_parse_adsb_rejects_a_malformed_payload() -> None:
    assert parse_adsb({"ac": "not-a-list", "now": 1}, "AAL4912") is None
    assert parse_adsb({"ac": [], "now": "not-a-number"}, "AAL4912") is None
    assert (
        parse_adsb({"ac": [{"flight": "AAL4912", "lat": "x", "lon": 1}], "now": 1}, "AAL4912")
        is None
    )


def test_parse_adsbdb_route_reads_the_route_payload(fixtures) -> None:
    payload = json.loads((fixtures / "adsbdb_route.json").read_text())

    route = parse_adsbdb_route(payload)

    assert route == {
        "origin_iata": "ASE",
        "dest_iata": "DFW",
        "origin_lat": 39.2232,
        "origin_lon": -106.8688,
        "dest_lat": 32.8998,
        "dest_lon": -97.0403,
    }


def test_parse_adsbdb_route_returns_none_when_route_is_missing() -> None:
    assert parse_adsbdb_route({"response": {}}) is None
    assert parse_adsbdb_route({}) is None
    assert parse_adsbdb_route({"response": {"flightroute": {"origin": {}}}}) is None


def test_parse_faa_nas_collects_airport_conditions(fixtures) -> None:
    delays = parse_faa_nas((fixtures / "faa_nas.xml").read_text())

    assert delays["SFO"] == [
        {
            "type": "ground_delay",
            "reason": "weather / low ceilings",
            "detail": "Avg: 36 minutes; Max: 2 hours",
        }
    ]
    assert delays["EWR"][0]["type"] == "arrival_departure_delay"
    assert delays["EWR"][0]["reason"] == "TM Initiatives:SWAP:WX"
    assert "Departure" in delays["EWR"][0]["detail"]
    assert "30 minutes" in delays["EWR"][0]["detail"]
    assert "44 minutes" in delays["EWR"][0]["detail"]
    assert any(entry["type"] == "closure" for entry in delays["JFK"])
    assert any(entry["type"] == "ground_stop" for entry in delays["LGA"])


def test_parse_faa_nas_returns_empty_mapping_for_malformed_xml() -> None:
    assert parse_faa_nas("<AIRPORT_STATUS_INFORMATION><Ground_Delay>") == {}


def test_parse_faa_nas_reads_an_inline_ground_stop() -> None:
    xml_text = (
        "<AIRPORT_STATUS_INFORMATION><Ground_Stop><ARPT>JFK</ARPT>"
        "<Reason>Weather</Reason><Start>1:00 PM</Start>"
        "<End_Time>3:00 PM</End_Time></Ground_Stop></AIRPORT_STATUS_INFORMATION>"
    )

    assert parse_faa_nas(xml_text) == {
        "JFK": [
            {
                "type": "ground_stop",
                "reason": "Weather",
                "detail": "Start: 1:00 PM; End Time: 3:00 PM",
            }
        ]
    }


def test_tracking_links_use_icao_and_iata_flight_forms() -> None:
    assert flightaware_link("AA", 4912) == "https://www.flightaware.com/live/flight/AAL4912"
    assert fr24_link("AA", 4912) == "https://www.flightradar24.com/data/flights/aa4912"
