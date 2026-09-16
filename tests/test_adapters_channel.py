"""Delivery through ``openclaw message send``: argv, routing, failure modes."""
from __future__ import annotations

import logging
import os

import pytest

from clawflight.adapters.channel_openclaw import (
    DEFAULT_BINARY,
    OpenClawPoster,
    binary_available,
    poster_for_recipient,
    poster_router,
    subprocess_runner,
)
from clawflight.adapters.channel_ntfy import (
    NtfyPoster,
    TOKEN_ENV,
    _HttpsTokenRedirectHandler,
)
from clawflight.recipients import RecipientConfig


class _RecordingRunner:
    """Captures argv instead of spawning anything."""

    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        return self.code


class _RecordingOpener:
    """Captures ntfy requests instead of opening a socket."""

    def __init__(self) -> None:
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        return 200


CONFIG = RecipientConfig.from_entries(
    [
        {
            "key": "alex",
            "name": "Alex",
            "channel": {"channel": "telegram", "to": "-1009876543210", "thread_id": "42"},
        },
        {
            "key": "sam",
            "name": "Sam",
            "channel": {"channel": "whatsapp", "to": "+15550000000"},
        },
        {
            "key": "ntfy",
            "name": "Ntfy",
            "channel": {"channel": "ntfy", "to": "https://ntfy.example.com/demo-topic"},
        },
        {
            "key": "unknown",
            "name": "Unknown",
            "channel": {"channel": "carrier-pigeon", "to": "roost"},
        },
        {"key": "robin", "name": "Robin"},
    ]
)


def test_the_poster_builds_a_channel_agnostic_argv() -> None:
    runner = _RecordingRunner()
    poster = OpenClawPoster("telegram", "-1009876543210", runner=runner)

    assert poster.post("AA4912 is delayed.") is True
    assert runner.calls[0][0] == [
        DEFAULT_BINARY,
        "message",
        "send",
        "--channel",
        "telegram",
        "--target",
        "-1009876543210",
        "--message",
        "AA4912 is delayed.",
    ]


def test_a_thread_id_is_appended_only_when_configured() -> None:
    runner = _RecordingRunner()

    OpenClawPoster("telegram", "-100", runner=runner, thread_id="42").post("hi")
    OpenClawPoster("telegram", "-100", runner=runner).post("hi")

    assert runner.calls[0][0][-2:] == ["--thread-id", "42"]
    assert "--thread-id" not in runner.calls[1][0]


def test_a_non_zero_exit_is_reported_as_a_failed_delivery() -> None:
    poster = OpenClawPoster("telegram", "-100", runner=_RecordingRunner(code=1))

    assert poster.post("AA4912 is delayed.") is False


def test_the_binary_and_timeout_are_configurable() -> None:
    runner = _RecordingRunner()
    poster = OpenClawPoster(
        "slack", "channel:C123", binary="/opt/bin/openclaw", runner=runner, timeout=5.0
    )

    poster.post("hello")

    assert runner.calls[0][0][0] == "/opt/bin/openclaw"
    assert runner.calls[0][1] == 5.0


def test_a_poster_cannot_be_built_without_a_channel_and_target() -> None:
    with pytest.raises(ValueError):
        OpenClawPoster("", "-100")
    with pytest.raises(ValueError):
        OpenClawPoster("telegram", "")


def test_poster_for_recipient_returns_none_without_a_target() -> None:
    runner = _RecordingRunner()

    assert poster_for_recipient(CONFIG.get("alex"), runner=runner) is not None
    assert poster_for_recipient(CONFIG.get("robin"), runner=runner) is None


def test_poster_dispatches_ntfy_and_keeps_other_channels_on_openclaw() -> None:
    runner = _RecordingRunner()

    assert isinstance(poster_for_recipient(CONFIG.get("ntfy"), runner=runner), NtfyPoster)
    assert isinstance(poster_for_recipient(CONFIG.get("sam"), runner=runner), OpenClawPoster)
    assert isinstance(poster_for_recipient(CONFIG.get("unknown"), runner=runner), OpenClawPoster)


def test_ntfy_config_cannot_name_an_unrelated_environment_variable(monkeypatch) -> None:
    unrelated_value = "invented-unrelated-value"
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", unrelated_value)
    environment_lookups = []

    def recording_get(name, default=None):
        environment_lookups.append(name)
        return original_get(name, default)

    original_get = os.environ.get
    monkeypatch.setattr("clawflight.adapters.channel_ntfy.os.environ.get", recording_get)
    opener = _RecordingOpener()
    config = RecipientConfig.from_entries(
        [
            {
                "key": "ntfy-negative",
                "name": "Ntfy Negative",
                "channel": {
                    "channel": "ntfy",
                    "to": "https://ntfy.example.com/invented-topic",
                    "token_env": "AWS_SECRET_ACCESS_KEY",
                },
            }
        ]
    )

    with pytest.raises(ValueError, match="move the token") as raised:
        poster_for_recipient(config.get("ntfy-negative"), opener=opener)

    assert unrelated_value not in str(raised.value)
    assert "AWS_SECRET_ACCESS_KEY" not in environment_lookups
    assert opener.calls == []


def test_ntfy_reads_only_the_fixed_token_at_each_send(monkeypatch) -> None:
    first_value = "invented-first-bearer"
    second_value = "invented-second-bearer"
    opener = _RecordingOpener()
    monkeypatch.setenv(TOKEN_ENV, first_value)
    poster = NtfyPoster(
        "https://ntfy.example.com/invented-topic",
        token_env=TOKEN_ENV,
        opener=opener,
    )

    assert poster.post("AA4912 gate update.")
    monkeypatch.setenv(TOKEN_ENV, second_value)
    assert poster.post("AA4912 boarding update.")

    assert opener.calls[0][0].get_header("Authorization") == "Bearer " + first_value
    assert opener.calls[1][0].get_header("Authorization") == "Bearer " + second_value
    assert first_value not in vars(poster).values()
    assert second_value not in vars(poster).values()


