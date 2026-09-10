"""Manual management: people, recipients, flights and settings from the CLI.

Before these existed a new install was unusable — the only way in was to
hand-edit JSON and stand up an IMAP mailbox before anything happened at all.
"""
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from clawflight import manage
from clawflight.config import load_config
from clawflight.people import PersonTable
from clawflight.registry import Registry


@pytest.fixture
def document() -> dict:
    return {}


# -- people -----------------------------------------------------------------


def test_adding_a_person_records_matchable_evidence(document) -> None:
    assert manage.add_person(document, "sam", "Sam", ["sam kestrel"], ["sam.kestrel"])

    person = PersonTable.from_entries(document["people"]).get("sam")
    assert person.display == "Sam"
    assert person.match_substrings == ("sam kestrel",)
    assert person.match_email_localparts == ("samkestrel",)


def test_a_person_with_no_match_terms_falls_back_to_their_name(document) -> None:
    # Otherwise the entry would match nothing and attribute nothing.
    manage.add_person(document, "sam", "Sam Kestrel")

    assert document["people"][0]["match_substrings"] == ["sam kestrel"]


def test_adding_the_same_person_twice_is_a_no_op(document) -> None:
    manage.add_person(document, "sam", "Sam", ["sam kestrel"])

    assert manage.add_person(document, "sam", "Sam", ["sam kestrel"]) is False


def test_re_adding_a_person_with_new_details_replaces_them(document) -> None:
    manage.add_person(document, "sam", "Sam", ["sam kestrel"])

    assert manage.add_person(document, "sam", "Sam", ["samuel kestrel"]) is True
    assert len(document["people"]) == 1
    assert document["people"][0]["match_substrings"] == ["samuel kestrel"]


def test_removing_a_person_also_clears_a_dangling_owner(document) -> None:
    # A stale owner makes doctor fail complaining about someone who is gone.
    manage.add_person(document, "sam", "Sam")
    document["owner"] = "sam"

    manage.remove_person(document, "sam")

    assert document["people"] == []
    assert "owner" not in document


def test_removing_an_unknown_person_says_so(document) -> None:
    with pytest.raises(manage.ManageError, match="no person"):
        manage.remove_person(document, "nobody")


def test_person_keys_are_validated(document) -> None:
    for bad in ("Has Space", "UPPER CASE!", "", "unknown"):
        with pytest.raises(manage.ManageError):
            manage.add_person(document, bad, "Someone")


# -- recipients -------------------------------------------------------------


def test_adding_a_recipient_records_a_deliverable_target(document) -> None:
    assert manage.add_recipient(
        document, "sam", "Sam", "telegram", "-100123", follow_all=True
    )

    entry = document["recipients"][0]
    assert entry["channel"] == {"channel": "telegram", "to": "-100123"}
    assert entry["follow_all"] is True
    assert entry["active"] is True


def test_a_recipient_needs_a_channel_and_a_target(document) -> None:
    for channel, target in (("", "-100"), ("telegram", "")):
        with pytest.raises(manage.ManageError):
            manage.add_recipient(document, "sam", "Sam", channel, target)


def test_a_thread_id_is_recorded_only_when_given(document) -> None:
    manage.add_recipient(document, "a", "A", "telegram", "-100", thread_id="42")
    manage.add_recipient(document, "b", "B", "telegram", "-100")

    assert document["recipients"][0]["channel"]["thread_id"] == "42"
    assert "thread_id" not in document["recipients"][1]["channel"]


def test_removing_a_recipient(document) -> None:
    manage.add_recipient(document, "sam", "Sam", "telegram", "-100")

    assert manage.remove_recipient(document, "sam") is True
    assert document["recipients"] == []
    with pytest.raises(manage.ManageError):
        manage.remove_recipient(document, "sam")


# -- settings ---------------------------------------------------------------


