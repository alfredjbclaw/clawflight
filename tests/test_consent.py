"""Itinerary consent: prompt scheduling, decisions, expiry, and leg aggregation."""
import json

import pytest

from clawflight.consent import (
    OPT_IN,
    OPT_OUT,
    STAGE_24H,
    STAGE_48H,
    ConsentLedger,
    itinerary_key,
)


HOUR = 3600
DEPARTURE = 2_000_000_000.0
ARRIVAL = DEPARTURE + 8 * HOUR
OWNER = "alex"


def _ledger(tmp_path, owner: str = OWNER) -> ConsentLedger:
    return ConsentLedger(str(tmp_path / "consent.json"), owner_key=owner)


def _due(ledger, key, traveler, hours_before, arrival=ARRIVAL):
    return ledger.prompts_due(
        itinerary_key=key,
        traveler_key=traveler,
        first_departure_epoch=DEPARTURE,
        final_arrival_epoch=arrival,
        now_epoch=DEPARTURE - hours_before * HOUR,
    )


def test_the_owner_gets_t48_then_one_unanswered_t24_reminder(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    key = itinerary_key("fake20", "unused")

    assert _due(ledger, key, OWNER, 49) == ()
    first = _due(ledger, key, "Alex", 48)
    assert [prompt.stage for prompt in first] == [STAGE_48H]
    assert first[0].due_at_epoch == DEPARTURE - 48 * HOUR
    assert _due(ledger, key, OWNER, 47) == ()
    assert [prompt.stage for prompt in _due(ledger, key, OWNER, 24)] == [STAGE_24H]
    assert _due(ledger, key, OWNER, 23) == ()

    reloaded = _ledger(tmp_path).get(key)
    assert reloaded is not None
    assert dict(reloaded.prompt_timestamps) == {
        STAGE_48H: DEPARTURE - 48 * HOUR,
        STAGE_24H: DEPARTURE - 24 * HOUR,
    }


def test_a_non_owner_gets_only_t24_and_a_late_run_is_not_doubled(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    other = itinerary_key(None, "trip-7")
    owner = itinerary_key(None, "trip-8")

    assert _due(ledger, other, "sam", 25) == ()
    assert [item.stage for item in _due(ledger, other, "sam", 24)] == [STAGE_24H]
    assert _due(ledger, other, "sam", 20) == ()
    # A first run inside the T-24 window emits one prompt, never both stages.
    assert [item.stage for item in _due(ledger, owner, OWNER, 20)] == [STAGE_24H]
    assert _due(ledger, owner, OWNER, 19) == ()


def test_an_unconfigured_owner_means_nobody_gets_the_early_prompt(tmp_path) -> None:
    ledger = _ledger(tmp_path, owner="")
    key = itinerary_key("fake20")

    assert _due(ledger, key, OWNER, 48) == ()
    assert [item.stage for item in _due(ledger, key, OWNER, 24)] == [STAGE_24H]


def test_opt_in_enables_deep_events_and_suppresses_the_reminder(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    key = itinerary_key("fake21")
    _due(ledger, key, OWNER, 48)

    state = ledger.record_decision(
        itinerary_key=key,
        decision=OPT_IN,
        now_epoch=DEPARTURE - 40 * HOUR,
        final_arrival_epoch=ARRIVAL,
    )

    assert state.decision == OPT_IN
    assert _due(ledger, key, OWNER, 24) == ()
    access = ledger.delivery_access(key, now_epoch=DEPARTURE)
    assert access.baseline_alerts is True
    assert access.deep_travel_day_events is True
    assert access.decision == OPT_IN


def test_opt_out_keeps_baseline_alerts_and_disables_deep_events(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    key = itinerary_key(None, "safe-fallback")
    ledger.record_decision(
        itinerary_key=key,
        decision=OPT_OUT,
        now_epoch=DEPARTURE - 30 * HOUR,
        final_arrival_epoch=ARRIVAL,
    )

    access = _ledger(tmp_path).delivery_access(key, now_epoch=DEPARTURE)

    assert access.baseline_alerts is True
    assert access.deep_travel_day_events is False
    assert access.decision == OPT_OUT


def test_an_unknown_itinerary_still_permits_baseline_alerts(tmp_path) -> None:
    access = _ledger(tmp_path).delivery_access("fallback:unknown", now_epoch=DEPARTURE)

    assert access.baseline_alerts is True
    assert access.deep_travel_day_events is False
    assert access.expired is False


def test_a_decision_expires_at_the_earlier_of_arrival_and_explicit_expiry(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    explicit_key = itinerary_key("fake30")
    arrival_key = itinerary_key("fake31")
    explicit = DEPARTURE + HOUR
    ledger.record_decision(
        itinerary_key=explicit_key,
        decision=OPT_IN,
        now_epoch=DEPARTURE,
        final_arrival_epoch=ARRIVAL,
        expires_at_epoch=explicit,
    )
    ledger.record_decision(
        itinerary_key=arrival_key,
        decision=OPT_IN,
        now_epoch=DEPARTURE,
        final_arrival_epoch=ARRIVAL,
        expires_at_epoch=ARRIVAL + HOUR,
    )

    assert ledger.delivery_access(explicit_key, now_epoch=explicit - 1).deep_travel_day_events
    explicit_access = ledger.delivery_access(explicit_key, now_epoch=explicit)
    arrival_access = ledger.delivery_access(arrival_key, now_epoch=ARRIVAL)
    assert not explicit_access.deep_travel_day_events and explicit_access.expired
    assert not arrival_access.deep_travel_day_events and arrival_access.expired
    assert ledger.get(explicit_key).expires_at_epoch == explicit
    assert ledger.get(arrival_key).expires_at_epoch == ARRIVAL


def test_missing_and_corrupt_ledgers_recover_and_write_valid_state(tmp_path) -> None:
    path = tmp_path / "nested" / "consent.json"
    missing = ConsentLedger(str(path), owner_key=OWNER)
    assert missing.delivery_access("fallback:unknown", now_epoch=0).baseline_alerts

    path.parent.mkdir(exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    recovered = ConsentLedger(str(path), owner_key=OWNER)
    key = itinerary_key("fake40")

    assert [item.stage for item in _due(recovered, key, "sam", 24)] == [STAGE_24H]
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1
    assert ConsentLedger(str(path), owner_key=OWNER).get(key) is not None


def test_a_shared_confirmation_has_one_decision_across_its_legs(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    first_leg_key = itinerary_key(" fake50 ", "first-leg")
    second_leg_key = itinerary_key("FAKE50", "second-leg")
    assert first_leg_key == second_leg_key

    _due(ledger, first_leg_key, OWNER, 48, arrival=DEPARTURE + 2 * HOUR)
    second_arrival = ARRIVAL + 24 * HOUR
    ledger.prompts_due(
        itinerary_key=second_leg_key,
        traveler_key=OWNER,
        first_departure_epoch=DEPARTURE + 10 * HOUR,
        final_arrival_epoch=second_arrival,
        now_epoch=DEPARTURE - 47 * HOUR,
    )
    ledger.record_decision(
        itinerary_key=first_leg_key,
        decision=OPT_IN,
        now_epoch=DEPARTURE - 46 * HOUR,
        final_arrival_epoch=second_arrival,
    )

    assert ledger.delivery_access(second_leg_key, now_epoch=DEPARTURE).deep_travel_day_events
    state = _ledger(tmp_path).get(second_leg_key)
    assert state.first_departure_epoch == DEPARTURE
    assert state.final_arrival_epoch == second_arrival
    assert state.expires_at_epoch == second_arrival


def test_prompt_timing_uses_the_itinerary_first_departure(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    key = itinerary_key("FAKE60")
    now = DEPARTURE - 49 * HOUR

    assert (
        ledger.prompts_due(
            itinerary_key=key,
            traveler_key=OWNER,
            first_departure_epoch=DEPARTURE,
            final_arrival_epoch=DEPARTURE + 2 * HOUR,
            now_epoch=now,
        )
        == ()
    )
    # The second leg is inside its own T-48 window, but the itinerary's first
    # departure is still 49 hours away, so no itinerary-level prompt is due.
    assert (
        ledger.prompts_due(
            itinerary_key=key,
            traveler_key=OWNER,
            first_departure_epoch=DEPARTURE + 24 * HOUR,
            final_arrival_epoch=ARRIVAL + 24 * HOUR,
            now_epoch=now,
        )
        == ()
    )
    assert [
        item.stage
        for item in ledger.prompts_due(
            itinerary_key=key,
            traveler_key=OWNER,
            first_departure_epoch=DEPARTURE + 24 * HOUR,
            final_arrival_epoch=ARRIVAL + 24 * HOUR,
            now_epoch=DEPARTURE - 48 * HOUR,
        )
    ] == [STAGE_48H]


def test_a_replacement_decision_clears_or_extends_a_prior_expiry(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    key = itinerary_key("FAKE70")
    ledger.record_decision(
        itinerary_key=key,
        decision=OPT_OUT,
        now_epoch=DEPARTURE - 2 * HOUR,
        final_arrival_epoch=ARRIVAL,
        expires_at_epoch=DEPARTURE - HOUR,
    )
    assert ledger.delivery_access(key, now_epoch=DEPARTURE).expired

    renewed = ledger.record_decision(
        itinerary_key=key, decision=OPT_IN, now_epoch=DEPARTURE, final_arrival_epoch=ARRIVAL
    )
    assert renewed.expires_at_epoch == ARRIVAL
    assert ledger.delivery_access(key, now_epoch=DEPARTURE).deep_travel_day_events

    extended = ARRIVAL - HOUR
    ledger.record_decision(
        itinerary_key=key,
        decision=OPT_IN,
        now_epoch=DEPARTURE,
        final_arrival_epoch=ARRIVAL,
        expires_at_epoch=DEPARTURE + HOUR,
    )
    renewed = ledger.record_decision(
        itinerary_key=key,
        decision=OPT_IN,
        now_epoch=DEPARTURE,
        final_arrival_epoch=ARRIVAL,
        expires_at_epoch=extended,
    )
    assert renewed.expires_at_epoch == extended


def test_separately_loaded_ledgers_cannot_claim_the_same_stage(tmp_path) -> None:
    # Given: two ledgers on one file, as a scheduler run and a retry would be.
    first = _ledger(tmp_path)
    stale = _ledger(tmp_path)
    key = itinerary_key("FAKE80")

    assert [item.stage for item in _due(first, key, OWNER, 48)] == [STAGE_48H]
    assert _due(stale, key, OWNER, 48) == ()
    assert dict(stale.get(key).prompt_timestamps) == {STAGE_48H: DEPARTURE - 48 * HOUR}


def test_an_earlier_schedule_correction_shortens_expiry_without_losing_legs(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    key = itinerary_key("FAKE90")
    now = DEPARTURE - 60 * HOUR
    ledger.prompts_due(
        itinerary_key=key,
        traveler_key=OWNER,
        first_departure_epoch=DEPARTURE,
        final_arrival_epoch=DEPARTURE + 2 * HOUR,
        now_epoch=now,
        leg_key="first-leg",
    )
    ledger.prompts_due(
        itinerary_key=key,
        traveler_key=OWNER,
        first_departure_epoch=DEPARTURE + 4 * HOUR,
        final_arrival_epoch=DEPARTURE + 10 * HOUR,
        now_epoch=now,
        leg_key="final-leg",
    )
    ledger.record_decision(
        itinerary_key=key,
        decision=OPT_IN,
        now_epoch=now,
        final_arrival_epoch=DEPARTURE + 10 * HOUR,
    )

    corrected_by_decision = DEPARTURE + 8 * HOUR
    state = ledger.record_decision(
        itinerary_key=key,
        decision=OPT_IN,
        now_epoch=now,
        final_arrival_epoch=corrected_by_decision,
    )
    assert state.expires_at_epoch == corrected_by_decision

    corrected_final = DEPARTURE + 6 * HOUR
    ledger.prompts_due(
        itinerary_key=key,
        traveler_key=OWNER,
        first_departure_epoch=DEPARTURE + 3 * HOUR,
        final_arrival_epoch=corrected_final,
        now_epoch=now,
        leg_key="final-leg",
    )
    state = _ledger(tmp_path).get(key)
    assert state.first_departure_epoch == DEPARTURE
    assert state.final_arrival_epoch == corrected_final
    assert state.expires_at_epoch == corrected_final
    assert (
        ledger.delivery_access(key, now_epoch=corrected_final).deep_travel_day_events is False
    )


def test_itinerary_keys_are_namespaced_and_bounded() -> None:
    assert itinerary_key("fake20") == "confirmation:FAKE20"
    assert itinerary_key(None, "trip-7") == "fallback:trip-7"
    # A fallback can never collide with a confirmation code.
    assert itinerary_key(None, "FAKE20") != itinerary_key("FAKE20")
    with pytest.raises(ValueError):
        itinerary_key(None)
    with pytest.raises(ValueError):
        itinerary_key("has space")
    with pytest.raises(ValueError):
        itinerary_key("", None)


def test_invalid_arguments_are_rejected(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    key = itinerary_key("FAKE95")

    with pytest.raises(ValueError):
        ledger.prompts_due(
            itinerary_key=key,
            traveler_key=OWNER,
            first_departure_epoch=DEPARTURE,
            final_arrival_epoch=DEPARTURE - HOUR,
            now_epoch=DEPARTURE - 48 * HOUR,
        )
    with pytest.raises(ValueError):
        ledger.record_decision(
            itinerary_key=key,
            decision="maybe",
            now_epoch=DEPARTURE,
            final_arrival_epoch=ARRIVAL,
        )
    with pytest.raises(ValueError):
        ledger.prompts_due(
            itinerary_key=key,
            traveler_key=OWNER,
            first_departure_epoch=float("inf"),
            final_arrival_epoch=ARRIVAL,
            now_epoch=DEPARTURE,
        )


def test_one_itinerary_cannot_have_two_travelers(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    key = itinerary_key("FAKE96")
    _due(ledger, key, OWNER, 48)

    with pytest.raises(ValueError):
        _due(ledger, key, "sam", 47)
