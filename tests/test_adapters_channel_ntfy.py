"""ntfy delivery requests are inspectable without a network connection."""
from __future__ import annotations

import logging
from typing import Optional
from urllib.error import URLError

import pytest

from clawflight.adapters.channel_ntfy import NtfyPoster


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
    poster = NtfyPoster("https://ntfy.sh/demo-topic", opener=_RecordingOpener())

    critical = poster.request_for("🚨 DL767 is delayed.", "critical")
    info = poster.request_for("DL767 is delayed.", "info")

    assert critical.full_url == "https://ntfy.sh/demo-topic"
    assert critical.get_method() == "POST"
    assert critical.get_header("Priority") == "4"
    assert critical.data == "🚨 DL767 is delayed.".encode("utf-8")
    assert critical.get_header("Content-type") == "text/plain; charset=utf-8"
    assert info.get_header("Priority") == "3"


def test_bare_topic_uses_self_hosted_base_url() -> None:
    request = NtfyPoster(
        "family", base_url="https://ntfy.example.com", opener=_RecordingOpener()
    ).request_for("Update")

    assert request.full_url == "https://ntfy.example.com/family"


def test_token_is_read_at_send_time_without_being_exposed(monkeypatch, caplog) -> None:
    bearer_value = "invented-bearer-value"
    monkeypatch.setenv("CLAWFLIGHT_NTFY_TOKEN", bearer_value)
    poster = NtfyPoster(
        "https://ntfy.example.com/demo-topic",
        token_env="CLAWFLIGHT_NTFY_TOKEN",
        opener=_RecordingOpener(),
    )

    with caplog.at_level(logging.DEBUG):
        request = poster.request_for("Update")

    assert request.get_header("Authorization") == "Bearer " + bearer_value
    assert bearer_value not in repr(poster)
    assert bearer_value not in caplog.text
    monkeypatch.delenv("CLAWFLIGHT_NTFY_TOKEN")
    assert poster.request_for("Update").get_header("Authorization") is None


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


def test_ascii_optional_headers_are_sent_and_unicode_title_is_omitted() -> None:
    configured = NtfyPoster(
        "https://ntfy.example.com/demo-topic",
        title="Flight update",
        tags="airplane,warning",
        opener=_RecordingOpener(),
    ).request_for("Update")
    emoji_title = NtfyPoster(
        "https://ntfy.example.com/demo-topic", opener=_RecordingOpener()
    ).request_for("🚨 Update")

    assert configured.get_header("Title") == "Flight update"
    assert configured.get_header("Tags") == "airplane,warning"
    assert emoji_title.get_header("Title") is None
