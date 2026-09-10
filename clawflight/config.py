"""Configuration loading, validation, and state-path resolution.

One file — ``clawflight.json`` — describes the owner, the attribution table,
the recipients, the mailbox adapter, and (optionally) the push upgrade. Secrets
appear only as *environment-variable names*; no credential is ever stored in
the config file or logged.

The loader is comment- and trailing-comma-tolerant so a hand-edited config with
``//`` notes still parses. It is deliberately not full JSON5.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .people import PersonTable, to_entries
from .recipients import RecipientConfig


DEFAULT_STATE_DIR = "~/.openclaw/clawflight"
CONFIG_FILENAME = "clawflight.json"
CONFIG_PATH_ENV = "CLAWFLIGHT_CONFIG"
STATE_DIR_ENV = "CLAWFLIGHT_STATE_DIR"

DEFAULT_HORIZON_DAYS = 3
MAX_HORIZON_DAYS = 30

_COMMENT_RE = re.compile(
    r"""("(?:\\.|[^"\\])*")|(/\*.*?\*/|//[^\n\r]*)""", re.DOTALL
)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


class ConfigError(ValueError):
    """Raised when a config file exists but cannot be parsed."""


@dataclass(frozen=True)
class MailboxSettings:
    """How confirmation email reaches clawflight.

    ``adapter`` is one of ``imap`` (poll a forwarding mailbox), ``mbox`` (read a
    local file drop — the offline/CI path), or ``none``.
    """

    adapter: str = "none"
    host: str = ""
    port: int = 993
    ssl: bool = True
    username: str = ""
    password_env: str = "CLAWFLIGHT_IMAP_PASSWORD"
    folder: str = "INBOX"
    path: str = ""
    trusted_senders: Tuple[str, ...] = ()
    poll_trusted_senders_only: bool = True
    max_messages: int = 50

    @property
    def enabled(self) -> bool:
        return self.adapter in ("imap", "mbox")


@dataclass(frozen=True)
class PushSettings:
    """The opt-in AeroDataBox upgrade. Absent or disabled means polling only."""

    enabled: bool = False
    base_url: str = "https://aerodatabox.p.rapidapi.com"
    rapidapi_key_env: str = "CLAWFLIGHT_RAPIDAPI_KEY"
    webhook_url: str = ""
    webhook_secret_env: str = "CLAWFLIGHT_WEBHOOK_SECRET"
    receiver_port: int = 8787
    path_prefix: str = ""
    min_credits: int = 50


@dataclass(frozen=True)
class Config:
    owner: str = ""
    people: PersonTable = field(default_factory=PersonTable)
    recipients: RecipientConfig = field(default_factory=RecipientConfig)
    mailbox: MailboxSettings = field(default_factory=MailboxSettings)
    push: PushSettings = field(default_factory=PushSettings)
    horizon_days: int = DEFAULT_HORIZON_DAYS
    state_dir: Path = field(default_factory=lambda: Path(DEFAULT_STATE_DIR).expanduser())
    source_path: Optional[Path] = None

    # -- resolved state paths ----------------------------------------------

    @property
    def registry_path(self) -> Path:
        return self.state_dir / "registry.json"

    @property
    def monitor_path(self) -> Path:
        return self.state_dir / "monitor.json"

    @property
    def outbox_path(self) -> Path:
        return self.state_dir / "outbox.json"

    @property
    def follows_path(self) -> Path:
        return self.state_dir / "follows.json"

    @property
    def consent_path(self) -> Path:
        return self.state_dir / "consent.json"

    @property
    def subscriptions_path(self) -> Path:
        return self.state_dir / "subscriptions.json"

    def ensure_state_dir(self) -> Path:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.state_dir, 0o700)
        except OSError:  # pragma: no cover - platform dependent
            pass
        return self.state_dir

    def to_dict(self) -> Dict[str, Any]:
        return {
            "owner": self.owner,
            "people": to_entries(self.people),
            "recipients": [
                {
                    "key": recipient.key,
                    "name": recipient.name,
                    "active": recipient.active,
                    "auto_follow_own": recipient.auto_follow_own,
                    "follow_all": recipient.follow_all,
                    "channel": dict(recipient.channel or {}),
                }
                for recipient in self.recipients.all_active()
            ],
            "mailbox": {
                "adapter": self.mailbox.adapter,
                "host": self.mailbox.host,
                "port": self.mailbox.port,
                "ssl": self.mailbox.ssl,
                "username": self.mailbox.username,
                "password_env": self.mailbox.password_env,
                "folder": self.mailbox.folder,
                "path": self.mailbox.path,
                "trusted_senders": list(self.mailbox.trusted_senders),
                "poll_trusted_senders_only": self.mailbox.poll_trusted_senders_only,
            },
            "push": {
                "enabled": self.push.enabled,
                "base_url": self.push.base_url,
                "rapidapi_key_env": self.push.rapidapi_key_env,
                "webhook_url": self.push.webhook_url,
                "webhook_secret_env": self.push.webhook_secret_env,
                "receiver_port": self.push.receiver_port,
                "path_prefix": self.push.path_prefix,
            },
            "horizon_days": self.horizon_days,
            "state_dir": str(self.state_dir),
        }


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def default_config_path() -> Path:
    override = os.environ.get(CONFIG_PATH_ENV)
    if override:
        return Path(override).expanduser()
    return Path(DEFAULT_STATE_DIR).expanduser() / CONFIG_FILENAME