def test_ntfy_rejects_token_authentication_over_http_without_sending() -> None:
    opener = _RecordingOpener()

    with pytest.raises(ValueError, match="requires an https URL"):
        NtfyPoster(
            "http://ntfy.example.com/invented-topic",
            token_env=TOKEN_ENV,
            opener=opener,
        )

    assert opener.calls == []


def test_ntfy_rejects_authenticated_redirect_from_https_to_http(monkeypatch) -> None:
    bearer_value = "invented-redirect-bearer"
    monkeypatch.setenv(TOKEN_ENV, bearer_value)
    attempted_requests = []

    def redirecting_opener(request, timeout):
        attempted_requests.append(request)
        return _HttpsTokenRedirectHandler().redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "http://redirect.example.com/invented-topic",
        )

    poster = NtfyPoster(
        "https://ntfy.example.com/invented-topic",
        token_env=TOKEN_ENV,
        opener=redirecting_opener,
    )

    assert poster.post("AA4912 gate update.") is False
    assert len(attempted_requests) == 1
    assert attempted_requests[0].full_url.startswith("https://")
    assert attempted_requests[0].get_header("Authorization") == "Bearer " + bearer_value


def test_ntfy_token_is_absent_from_logs_repr_and_validation_error(
    monkeypatch, caplog
) -> None:
    bearer_value = "invented-private-bearer"
    monkeypatch.setenv(TOKEN_ENV, bearer_value)
    sent_opener = _RecordingOpener()
    blocked_opener = _RecordingOpener()
    poster = NtfyPoster(
        "https://ntfy.example.com/invented-topic",
        token_env=TOKEN_ENV,
        opener=sent_opener,
    )

    with caplog.at_level(logging.DEBUG):
        assert poster.post("AA4912 gate update.")
    with pytest.raises(ValueError) as raised:
        NtfyPoster(
            "http://ntfy.example.com/invented-topic",
            token_env=TOKEN_ENV,
            opener=blocked_opener,
        )

    assert bearer_value not in str(raised.value)
    assert bearer_value not in repr(poster)
    assert bearer_value not in caplog.text
    assert blocked_opener.calls == []


def test_ntfy_request_keeps_body_priority_base_url_and_rfc2047_title() -> None:
    opener = _RecordingOpener()
    poster = NtfyPoster(
        "invented-topic",
        base_url="https://ntfy.example.com",
        title="✈️ AA4912 update",
        opener=opener,
    )

    assert poster.post("AA4912 is delayed.", "critical")
    request = opener.calls[0][0]

    assert request.full_url == "https://ntfy.example.com/invented-topic"
    assert request.get_method() == "POST"
    assert request.data == "AA4912 is delayed.".encode("utf-8")
    assert request.get_header("Priority") == "4"
    assert request.get_header("Title").startswith("=?utf-8?")


def test_the_router_resolves_each_recipient_to_its_own_channel() -> None:
    runner = _RecordingRunner()
    route = poster_router(CONFIG, runner=runner)

    route("alex").post("one")
    route("sam").post("two")

    assert runner.calls[0][0][3:7] == ["--channel", "telegram", "--target", "-1009876543210"]
    assert runner.calls[1][0][3:7] == ["--channel", "whatsapp", "--target", "+15550000000"]
    assert route("robin") is None
    assert route("nobody") is None


def test_the_router_caches_its_lookups() -> None:
    route = poster_router(CONFIG, runner=_RecordingRunner())

    assert route("alex") is route("alex")


def test_the_default_runner_never_uses_a_shell(monkeypatch) -> None:
    seen = {}

    class _Completed:
        returncode = 0

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return _Completed()

    monkeypatch.setattr("clawflight.adapters.channel_openclaw.subprocess.run", fake_run)

    assert subprocess_runner(["openclaw", "message", "send"], 30.0) == 0
    assert seen["argv"] == ["openclaw", "message", "send"]
    assert "shell" not in seen["kwargs"]
    assert seen["kwargs"]["timeout"] == 30.0
    assert seen["kwargs"]["check"] is False


def test_the_default_runner_reports_a_missing_binary_as_failure(monkeypatch) -> None:
    def raise_oserror(argv, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr("clawflight.adapters.channel_openclaw.subprocess.run", raise_oserror)

    assert subprocess_runner(["nope"], 1.0) == 1


def test_a_timeout_is_reported_as_failure_not_an_exception(monkeypatch) -> None:
    import subprocess

    def raise_timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1.0)

    monkeypatch.setattr("clawflight.adapters.channel_openclaw.subprocess.run", raise_timeout)

    assert subprocess_runner(["openclaw"], 1.0) == 1


def test_binary_availability_is_reported_without_running_anything(monkeypatch) -> None:
    monkeypatch.setattr(
        "clawflight.adapters.channel_openclaw.shutil.which", lambda name: "/usr/bin/" + name
    )
    assert binary_available("openclaw") is True

    monkeypatch.setattr("clawflight.adapters.channel_openclaw.shutil.which", lambda name: None)
    assert binary_available("openclaw") is False


def test_no_message_text_is_ever_interpolated_into_a_shell_string() -> None:
    # A hostile gate value from a webhook must stay one argv element.
    runner = _RecordingRunner()
    hostile = 'B12"; rm -rf / #'

    OpenClawPoster("telegram", "-100", runner=runner).post(hostile)

    assert runner.calls[0][0][-1] == hostile
    assert len(runner.calls[0][0]) == 9
