# Bring your own adapter

clawflight has exactly two pluggable edges: where messages come in, and where
alerts go out. Both are one-method interfaces. This is deliberate — it is how
personal integrations stay in your own workspace instead of in this repository.

## Inbound: `MailboxAdapter`

```python
class MailboxAdapter(Protocol):
    def fetch(self, limit: int = 50) -> list[MailboxMessage]: ...
```

```python
@dataclass(frozen=True)
class MailboxMessage:
    source_id: str        # stable for the life of the message — the dedup key
    sender: str           # checked against trusted_senders
    subject: str
    body: str             # plain text
    received_at: datetime # timezone-aware
```

`source_id` must be stable: a Message-ID, an IMAP `UIDVALIDITY/UID` pair, a file
path. Re-ingesting a message whose id and content digest are unchanged is a
no-op, which is what makes an hourly sweep safe.

### Shipped adapters

| adapter | use |
|---|---|
| `adapters.mailbox_mbox.MboxAdapter` | an mbox file or a folder of `.eml` files — no credentials, no network |
| `adapters.mailbox_imap.ImapAdapter` | a forwarding address over stdlib `imaplib` |

### Writing your own

```python
from datetime import datetime, timezone
from clawflight.adapters.mailbox import MailboxMessage, messages_to_candidates
from clawflight.registry import Registry

class MyCalendarAdapter:
    """Feed clawflight from whatever you already have."""

    def fetch(self, limit: int = 50) -> list[MailboxMessage]:
        return [
            MailboxMessage(
                source_id=item["id"],
                sender=item["organizer"],
                subject=item["title"],
                body=item["description"],
                received_at=datetime.now(timezone.utc),
            )
            for item in my_source.recent(limit)
        ]

candidates, skipped = messages_to_candidates(
    MyCalendarAdapter().fetch(), trusted_sources={"air.example"}
)
Registry(path, people).merge_email_candidates(candidates)
```

Two pipelines are available, and most adapters want both:

- `messages_to_candidates(...)` — labelled `Key: value` bodies. Strict: an
  incomplete or ambiguous message yields nothing rather than a partial booking.
- `messages_to_parsed_flights(...)` — the visual airline receipt layouts.

Both enforce the trusted-sender gate themselves. You never have to remember to.

### Adapters that stay private

The private ancestor of this package ingested from a personal Google Workspace
CLI and a macOS calendar CLI. Both conform to `MailboxAdapter` and both stay out
of this repository, because each is specific to one person's machine and needs
credentials no community package should ask for. Keep yours the same way: a
small module in your own workspace that imports `clawflight` and calls
`Registry.merge_email_candidates`.

## Outbound: `Poster`

```python
class Poster(Protocol):
    def post(self, text: str, priority: "NotificationPriority" = "info") -> bool: ...
```

Return `True` only when the message is genuinely accepted. `False` or an
exception marks the delivery failed, and the outbox retries it with exponential
backoff. Do **not** implement retry inside a poster — that is the outbox's job,
and duplicating it produces duplicate messages.

### Shipped posters

Choose a channel per recipient. The existing generic recipient command also
accepts ntfy without new CLI surface: `clawflight recipient add --channel ntfy
--to <url-or-topic>`.

```json
{
  "key": "alex",
  "name": "Alex",
  "channel": {
    "channel": "telegram",
    "to": "-1001234567890:topic:42",
    "thread_id": "42"
  }
}
```

Any non-`ntfy` channel uses
`adapters.channel_openclaw.OpenClawPoster`, which shells out to `openclaw
message send`. The argv is a list and there is no shell, so message text is
never interpretable as a command. `thread_id` is optional; omit it when the
channel does not use threads.

```json
{
  "key": "sam",
  "name": "Sam",
  "channel": {
    "channel": "ntfy",
    "to": "family",
    "base_url": "https://ntfy.example.com",
    "token_env": "CLAWFLIGHT_NTFY_TOKEN",
    "title": "Flight update",
    "tags": "airplane"
  }
}
```

`adapters.channel_ntfy.NtfyPoster` posts with `urllib.request`. Its `to` may
be a full `http(s)` topic URL, or a bare topic joined to `base_url`; bare topics
default to `https://ntfy.sh`. `base_url` is optional. To use authentication,
set `token_env` to the fixed value `CLAWFLIGHT_NTFY_TOKEN` and export the token
under that name. The token is read when each message is sent and is sent only
to an `https` URL. Omit `token_env` for unauthenticated publishing.

Older configs that use another `token_env` name must move that token to
`CLAWFLIGHT_NTFY_TOKEN` and replace the configured name. clawflight rejects the
old setting with a migration error rather than silently publishing without
authentication. `title` and `tags` are optional ntfy headers. The poster sends
priority 4 for critical alerts and 3 for informational alerts.

```python
from clawflight.adapters.channel_openclaw import poster_router

outbox.deliver_pending(None, now, poster_for=poster_router(config.recipients))
```

`poster_router` returns the `poster_for` callable the outbox drain expects:
recipient key → poster, or `None` when that recipient has no channel configured
(which the outbox records as a visible failure rather than a silent drop).

### Writing your own

```python
class WebhookPoster:
    def __init__(self, url: str, session):
        self.url, self.session = url, session

    def post(self, text: str, priority="info") -> bool:
        try:
            response = self.session.post(self.url, json={"text": text}, timeout=10)
        except Exception:
            return False          # the outbox will retry
        return 200 <= response.status_code < 300

outbox.deliver_pending(
    None, now, poster_for=lambda key: WebhookPoster(URLS[key], session)
)
```

## Inbound live data

The poll path takes its `Observation` from a caller-supplied fetcher, so the
package itself never opens a socket:

```python
def fetch(record) -> Observation: ...

run_once(fetch, registry, monitor, airports, poster, now_epoch=time.time())
```

A live fetcher builds its adsb.lol query from
`models.polling_callsign(record.leg)` — which prefers the *operating* carrier,
since a codeshare flies under the operator's callsign — and parses the response
with `status.parse_adsb`. Airport conditions come from `status.parse_faa_nas`.

The CLI ships a fetcher that returns position-free observations. Everything that
does not need a live position still works: the watch window, schedule-change
notes from ingested email, connection analysis, push-corroborated delays, and
the whole delivery path.
