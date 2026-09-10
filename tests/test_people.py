"""Config-driven attribution: the rewritten replacement for a hardcoded family."""
from clawflight.models import PersonRef
from clawflight.people import UNKNOWN_PERSON, Person, PersonTable, to_entries


def test_empty_table_attributes_nothing() -> None:
    # Given: the default table a fresh install starts with.
    table = PersonTable()

    # Then: no evidence of any kind produces an attribution.
    assert not table
    assert table.from_text("ALEX KESTREL") is None
    assert table.from_title("Alex's flight home") is None
    assert table.from_attendee("alex.kestrel@example.com") is None
    assert table.person_from_hints({"passenger_name": "ALEX KESTREL"}) == UNKNOWN_PERSON
    assert table.rank({"passenger_name": "ALEX KESTREL"}) == 0


def test_name_variants_all_resolve_to_one_person(people) -> None:
    # Given: the spelling variants the fixtures actually contain.
    assert people.from_text("ALEX KESTREL") == PersonRef("alex", "Alex")
    assert people.from_text("ALEXANDRA MORGAN KESTREL") == PersonRef("alex", "Alex")
    assert people.from_text("SAMUEL T KESTREL") == PersonRef("sam", "Sam")
    assert people.from_text("SAM KESTREL") == PersonRef("sam", "Sam")
    assert people.from_text("TAYLOR RIVERS") is None


def test_possessive_titles_match_key_first_name_display_and_alias(people) -> None:
    assert people.from_title("Robin's flight home") == PersonRef("robin", "Robin")
    assert people.from_title("Alex's flight to Denver") == PersonRef("alex", "Alex")
    assert people.from_title("Parent's flight home") == PersonRef("robin", "Robin")
    assert people.from_title("A flight to Denver") is None


def test_possessive_titles_accept_a_typographic_apostrophe(people) -> None:
    assert people.from_title("Alex’s flight to Denver") == PersonRef("alex", "Alex")


def test_attendee_local_part_requires_an_exact_known_handle(people) -> None:
    # Given: a real handle and a prose-like lookalike.
    assert people.from_attendee("sam.kestrel@example.com") == PersonRef("sam", "Sam")
    assert people.from_attendee("Sam <s.kestrel@example.com>") == PersonRef("sam", "Sam")
    # A prefix match would let "samantha@" attribute to "sam".
    assert people.from_attendee("samantha@example.com") is None
    assert people.from_attendee("ops@example.com") is None
    assert people.from_attendee("not-an-address") is None


def test_evidence_ranks_are_ordered_passenger_then_title_then_attendee(people) -> None:
    assert people.rank({"passenger_name": "ALEX KESTREL"}) == 3
    assert people.rank({"title": "Alex's flight"}) == 2
    assert people.rank({"attendees": ["alex.kestrel@example.com"]}) == 1
    assert people.rank({"title": "Some meeting"}) == 0


def test_person_from_hints_prefers_the_strongest_evidence(people) -> None:
    # Given: hints where every evidence class names a different person.
    hints = {
        "passenger_name": "SAM KESTREL",
        "title": "Alex's flight",
        "attendees": ["robin.kestrel@example.com"],
    }

    assert people.person_from_hints(hints) == PersonRef("sam", "Sam")
    assert people.person_from_hints({k: hints[k] for k in ("title", "attendees")}) == (
        PersonRef("alex", "Alex")
    )
    assert people.person_from_hints({"attendees": hints["attendees"]}) == (
        PersonRef("robin", "Robin")
    )
    assert people.person_from_hints({}) == UNKNOWN_PERSON


def test_from_entries_skips_invalid_rows_instead_of_raising() -> None:
    # Given: a config file with a typo in every wrong way one can be made.
    table = PersonTable.from_entries(
        [
            {"key": "alex", "display": "Alex", "match_substrings": ["alex kestrel"]},
            {"key": "alex", "display": "Duplicate"},
            {"key": "has a space", "display": "Bad key"},
            {"key": "unknown", "display": "Reserved"},
            {"key": "nodisplay"},
            {"display": "nokey"},
            "not-a-mapping",
            None,
        ]
    )

    # Then: exactly the one valid row survives and it wins over the duplicate.
    assert [person.key for person in table] == ["alex"]
    assert table.get("alex").display == "Alex"
    assert table.get("missing") is None
    assert len(table) == 1


def test_from_entries_accepts_name_as_an_alias_for_display() -> None:
    table = PersonTable.from_entries([{"key": "sam", "name": "Sam"}])

    assert table.get("sam") == Person(key="sam", display="Sam")


def test_from_entries_normalizes_key_case_and_surrounding_space() -> None:
    # Given: a hand-edited config that capitalised or padded the key.
    table = PersonTable.from_entries([{"key": "  Alex  ", "display": "Alex"}])

    # Then: the key is canonicalised rather than the row being dropped.
    assert [person.key for person in table] == ["alex"]


def test_local_parts_are_normalized_on_both_sides() -> None:
    # Given: a config that writes the handle with separators.
    table = PersonTable.from_entries(
        [{"key": "alex", "display": "Alex", "match_email_localparts": ["a.kestrel"]}]
    )

    # Then: separator style in the config never has to match the address.
    assert table.from_attendee("a-kestrel@example.com") == PersonRef("alex", "Alex")
    assert table.from_attendee("a_kestrel@example.com") == PersonRef("alex", "Alex")
    assert table.from_attendee("akestrel@example.com") == PersonRef("alex", "Alex")


def test_entries_round_trip_through_config_shape(people) -> None:
    entries = to_entries(people)
    rebuilt = PersonTable.from_entries(entries)

    assert [person.key for person in rebuilt] == [person.key for person in people]
    assert rebuilt.from_text("ALEXANDRA MORGAN KESTREL") == PersonRef("alex", "Alex")


def test_lookups_tolerate_non_string_evidence(people) -> None:
    assert people.from_text(None) is None
    assert people.from_title(17) is None
    assert people.from_attendee(["alex.kestrel@example.com"]) is None
    assert people.rank({"attendees": "alex.kestrel@example.com"}) == 0
