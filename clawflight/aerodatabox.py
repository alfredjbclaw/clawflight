"""Optional AeroDataBox push upgrade: notification normalization + subscriptions.

This module ships but stays dormant unless ``push.enabled`` is set. Every HTTP
call is an injected callable, so the package itself performs no network I/O and
tests never need a key.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Callable, Dict, List, Optional, Set
from urllib.parse import quote

from .models import FlightUpdate


JsonMapping = Dict[str, object]
HttpPost = Callable[[str, JsonMapping], JsonMapping]
HttpGet = Callable[[str], JsonMapping]
HttpDelete = Callable[[str], JsonMapping]

MAX_VENDOR_STRING = 64


def normalize_notification(payload: JsonMapping) -> List[FlightUpdate]:
    updates: List[FlightUpdate] = []
    for flight in _flight_objects(payload):
        number = _string(flight.get("number")) or _string(flight.get("flightNumber"))
        if number is None:
            continue
        departure = _mapping(flight.get("departure"))
        arrival = _mapping(flight.get("arrival"))
        updates.append(
            FlightUpdate(
                flight_number=number.upper().replace(" ", ""),
                status=_string(flight.get("status")),
                departure_scheduled=_time_value(departure, "scheduledTime"),
                departure_revised=_time_value(departure, "revisedTime"),
                arrival_scheduled=_time_value(arrival, "scheduledTime"),
                arrival_revised=_time_value(arrival, "revisedTime"),
                departure_terminal=_string(departure.get("terminal")),
                departure_gate=_string(departure.get("gate")),
                arrival_terminal=_string(arrival.get("terminal")),
                arrival_gate=_string(arrival.get("gate")),
                arrival_baggage_belt=(
                    _bounded_string(arrival.get("baggageBelt"))
                    or _bounded_string(arrival.get("baggageClaim"))
                ),
            )
        )
    return updates


class SubscriptionClient:
    def __init__(
        self,
        base_url: str,
        state_path: str,
        endpoint: str,
        http_post: HttpPost,
        http_get: HttpGet,
        http_delete: HttpDelete,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._state_path = state_path
        self._endpoint = endpoint
        self._http_post = http_post
        self._http_get = http_get
        self._http_delete = http_delete
        self._state = self._load_state()

    def subscribe(self, flight_number: str) -> str:
        number = flight_number.strip().upper().replace(" ", "")
        response = self._http_post(
            self._url(
                "/subscriptions/webhook/FlightByNumber/{}?useCredits=true".format(
                    quote(number, safe="")
                )
            ),
            {"url": self._endpoint, "maxDeliveryRetries": 2},
        )
        subscription_id = _string(response.get("id")) or _string(
            response.get("subscriptionId")
        )
        if subscription_id is None:
            raise ValueError("subscription response has no id")
        self._subscriptions()[subscription_id] = number
        self._write_state()
        return subscription_id

    def unsubscribe(self, subscription_id: str) -> bool:
        self._http_delete(
            self._url("/subscriptions/webhook/{}".format(quote(subscription_id, safe="")))
        )
        self._subscriptions().pop(subscription_id, None)
        self._write_state()
        return True

    def remote_subscription_ids(self) -> Set[str]:
        """Subscription ids the vendor currently holds for us.

        Used by the sweep to reconcile against locally tracked ids and
        unsubscribe leaked remote subscriptions we no longer track.
        """
        response = self._http_get(self._url("/subscriptions/webhook"))
        ids: Set[str] = set()
        for item in _subscription_items(response):
            identifier = _string(item.get("id")) or _string(item.get("subscriptionId"))
            if identifier is not None:
                ids.add(identifier)
        return ids

    def tracked_subscription_ids(self) -> Set[str]:
        return set(self._subscriptions())

    def balance(self) -> int:
        response = self._http_get(self._url("/subscriptions/balance"))
        for key in ("creditsRemaining", "credits", "balance", "availableCredits"):
            value = response.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, float):
                return int(value)
        return 0

    def refill(self, credits: int) -> None:
        self._http_post(self._url("/subscriptions/balance/refill"), {"credits": credits})

    def _url(self, path: str) -> str:
        return self._base_url + path

    def _subscriptions(self) -> Dict[str, str]:
        subscriptions = self._state.get("subscriptions")
        if not isinstance(subscriptions, dict):
            subscriptions = {}
            self._state["subscriptions"] = subscriptions
        return subscriptions

    def _load_state(self) -> Dict[str, Dict[str, str]]:
        try:
            with open(self._state_path, encoding="utf-8") as source:
                decoded = json.load(source)
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {"subscriptions": {}}
        if not isinstance(decoded, dict):
            return {"subscriptions": {}}
        subscriptions = decoded.get("subscriptions")
        if not isinstance(subscriptions, dict):
            return {"subscriptions": {}}
        clean = {
            key: value
            for key, value in subscriptions.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        return {"subscriptions": clean}

    def _write_state(self) -> None:
        directory = os.path.dirname(os.path.abspath(self._state_path))
        os.makedirs(directory, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".clawflight-subscriptions-", dir=directory
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            json.dump(self._state, destination, separators=(",", ":"), sort_keys=True)
        os.chmod(temporary, 0o600)
        os.replace(temporary, self._state_path)


def _flight_objects(payload: JsonMapping) -> List[JsonMapping]:
    for key in ("flights", "flight", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = value.get("flights")
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
            return [value]
    return []


def _subscription_items(response: JsonMapping) -> List[JsonMapping]:
    for key in ("items", "subscriptions", "data"):
        value = response.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _mapping(value: object) -> JsonMapping:
    return value if isinstance(value, dict) else {}


def _string(value: object) -> Optional[str]:
    # Clamp vendor-supplied strings so a hostile or broken webhook cannot inject
    # unbounded gate/terminal/status text downstream into a chat message.
    return (
        value.strip()[:MAX_VENDOR_STRING]
        if isinstance(value, str) and value.strip()
        else None
    )


def _bounded_string(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if normalized and len(normalized) <= MAX_VENDOR_STRING else None


def _time_value(section: JsonMapping, field: str) -> Optional[str]:
    value = section.get(field)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, dict):
        return _string(value.get("local")) or _string(value.get("utc"))
    return None