def test_settings_are_coerced_to_their_declared_type(document) -> None:
    manage.set_setting(document, "owner", "alex")
    manage.set_setting(document, "horizon_days", "5")
    manage.set_setting(document, "push.enabled", "true")
    manage.set_setting(document, "mailbox.trusted_senders", "delta.com, aa.com")

    assert document["owner"] == "alex"
    assert document["horizon_days"] == 5
    assert document["push"]["enabled"] is True
    assert document["mailbox"]["trusted_senders"] == ["delta.com", "aa.com"]


def test_an_unchanged_setting_reports_no_change(document) -> None:
    manage.set_setting(document, "owner", "alex")

    assert manage.set_setting(document, "owner", "alex") is False


def test_unknown_settings_are_refused_with_the_valid_list(document) -> None:
    # A free-form dotted path would silently create a key nothing ever reads.
    with pytest.raises(manage.ManageError, match="Settable"):
        manage.set_setting(document, "mailbox.hosst", "imap.example.com")


def test_bad_values_are_refused(document) -> None:
    with pytest.raises(manage.ManageError, match="true or false"):
        manage.set_setting(document, "push.enabled", "maybe")
    with pytest.raises(manage.ManageError, match="whole number"):
        manage.set_setting(document, "horizon_days", "lots")


def test_a_credential_cannot_be_stored_as_a_setting(document) -> None:
    # The whole secret model is that config holds the NAME of a variable.
    for path in ("mailbox.password", "push.api_key", "push.secret"):
        with pytest.raises(manage.ManageError):
            manage.set_setting(document, path, "hunter2")


def test_the_env_var_name_settings_are_still_allowed(document) -> None:
    assert manage.set_setting(document, "mailbox.password_env", "MY_IMAP_PASSWORD")
    assert document["mailbox"]["password_env"] == "MY_IMAP_PASSWORD"


# -- flights ----------------------------------------------------------------


def test_a_manual_flight_resolves_local_times_against_the_airport() -> None:
    flight = manage.build_flight(
        "DL767", "2026-09-12", origin="JFK", dest="LAX",
        depart="16:55", arrive="20:20", conf_code="abc123", seat="22e",
    )

    assert (flight.leg.carrier, flight.leg.number) == ("DL", 767)
    # JFK is UTC-4 in September, LAX is UTC-7.
    assert flight.leg.sched_dep_iso == "2026-09-12T16:55:00-04:00"
    assert flight.leg.sched_arr_iso == "2026-09-12T20:20:00-07:00"
    assert flight.leg.conf_code == "ABC123"
    assert flight.leg.seat == "22E"


def test_flight_designators_are_accepted_in_the_forms_people_type() -> None:
    for text in ("DL767", "dl767", " DL 767 ", "DL-767"):
        assert manage.parse_flight_designator(text) == ("DL", 767)
    with pytest.raises(manage.ManageError):
        manage.parse_flight_designator("Delta 767")


def test_naming_a_person_outranks_every_inferred_signal() -> None:
    # Matching by display name would fail: match_substrings holds how an
    # airline spells someone, which is rarely a substring of their name.
    people = PersonTable.from_entries(
        [{"key": "sam", "display": "Sam", "match_substrings": ["samuel t kestrel"]}]
    )

    flight = manage.build_flight("DL767", "2026-09-12", person="sam", people=people)

    assert flight.hints["person_key"] == "sam"
    assert people.person_from_hints(flight.hints).key == "sam"
    assert people.rank(flight.hints) == 4


def test_an_unknown_person_is_refused_and_lists_the_known_ones() -> None:
    people = PersonTable.from_entries([{"key": "sam", "display": "Sam"}])

    with pytest.raises(manage.ManageError, match="sam"):
        manage.build_flight("DL767", "2026-09-12", person="nobody", people=people)


