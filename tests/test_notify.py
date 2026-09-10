"""Message composition: priority, context, links, trip cards, arrival posts."""
from clawflight.models import FlightEvent, PersonRef
from clawflight.notify import (
    FakePoster,
    MessageCap,
    classify,
    compose_arrival_post,
    compose_post,
    compose_trip_card,
)

from conftest import make_leg, make_record


def test_critical_alerts_carry_context_and_both_tracking_links() -> None:
    # Given: a delay event for a booked family flight.
    record = make_record()
    event = FlightEvent(record.flight_id, "delay", "AA4912 is delayed by 20 minutes.", True, 1.0)

    message = compose_post(event, record)

    assert classify(event) == "critical"
    assert message.startswith("🚨 AA4912 is delayed")
    assert "Service date: 2026-07-11" in message
    assert "Route: ASE -> DFW" in message
    assert "Traveler: Alex" in message
    assert "Confirmation: FAKE01" in message
    assert "https://www.flightaware.com/live/flight/AAL4912" in message
    assert "https://www.flightradar24.com/data/flights/aa4912" in message


def test_informational_events_are_classified_and_prefixed_differently() -> None:
    record = make_record()
    event = FlightEvent(record.flight_id, "halfway", "AA4912 is halfway.", True, 1.0)

    message = compose_post(event, record)

    assert classify(event) == "info"
    assert message.startswith("✈️ ")


def test_connection_alerts_are_critical() -> None:
    event = FlightEvent("AA4912-2026-07-11", "connection_alert", "Tight connection", True, 1.0)

    assert classify(event) == "critical"


def test_a_delay_post_carries_the_new_times_from_the_event_message() -> None:
    # Given: a delay event whose message already carries the new-times phrase.
    record = make_record()
    text = (
        "AA4912 departure is delayed by at least 45 minutes. "
        "New departure 10:29 (LHR local) / 05:29 ET"
    )
    event = FlightEvent(record.flight_id, "delay", text, True, 1.0)

    # Then: the recipient sees concrete new times, not just the slip.
    assert "New departure 10:29 (LHR local) / 05:29 ET" in compose_post(event, record)


def test_a_post_without_a_confirmation_code_omits_that_line() -> None:
    record = make_record(leg=make_leg(conf_code=None))
    event = FlightEvent(record.flight_id, "delay", "Delayed", True, 1.0)

    assert "Confirmation:" not in compose_post(event, record)


def test_tracking_start_uses_a_bounded_trip_card() -> None:
    record = make_record()
    event = FlightEvent(record.flight_id, "tracking_started", "Tracking started.", False, 1.0)

    text = compose_post(event, record)

    assert "Travel Day" in text
    assert "AA4912" in text
    assert "Seat 10C" in text


def test_landing_uses_the_arrival_welcome() -> None:
    record = make_record()
    event = FlightEvent(record.flight_id, "landing", "Landed.", True, 1.0)

    text = compose_post(event, record, "Baggage claim 4")

    assert "has arrived at DFW" in text
    assert "Service date: 2026-07-11" in text
    assert "Ground: Baggage claim 4" in text


def test_trip_card_covers_single_leg_multi_leg_weather_and_empty() -> None:
    first = make_record()
    second = make_record(
        flight_id="AA1203-2026-07-11",
        leg=make_leg(
            number=1203,
            origin="DFW",
            dest="JFK",
            sched_dep_iso="2026-07-11T17:39:00-05:00",
            sched_arr_iso="2026-07-11T22:30:00-04:00",
            seat="21D",
        ),
    )

    single = compose_trip_card([first])
    multi = compose_trip_card([first, second])

    assert "Travel Day — 2026-07-11 (Alex)" in single
    assert "AA4912 ASE -> DFW" in single
    assert "[FAKE01]" in single and "Seat 10C" in single
    assert "AA4912" in multi and "AA1203" in multi and "DFW -> JFK" in multi
    assert "Weather: Sunny, 85F" in compose_trip_card([first], weather="Sunny, 85F")
    assert compose_trip_card([]) == ""


def test_trip_card_tolerates_missing_times_and_airports() -> None:
    record = make_record(
        leg=make_leg(origin=None, dest=None, sched_dep_iso=None, sched_arr_iso=None, seat=None)
    )

    card = compose_trip_card([record])

    assert "? -> ?" in card
    assert "Seat" not in card


def test_arrival_post_never_includes_home_routing() -> None:
    # Drive-home ETA is deliberately out of scope for this package.
    post = compose_arrival_post(make_record())

    assert "drive" not in post.lower()
    assert "home" not in post.lower()
    assert "ETA" not in post


def test_arrival_post_includes_supplied_weather_and_ground_context() -> None:
    post = compose_arrival_post(
        make_record(), weather="Clear, 72F", ground_info="No ground delays"
    )

    assert "Clear, 72F" in post
    assert "No ground delays" in post


def test_long_person_names_do_not_break_composition() -> None:
    record = make_record(person=PersonRef("alex", "Alex " * 60))

    assert "Traveler:" in compose_post(
        FlightEvent(record.flight_id, "delay", "Delayed", True, 1.0), record
    )


def test_fake_poster_records_calls() -> None:
    poster = FakePoster()

    assert poster.post("Flight update") is True
    assert poster.calls == ["Flight update"]


def test_message_cap_limits_each_priority_per_flight() -> None:
    cap = MessageCap(max_info=2, max_critical=3)

    assert [cap.allow("f1", "info") for _ in range(3)] == [True, True, False]
    assert [cap.allow("f1", "critical") for _ in range(4)] == [True, True, True, False]
    # Caps are per flight, not global.
    assert cap.allow("f2", "info") is True


def test_message_cap_reset_clears_one_flight_or_all() -> None:
    cap = MessageCap(max_info=1, max_critical=1)
    cap.allow("f1", "info")
    cap.allow("f2", "info")

    cap.reset("f1")
    assert cap.allow("f1", "info") is True
    assert cap.allow("f2", "info") is False

    cap.reset()
    assert cap.allow("f2", "info") is True
