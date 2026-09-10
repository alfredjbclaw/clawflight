from clawflight.airports import (
    AIRPORTS_CSV,
    default_airports,
    great_circle_nm,
    load_airports,
    progress_fraction,
)
from clawflight.models import Position


def test_packaged_airport_table_loads_with_location_and_timezone() -> None:
    # Given: the airport table that ships inside the package.
    airports = default_airports()

    # Then: IATA keys retain their location and timezone data.
    assert airports["ASE"].name == "Aspen-Pitkin County"
    assert airports["DFW"].tz == "America/Chicago"
    assert airports["LHR"].tz == "Europe/London"
    assert len(airports) > 50


def test_default_airports_returns_an_isolated_copy() -> None:
    # Given: two independent calls into the cached table.
    first = default_airports()
    first.pop("ASE", None)

    # Then: mutating one caller's dict cannot corrupt the next caller's.
    assert "ASE" in default_airports()


def test_packaged_csv_path_is_importable_without_fixtures() -> None:
    # The engine must not depend on the tests directory to know about airports.
    assert AIRPORTS_CSV.exists()
    assert "fixtures" not in AIRPORTS_CSV.parts


def test_load_airports_skips_malformed_rows(tmp_path) -> None:
    # Given: a CSV containing one complete row and one malformed row.
    path = tmp_path / "airports.csv"
    path.write_text(
        "iata,name,lat,lon,tz\n"
        "JFK,John F Kennedy,40.6413,-73.7781,America/New_York\n"
        "BAD,broken\n"
    )

    # When: the loader receives the partial data.
    airports = load_airports(str(path))

    # Then: only the usable airport is returned.
    assert tuple(airports) == ("JFK",)


def test_progress_fraction_uses_remaining_great_circle_distance() -> None:
    # Given: a route with an aircraft sitting at its destination.
    airports = default_airports()
    position = Position(32.8998, -97.0403, 10_000.0, 300.0, 0.0, 1.0)

    # When: route progress and direct distance are calculated.
    progress = progress_fraction(position, airports["ASE"], airports["DFW"])

    # Then: destination progress is complete and the route has a real distance.
    assert progress == 1.0
    assert great_circle_nm(airports["ASE"], airports["DFW"]) > 500


def test_progress_fraction_is_zero_for_a_degenerate_route() -> None:
    # Given: an origin and destination that are the same airport.
    airports = default_airports()
    position = Position(39.2232, -106.8688, 0.0, 0.0, 0.0, 1.0)

    # Then: no division by a zero-length route occurs.
    assert progress_fraction(position, airports["ASE"], airports["ASE"]) == 0.0
