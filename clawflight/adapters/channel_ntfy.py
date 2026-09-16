"""Delivery through ntfy's HTTP publish endpoint.

The opener is injected so tests can inspect the request without opening a
socket. Authentication is read from the package-owned environment variable
only when a message is sent; it is never retained by the poster.
"""
from __future__ import annotations

import os
import re
from email.header import Header
from typing import Callable, Optional
from urllib.error import HTTPError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..notify import NotificationPriority


DEFAULT_BASE_URL = "https://ntfy.sh"
DEFAULT_TIMEOUT_SECONDS = 30.0
TOKEN_ENV = "CLAWFLIGHT_NTFY_TOKEN"

# Alert text carries the marketed flight number. Keep the ntfy title short and
# useful rather than copying its emoji-prefixed text.
_FLIGHT_IDENTIFIER = re.compile(
    r"(?<![A-Z0-9])(?=[A-Z0-9]{2,3}\d{1,4}\b)(?=[A-Z0-9]*[A-Z])"
    r"([A-Z0-9]{2,3}\d{1,4})\b"
)

# An opener receives the complete request and its explicit timeout, then
# returns the HTTP status. Keeping it this small makes the edge injectable.
Opener = Callable[[Request, float], int]


class _HttpsTokenRedirectHandler(HTTPRedirectHandler):
    """Refuse to move an authenticated publish request off HTTPS."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.has_header("Authorization") and urlparse(newurl).scheme.lower() != "https":
            raise HTTPError(
                newurl,
                code,
                "ntfy authentication redirect requires an https URL",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_HTTPS_TOKEN_OPENER = build_opener(_HttpsTokenRedirectHandler())


def urllib_opener(request: Request, timeout: float) -> int:
    """Open one request with urllib and return its HTTP status."""
    with _HTTPS_TOKEN_OPENER.open(request, timeout=timeout) as response:  # noqa: S310
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
        if token_env is not None and token_env != TOKEN_ENV:
            raise ValueError(
                "ntfy token_env must be {}; move the token to that environment "
                "variable or omit token_env".format(TOKEN_ENV)
            )
        self._use_token = token_env is not None
        if self._use_token and urlparse(self.url).scheme != "https":
            raise ValueError("ntfy authentication requires an https URL")
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
        title = self.title if self.title is not None else _title_from_text(text)
        _add_title_header(headers, title)
        _add_ascii_header(headers, "Tags", self.tags)
        if self._use_token:
            token = os.environ.get(TOKEN_ENV)
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


def _title_from_text(text: str) -> Optional[str]:
    """Return the first short flight identifier present in an alert."""
    match = _FLIGHT_IDENTIFIER.search(text)
    return match.group(1) if match is not None else None


def _add_title_header(headers: dict, title: Optional[str]) -> None:
    """Add a non-empty title, encoding non-ASCII values for HTTP transport."""
    if title is None or not title.strip() or "\r" in title or "\n" in title:
        return
    try:
        title.encode("ascii")
    except UnicodeEncodeError:
        encoded = Header(title, "utf-8").encode(linesep=" ")
        if "\r" in encoded or "\n" in encoded:
            return
        headers["Title"] = encoded
        return
    headers["Title"] = title
