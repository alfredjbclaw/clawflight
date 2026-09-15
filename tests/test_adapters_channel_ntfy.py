"""ntfy delivery requests are inspectable without a network connection."""
from __future__ import annotations

import logging
from email.header import decode_header
from typing import Optional
from urllib.error import URLError

import pytest

from clawflight.adapters.channel_ntfy import NtfyPoster
from clawflight.models import FlightEvent
from clawflight.notify import compose_post

from conftest import make_record


class _RecordingOpener:
    def __init__(self, status: int = 200, error: Optional[Exception] = None) -> None:
        self.status = status
        self.error = error
        self.calls = []

    def __call__(self, request, timeout: float) -> int:
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.status


def test_request_shape_uses_utf8_body_and_ntfy_priority() -> None:
    opener = _RecordingOpener()
    poster = NtfyPoster("https://ntfy.sh/demo-topic", opener=opener)

    assert poster.post("🚨 DL767 is delayed.", "critical")
    assert poster.post("DL767 is delayed.", "info")
    critical = opener.calls[0][0]
    info = opener.calls[1][0]

    assert critical.full_url == "https://ntfy.sh/demo-topic"
    assert critical.get_method() == "POST"
    assert critical.get_header("Priority") == "4"
    assert critical.data == "🚨 DL767 is delayed.".encode("utf-8")
    assert critical.get_header("Content-type") == "text/plain; charset=utf-8"
    assert critical.get_header("Title") == "DL767"
    assert info.get_header("Priority") == "3"


def test_bare_topic_uses_self_hosted_base_url() -> None:
    opener = _RecordingOpener()
    poster = NtfyPoster("family", base_url="https://ntfy.example.com", opener=opener)

    assert poster.post("Update")

    assert opener.calls[0][0].full_url == "https://ntfy.example.com/family"


def test_token_is_read_at_send_time_without_being_exposed(monkeypatch, caplog) -> None:
    bearer_value = "invented-bearer-value"
    opener = _RecordingOpener()
    monkeypatch.setenv("CLAWFLIGHT_NTFY_TOKEN", bearer_value)
    poster = NtfyPoster(
        "https://ntfy.example.com/demo-topic",
        token_env="CLAWFLIGHT_NTFY_TOKEN",
        opener=opener,
    )

    with caplog.at_level(logging.DEBUG):
        assert poster.post("Update")

    assert opener.calls[0][0].get_header("Authorization") == "Bearer " + bearer_value
    assert bearer_value not in vars(poster).values()
    assert bearer_value not in repr(poster)
    assert bearer_value not in caplog.text
    monkeypatch.delenv("CLAWFLIGHT_NTFY_TOKEN")
    assert poster.post("Update")
    assert opener.calls[1][0].get_header("Authorization") is None


def test_post_acknowledges_only_2xx_and_handles_transport_failure() -> None:
    successful = _RecordingOpener(200)
    assert NtfyPoster(
        "https://ntfy.example.com/demo-topic", opener=successful, timeout=7.0
    ).post("Update")
    assert successful.calls[0][1] == 7.0
    assert not NtfyPoster(
        "https://ntfy.example.com/demo-topic", opener=_RecordingOpener(401)
    ).post("Update")
    assert not NtfyPoster(
        "https://ntfy.example.com/demo-topic", opener=_RecordingOpener(500)
    ).post("Update")
    assert not NtfyPoster(
        "https://ntfy.example.com/demo-topic", opener=_RecordingOpener(error=URLError("offline"))
    ).post("Update")


@pytest.mark.parametrize("value", ["file:///tmp/topic", "ftp://ntfy.example.com/topic"])
def test_unsafe_topic_or_base_url_is_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        NtfyPoster(value)
    with pytest.raises(ValueError):
        NtfyPoster("family", base_url=value)


def test_non_ascii_configured_title_is_rfc2047_encoded() -> None:
    opener = _RecordingOpener()
    poster = NtfyPoster(
        "https://ntfy.example.com/demo-topic", title="✈️ DL767 update", opener=opener
    )

    assert poster.post("🚨 DL767 is delayed.")
    title = opener.calls[0][0].get_header("Title")

    assert title is not None
    assert _decoded_title(title) == "✈️ DL767 update"


def _decoded_title(title: str) -> str:
    return "".join(
        part.decode(charset or "ascii") if isinstance(part, bytes) else part
        for part, charset in decode_header(title)
    )


def test_long_non_ascii_title_is_rfc2047_encoded_without_newlines() -> None:
    opener = _RecordingOpener()
    title_text = "✈️ " + "DL767 update " * 30
    poster = NtfyPoster(
        "https://ntfy.example.com/demo-topic", title=title_text, opener=opener
    )

    assert poster.post("🚨 DL767 is delayed.")
    title = opener.calls[0][0].get_header("Title")

    assert title is not None
    assert "\r" not in title and "\n" not in title
    assert _decoded_title(title) == title_text


def test_ascii_optional_headers_are_sent_unencoded() -> None:
    opener = _RecordingOpener()
    poster = NtfyPoster(
        "https://ntfy.example.com/demo-topic",
        title="Flight update",
        tags="airplane,warning",
        opener=opener,
    )

    assert poster.post("Update")
    configured = opener.calls[0][0]

    assert configured.get_header("Title") == "Flight update"
    assert configured.get_header("Tags") == "airplane,warning"


def test_tracking_start_title_uses_flight_on_trip_card_line() -> None:
    opener = _RecordingOpener()
    record = make_record()
    text = compose_post(
        FlightEvent(record.flight_id, "tracking_started", "Tracking started.", False, 1.0),
        record,
    )
    poster = NtfyPoster("https://ntfy.example.com/demo-topic", opener=opener)

    assert poster.post(text)

    assert opener.calls[0][0].get_header("Title") == "AA4912"


def test_message_without_a_flight_identifier_omits_title() -> None:
    opener = _RecordingOpener()
    poster = NtfyPoster("https://ntfy.example.com/demo-topic", opener=opener)

    assert poster.post("🚨 Service disruption reported.", "critical")

    assert opener.calls[0][0].get_header("Title") is None