def strip_json_comments(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments and trailing commas.

    String contents are preserved: the alternation captures a complete JSON
    string first, so a ``//`` inside a URL is never treated as a comment.
    """

    def _replace(match: "re.Match") -> str:
        return match.group(1) if match.group(1) else ""

    return _TRAILING_COMMA_RE.sub(r"\1", _COMMENT_RE.sub(_replace, text))


def loads(text: str) -> Dict[str, Any]:
    try:
        payload = json.loads(strip_json_comments(text))
    except json.JSONDecodeError as exc:
        raise ConfigError("config is not valid JSON: {}".format(exc)) from exc
    if not isinstance(payload, dict):
        raise ConfigError("config must be a JSON object")
    return payload


def load_config(path: Optional[object] = None) -> Config:
    """Load config from *path*, or the default location.

    A missing file yields an all-default Config: clawflight starts, attributes
    nothing, notifies nobody, and ``clawflight doctor`` explains what is needed.
    """
    resolved = Path(path).expanduser() if path is not None else default_config_path()
    try:
        text = resolved.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError):
        return Config(source_path=resolved, state_dir=_state_dir_from(None))
    except OSError as exc:
        raise ConfigError("config could not be read: {}".format(exc)) from exc
    return from_mapping(loads(text), source_path=resolved)


def from_mapping(
    payload: Mapping[str, Any], source_path: Optional[Path] = None
) -> Config:
    people = PersonTable.from_entries(payload.get("people"))
    recipients = RecipientConfig.from_entries(payload.get("recipients"))
    owner = payload.get("owner")
    owner = owner.strip().casefold() if isinstance(owner, str) else ""
    return Config(
        owner=owner,
        people=people,
        recipients=recipients,
        mailbox=_mailbox_from(payload.get("mailbox")),
        push=_push_from(payload.get("push")),
        horizon_days=_horizon_from(payload.get("horizon_days")),
        state_dir=_state_dir_from(payload.get("state_dir")),
        source_path=source_path,
    )


def _state_dir_from(value: object) -> Path:
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override).expanduser()
    if isinstance(value, str) and value.strip():
        return Path(value.strip()).expanduser()
    return Path(DEFAULT_STATE_DIR).expanduser()


def _horizon_from(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_HORIZON_DAYS
    return max(0, min(MAX_HORIZON_DAYS, value))


def _mailbox_from(value: object) -> MailboxSettings:
    if not isinstance(value, dict):
        return MailboxSettings()
    defaults = MailboxSettings()
    adapter = value.get("adapter")
    adapter = adapter.strip().casefold() if isinstance(adapter, str) else "none"
    if adapter not in ("imap", "mbox", "none"):
        adapter = "none"
    port = value.get("port")
    return MailboxSettings(
        adapter=adapter,
        host=_text(value.get("host"), defaults.host),
        port=port if isinstance(port, int) and not isinstance(port, bool) else defaults.port,
        ssl=bool(value.get("ssl", defaults.ssl)),
        username=_text(value.get("username"), defaults.username),
        password_env=_text(value.get("password_env"), defaults.password_env),
        folder=_text(value.get("folder"), defaults.folder),
        path=_text(value.get("path"), defaults.path),
        trusted_senders=_string_tuple(value.get("trusted_senders")),
        poll_trusted_senders_only=bool(
            value.get("poll_trusted_senders_only", defaults.poll_trusted_senders_only)
        ),
        max_messages=_positive_int(value.get("max_messages"), defaults.max_messages),
    )


def _push_from(value: object) -> PushSettings:
    if not isinstance(value, dict):
        return PushSettings()
    defaults = PushSettings()
    return PushSettings(
        enabled=bool(value.get("enabled", False)),
        base_url=_text(value.get("base_url"), defaults.base_url),
        rapidapi_key_env=_text(value.get("rapidapi_key_env"), defaults.rapidapi_key_env),
        webhook_url=_text(value.get("webhook_url"), defaults.webhook_url),
        webhook_secret_env=_text(
            value.get("webhook_secret_env"), defaults.webhook_secret_env
        ),
        receiver_port=_positive_int(value.get("receiver_port"), defaults.receiver_port),
        path_prefix=_text(value.get("path_prefix"), defaults.path_prefix),
        min_credits=_positive_int(value.get("min_credits"), defaults.min_credits),
    )


def _text(value: object, fallback: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _string_tuple(value: object) -> Tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(
        dict.fromkeys(
            item.strip() for item in value if isinstance(item, str) and item.strip()
        )
    )


def _positive_int(value: object, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return fallback
    return value


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    severity: str  # "error" | "warning" | "info"
    message: str


def validate(config: Config) -> List[Finding]:
    """Return an ordered list of configuration findings for ``doctor``.

    Never raises and never touches the network. Errors mean clawflight cannot
    do useful work; warnings mean it will run in a reduced mode.
    """
    findings: List[Finding] = []

    if not config.people:
        findings.append(
            Finding(
                "warning",
                "No people configured: every flight will be attributed to 'unknown'.",
            )
        )
    if not config.owner:
        findings.append(
            Finding("warning", "No owner configured: unknown-person flights have no default.")
        )
    elif config.people and config.people.get(config.owner) is None:
        findings.append(
            Finding(
                "error",
                "owner '{}' is not one of the configured people.".format(config.owner),
            )
        )

    active = config.recipients.all_active()
    if not active:
        findings.append(
            Finding("error", "No active recipients: alerts would be composed but never sent.")
        )
    for recipient in active:
        if not recipient.deliverable:
            findings.append(
                Finding(
                    "error",
                    "recipient '{}' has no channel target "
                    "(needs channel.channel and channel.to).".format(recipient.key),
                )
            )
        if config.people and config.people.get(recipient.key) is None:
            findings.append(
                Finding(
                    "info",
                    "recipient '{}' is not in the people table; it can still "
                    "follow flights explicitly.".format(recipient.key),
                )
            )

    mailbox = config.mailbox
    if mailbox.adapter == "none":
        findings.append(
            Finding(
                "warning",
                "No mailbox adapter: itineraries must be supplied by another ingestion path.",
            )
        )
    elif mailbox.adapter == "imap":
        if not mailbox.host or not mailbox.username:
            findings.append(
                Finding("error", "imap mailbox needs both host and username.")
            )
        if not os.environ.get(mailbox.password_env):
            findings.append(
                Finding(
                    "error",
                    "environment variable {} is not set (imap password).".format(
                        mailbox.password_env
                    ),
                )
            )
    elif mailbox.adapter == "mbox" and not mailbox.path:
        findings.append(Finding("error", "mbox mailbox needs a path."))
    if mailbox.enabled and not mailbox.trusted_senders:
        findings.append(
            Finding(
                "error",
                "mailbox.trusted_senders is empty: no message would ever be ingested.",
            )
        )

    if config.push.enabled:
        if not os.environ.get(config.push.rapidapi_key_env):
            findings.append(
                Finding(
                    "error",
                    "push is enabled but {} is not set.".format(
                        config.push.rapidapi_key_env
                    ),
                )
            )
        if not os.environ.get(config.push.webhook_secret_env):
            findings.append(
                Finding(
                    "error",
                    "push is enabled but {} is not set.".format(
                        config.push.webhook_secret_env
                    ),
                )
            )
        if not config.push.webhook_url:
            findings.append(
                Finding("error", "push is enabled but push.webhook_url is empty.")
            )
    else:
        findings.append(
            Finding("info", "Polling-only mode: keyless public feeds, no push upgrade.")
        )

    return findings


def with_state_dir(config: Config, state_dir: object) -> Config:
    """Return a copy of *config* rooted at a different state directory."""
    return replace(config, state_dir=Path(state_dir).expanduser())
