"""The optional push vendor boundary: normalization and subscription state."""
import json
import stat

import pytest

from clawflight.aerodatabox import MAX_VENDOR_STRING, SubscriptionClient, normalize_notification


def test_notification_normalization_covers_gate_delay_and_cancellation(fixtures) -> None:
    payload = json.loads((fixtures / "adb_notification.json").read_text())

    updates = normalize_notification(payload)

    assert [update.flight_number for update in updates] == ["AA4912", "AA1203", "DL4133"]
    assert updates[0].departure_gate == "B12"
    assert updates[0].arrival_terminal == "C"
    assert updates[1].departure_revised == "2026-07-11T18:07:00-05:00"
    assert updates[1].arrival_revised == "2026-07-11T23:00:00-04:00"
    # Routing data survives even where the notification is otherwise empty.
    assert updates[2].status == "Cancelled"
    assert updates[2].departure_scheduled is None


def test_notification_shapes_are_all_understood() -> None:
    single = {"flight": {"number": "AA4912"}}
    listed = {"flights": [{"number": "AA4912"}]}
    nested = {"data": {"flights": [{"number": "AA4912"}]}}
    alternate_key = {"flight": {"flightNumber": "aa 4912"}}

    for payload in (single, listed, nested, alternate_key):
        assert [u.flight_number for u in normalize_notification(payload)] == ["AA4912"]


def test_a_notification_without_a_usable_flight_number_yields_nothing() -> None:
    assert normalize_notification({"flight": {"status": "Scheduled"}}) == []
    assert normalize_notification({}) == []
    assert normalize_notification({"flights": ["not-a-mapping"]}) == []


def test_time_values_accept_string_and_local_utc_shapes() -> None:
    payload = {
        "flight": {
            "number": "AA4912",
            "departure": {"scheduledTime": "2026-07-11T12:00:00-05:00"},
            "arrival": {"scheduledTime": {"utc": "2026-07-11T21:10:00Z"}},
        }
    }

    update = normalize_notification(payload)[0]

    assert update.departure_scheduled == "2026-07-11T12:00:00-05:00"
    assert update.arrival_scheduled == "2026-07-11T21:10:00Z"


def test_baggage_belt_accepts_either_documented_vendor_key() -> None:
    belt = normalize_notification(
        {"flight": {"number": "AA4912", "arrival": {"baggageBelt": "  Claim 7  "}}}
    )[0]
    claim = normalize_notification(
        {"flight": {"number": "AA4912", "arrival": {"baggageClaim": "B3"}}}
    )[0]

    assert belt.arrival_baggage_belt == "Claim 7"
    assert claim.arrival_baggage_belt == "B3"


def test_an_absent_baggage_belt_is_never_fabricated_from_a_gate() -> None:
    update = normalize_notification(
        {"flight": {"number": "AA4912", "arrival": {"terminal": "C", "gate": "C14"}}}
    )[0]

    assert update.arrival_baggage_belt is None


def test_malformed_and_oversized_baggage_values_behave_like_an_absent_belt() -> None:
    for value in (17, ["Claim 7"], {"name": "Claim 7"}, "X" * (MAX_VENDOR_STRING + 1)):
        update = normalize_notification(
            {"flight": {"number": "AA4912", "arrival": {"baggageBelt": value}}}
        )[0]
        assert update.arrival_baggage_belt is None


def test_oversized_vendor_strings_are_clamped_before_reaching_a_message() -> None:
    # A hostile or broken webhook must not inject unbounded text into a chat.
    update = normalize_notification(
        {
            "flight": {
                "number": "AA4912",
                "status": "S" * 200,
                "departure": {"gate": "G" * 200},
            }
        }
    )[0]

    assert len(update.status) == MAX_VENDOR_STRING
    assert len(update.departure_gate) == MAX_VENDOR_STRING


def test_subscription_client_uses_documented_paths_and_tracks_state(tmp_path) -> None:
    # Given: fully injected transport callables and an empty state file.
    calls = []

    def post(url, payload):
        calls.append(("post", url, payload))
        return {"id": "sub-1"}

    def get(url):
        calls.append(("get", url, None))
        return {"credits": 87}

    def delete(url):
        calls.append(("delete", url, None))
        return {"deleted": True}

    state_path = tmp_path / "subscriptions.json"
    client = SubscriptionClient(
        "https://api.example.test/",
        str(state_path),
        "https://receiver.example.test/hook/secret",
        post,
        get,
        delete,
    )

    subscription_id = client.subscribe(" aa 4912 ")
    assert client.tracked_subscription_ids() == {"sub-1"}
    balance = client.balance()
    client.refill(20)
    client.unsubscribe(subscription_id)

    assert subscription_id == "sub-1"
    assert balance == 87
    assert calls[0] == (
        "post",
        "https://api.example.test/subscriptions/webhook/FlightByNumber/AA4912?useCredits=true",
        {"url": "https://receiver.example.test/hook/secret", "maxDeliveryRetries": 2},
    )
    assert calls[2] == (
        "post",
        "https://api.example.test/subscriptions/balance/refill",
        {"credits": 20},
    )
    assert calls[3] == ("delete", "https://api.example.test/subscriptions/webhook/sub-1", None)
    assert json.loads(state_path.read_text()) == {"subscriptions": {}}
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600


def test_a_subscription_response_without_an_id_is_an_error(tmp_path) -> None:
    client = SubscriptionClient(
        "https://api.example.test",
        str(tmp_path / "subs.json"),
        "https://receiver.example.test/hook/secret",
        lambda url, payload: {"unexpected": True},
        lambda url: {},
        lambda url: {},
    )

    with pytest.raises(ValueError):
        client.subscribe("AA4912")


def test_remote_subscription_ids_are_listed_for_reconciliation(tmp_path) -> None:
    # Given: a vendor GET returning the live subscription list.
    client = SubscriptionClient(
        "https://api.example.test",
        str(tmp_path / "subs.json"),
        "https://receiver.example.test/hook/secret",
        lambda url, payload: {"id": "sub-1"},
        lambda url: {"items": [{"id": "remote-1"}, {"subscriptionId": "remote-2"}, {"noise": True}]},
        lambda url: {},
    )

    # Then: every vendor id is surfaced so a sweep can unsubscribe leaks.
    assert client.remote_subscription_ids() == {"remote-1", "remote-2"}


def test_balance_reads_any_documented_credit_key_and_defaults_to_zero(tmp_path) -> None:
    def client_with(response):
        return SubscriptionClient(
            "https://api.example.test",
            str(tmp_path / "subs.json"),
            "https://receiver.example.test/hook/secret",
            lambda url, payload: {},
            lambda url: response,
            lambda url: {},
        )

    assert client_with({"creditsRemaining": 12}).balance() == 12
    assert client_with({"availableCredits": 7.9}).balance() == 7
    assert client_with({"unexpected": "shape"}).balance() == 0


def test_a_corrupt_subscription_state_file_starts_empty(tmp_path) -> None:
    path = tmp_path / "subs.json"

    for contents in ("{not json", '{"subscriptions": "nope"}', "[]"):
        path.write_text(contents)
        client = SubscriptionClient(
            "https://api.example.test",
            str(path),
            "https://receiver.example.test/hook/secret",
            lambda url, payload: {"id": "sub-1"},
            lambda url: {},
            lambda url: {},
        )
        assert client.tracked_subscription_ids() == set()
