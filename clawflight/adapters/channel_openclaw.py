"""Delivery through ``openclaw message send``.

One outbound CLI covers every OpenClaw channel — Telegram, iMessage, WhatsApp,
Discord, Slack, Signal, Matrix, Teams, Google Chat, and channel plugins — so
this adapter is intentionally thin. It builds an argv, runs it, and reports
whether the process succeeded. Durable retry stays in the outbox.

The runner is injected, so tests assert on argv and never spawn a process.
"""
from __future__ import annotations

import shutil
import subprocess
from typing import Callable, List, Optional, Sequence

from ..notify import Poster
from ..recipients import Recipient, RecipientConfig


DEFAULT_BINARY = "openclaw"
DEFAULT_TIMEOUT_SECONDS = 30

#: A runner takes an argv and a timeout and returns a process exit code.
Runner = Callable[[Sequence[str], float], int]


def subprocess_runner(argv: Sequence[str], timeout: float) -> int:
    """Default runner: run argv with no shell and a bounded timeout."""
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, shell=False
            list(argv),
            timeout=timeout,
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 1
    return completed.returncode


class OpenClawPoster:
    """Post one channel's messages via ``openclaw message send``."""

    def __init__(
        self,
        channel: str,
        target: str,
        *,
        binary: str = DEFAULT_BINARY,
        runner: Optional[Runner] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        thread_id: Optional[str] = None,
    ) -> None:
        if not channel or not target:
            raise ValueError("an OpenClaw poster needs both a channel and a target")
        self.channel = channel
        self.target = target
        self.binary = binary
        self.timeout = timeout
        self.thread_id = thread_id
        self._runner = runner or subprocess_runner

    def argv(self, text: str) -> List[str]:
        argv = [
            self.binary,
            "message",
            "send",
            "--channel",
            self.channel,
            "--target",
            self.target,
            "--message",
            text,
        ]
        if self.thread_id:
            argv.extend(["--thread-id", self.thread_id])
        return argv

    def post(self, text: str) -> bool:
        return self._runner(self.argv(text), self.timeout) == 0


def poster_for_recipient(
    recipient: Recipient,
    *,
    binary: str = DEFAULT_BINARY,
    runner: Optional[Runner] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Optional[OpenClawPoster]:
    """Build a poster for one recipient, or None when it has no channel target."""
    if not recipient.deliverable:
        return None
    channel = recipient.channel or {}
    return OpenClawPoster(
        str(channel["channel"]),
        str(channel["to"]),
        binary=binary,
        runner=runner,
        timeout=timeout,
        thread_id=channel.get("thread_id"),
    )


def poster_router(
    config: RecipientConfig,
    *,
    binary: str = DEFAULT_BINARY,
    runner: Optional[Runner] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Callable[[str], Optional[Poster]]:
    """Return the ``poster_for`` callable the outbox drain expects."""
    cache = {}

    def resolve(recipient_key: str) -> Optional[Poster]:
        if recipient_key not in cache:
            recipient = config.get(recipient_key)
            cache[recipient_key] = (
                None
                if recipient is None
                else poster_for_recipient(
                    recipient, binary=binary, runner=runner, timeout=timeout
                )
            )
        return cache[recipient_key]

    return resolve


def binary_available(binary: str = DEFAULT_BINARY) -> bool:
    """Whether the outbound CLI is on PATH. Used by ``clawflight doctor``."""
    return shutil.which(binary) is not None
