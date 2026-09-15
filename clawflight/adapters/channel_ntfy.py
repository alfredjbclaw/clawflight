"""Delivery through ntfy's HTTP publish endpoint.

The opener is injected so tests can inspect the request without opening a
socket. Authentication is read from its named environment variable only when a
message is sent; it is never retained by the poster.
"""
from __future__ import annotations

import os
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from ..notify import NotificationPriority


DEFAULT_BASE_URL = "https://ntfy.sh"
DEFAULT_TIMEOUT_SECONDS = 30.0

# An opener receives the complete request and its explicit timeout, then
# returns the HTTP status. Keeping it this small makes the edge injectable.
Opener = Callable[[Request, float], int]


def urllib_opener(request: Request, timeout: float) -> int:
    """Open one request with urllib and return its HTTP status."""
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - validated URL
        return response.getcode()


class NtfyPoster:
    """Post messages to one ntfy topic with bounded, injected transport."""

    def __init__(
        self,
        to: str,
        *,
        base_url: Optional[str] = None,
        token_env: Optional[str] = None,
        title: Optional[str] = None,
        tags: Optional[str] = None,
        opener: Optional[Opener] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not to:
            raise ValueError("an ntfy poster needs a topic")
        self.url = _post_url(to, base_url)
        self.token_env = token_env
        self.title = title
        self.tags = tags
        self.timeout = timeout
        self._opener = opener or urllib_opener

    def request_for(self, text: str, priority: NotificationPriority = "info") -> Request:
        """Build the exact UTF-8 ntfy publish request for one message."""
        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "Priority": "4" if priority == "critical" else "3",
        }
        title = self.title if self.title is not None else text.split("\n", 1)[0]
        _add_ascii_header(headers, "Title", title)
        _add_ascii_header(headers, "Tags", self.tags)
        if self.token_env:
            token = os.environ.get(self.token_env)
            if token:
                _add_ascii_header(headers, "Authorization", "Bearer " + token)
        return Request(self.url, data=text.encode("utf-8"), headers=headers, method="POST")

    def post(self, text: str, priority: NotificationPriority = "info") -> bool:
        try:
            status = self._opener(self.request_for(text, priority), self.timeout)
        except Exception:  # opener failures are durable-outbox failures
            return False
        return 200 <= status < 300


def _post_url(to: str, base_url: Optional[str]) -> str:
    """Resolve an absolute topic URL or a bare topic against a safe base URL."""
    parsed_to = urlparse(to)
    if parsed_to.scheme:
        _validate_http_url(to, "to")
        if base_url is not None:
            _validate_http_url(base_url, "base_url")
        return to
    base = base_url or DEFAULT_BASE_URL
    _validate_http_url(base, "base_url")
    return urljoin(base.rstrip("/") + "/", to.lstrip("/"))


def _validate_http_url(value: str, field_name: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("{} must be an http(s) URL".format(field_name))


def _add_ascii_header(headers: dict, name: str, value: Optional[str]) -> None:
    """Add a header only when its complete value is safe for HTTP headers."""
    if value is None:
        return
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return
    headers[name] = value