def test_bad_flight_arguments_are_refused_clearly() -> None:
    with pytest.raises(manage.ManageError, match="YYYY-MM-DD"):
        manage.build_flight("DL767", "12/09/2026")
    with pytest.raises(manage.ManageError, match="not a real date"):
        manage.build_flight("DL767", "2026-02-30")
    with pytest.raises(manage.ManageError, match="IATA"):
        manage.build_flight("DL767", "2026-09-12", origin="Kennedy")
    with pytest.raises(manage.ManageError, match="HH:MM"):
        manage.build_flight("DL767", "2026-09-12", origin="JFK", depart="4:55pm")
    with pytest.raises(manage.ManageError, match="same airport"):
        manage.build_flight("DL767", "2026-09-12", origin="JFK", dest="jfk")


def test_a_time_without_its_airport_is_refused() -> None:
    # Local clock time is meaningless without a zone to resolve it in.
    with pytest.raises(manage.ManageError, match="airport"):
        manage.build_flight("DL767", "2026-09-12", depart="16:55")


def test_an_unknown_airport_cannot_silently_drop_the_time() -> None:
    with pytest.raises(manage.ManageError, match="timezone"):
        manage.build_flight("DL767", "2026-09-12", origin="ZZZ", depart="16:55")


# -- documents on disk ------------------------------------------------------


def test_config_documents_are_written_privately_and_atomically(tmp_path) -> None:
    path = tmp_path / "clawflight.json"
    document = {}
    manage.add_person(document, "sam", "Sam")

    manage.write_document(path, document)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["people"][0]["key"] == "sam"


def test_reading_a_document_reports_that_comments_will_be_lost(tmp_path) -> None:
    path = tmp_path / "clawflight.json"
    path.write_text('{\n  // who owns this\n  "owner": "alex"\n}\n', encoding="utf-8")

    document, had_comments = manage.read_document(path)

    assert document["owner"] == "alex"
    assert had_comments is True


def test_a_missing_document_starts_empty(tmp_path) -> None:
    document, had_comments = manage.read_document(tmp_path / "nope.json")

    assert document == {} and had_comments is False


def test_a_broken_document_is_reported_not_silently_replaced(tmp_path) -> None:
    path = tmp_path / "clawflight.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(manage.ManageError):
        manage.read_document(path)


def test_a_manual_flight_round_trips_through_the_registry(tmp_path) -> None:
    people = PersonTable.from_entries([{"key": "sam", "display": "Sam"}])
    registry = Registry(str(tmp_path / "registry.json"), people)
    flight = manage.build_flight(
        "DL767", "2026-09-12", origin="JFK", dest="LAX",
        depart="16:55", person="sam", people=people,
    )

    report = registry.merge([flight])
    record = registry.get("DL767-2026-09-12")

    assert report["created"] == ["DL767-2026-09-12"]
    assert record.person.key == "sam"
    assert record.sources == ("manual:2026-09-12",)
    # Attribution survives a reload, so a later sweep cannot downgrade it.
    assert Registry(str(tmp_path / "registry.json"), people).get(
        "DL767-2026-09-12"
    ).person.key == "sam"


def test_forgetting_a_flight_clears_registry_and_monitor_state(tmp_path) -> None:
    from clawflight.monitor import Monitor

    people = PersonTable.from_entries([{"key": "sam", "display": "Sam"}])
    registry = Registry(str(tmp_path / "registry.json"), people)
    registry.merge([manage.build_flight("DL767", "2026-09-12", origin="JFK", depart="16:55")])
    monitor = Monitor(str(tmp_path / "monitor.json"))
    monitor._state["DL767-2026-09-12"] = {"phase": "watch", "sent": ["tracking_started"]}
    monitor._write_state()

    assert registry.forget("DL767-2026-09-12") is True
    assert monitor.forget("DL767-2026-09-12") is True

    # Leaving "already sent" markers behind would silently suppress alerts if
    # the same flight were added again later.
    assert registry.get("DL767-2026-09-12") is None
    assert Monitor(str(tmp_path / "monitor.json")).state_snapshot() == {}
    assert registry.forget("DL767-2026-09-12") is False
