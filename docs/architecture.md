# Architecture

```
airline email ─▶ MailboxAdapter ─┐
calendar export ─────────────────┤
                                 ▼
                    parse ▸ attribute ▸ registry.json
                                 ▼
            watch loop (cron tick): adsb.lol + adsbdb + FAA NAS
                 (opt-in: AeroDataBox push webhook)
                                 ▼
              monitor.json ▸ events ▸ per-recipient fan-out
                                 ▼
                    outbox.json ▸ Poster ▸ your group chat
```

Two rules shape everything below.

**Evidence, not guesses.** Where the engine cannot prove something it records
nothing rather than inventing it. An unmapped city yields a leg with a missing
airport. An unmatched name yields `unknown`. A delay with no corroboration
yields an informational note, not an alert.

**Durable before delivered.** Every event is persisted before the first
delivery attempt, and only marked delivered when the channel acknowledges it.
A crash between the two costs a retry, never a message.

## Modules

| module | responsibility |
|---|---|
| `models.py` | frozen dataclasses; the vocabulary everything else shares |
| `airports.py` | packaged airport table, haversine, progress, ETA |
| `parse.py` | calendar exports and the three airline email layouts |
| `email_ingest.py` | trusted-sender gate; labelled fields → bounded candidates |
| `people.py` | config-driven attribution and its evidence ranking |
| `registry.py` | the single writer of `registry.json`: merge, attribute, group |
| `monitor.py` | phase state machine, delay buckets, push diffing |
| `connections.py` | tight and missed connections across an itinerary |
| `notify.py` | message composition and the durable delivery outbox |
| `recipients.py` | who is subscribed to which flight |
| `consent.py` | per-itinerary opt-in for the deeper travel-day stream |
| `runner.py` | one monitoring pass |
| `audit.py` | non-mutating health report |
| `config.py` | load, validate, resolve state paths |
| `cli.py` | `setup tick sweep status follow mute doctor` |
| `adapters/` | the two pluggable edges (see [adapters.md](adapters.md)) |
| `aerodatabox.py`, `webhook_receiver.py` | the opt-in push upgrade |

Nothing performs network I/O at import. The HTTP client is always a
caller-supplied callable, which is why the whole suite runs offline.

## Attribution

Evidence is ranked so a weaker later source can never overwrite a stronger
earlier one:

| rank | evidence |
|---|---|
| 3 | an explicit passenger name on a booking |
| 2 | a possessive calendar title — "Alex's flight home" |
| 1 | a known attendee address on a calendar event |
| 0 | nothing → `unknown` |

The rank is persisted alongside each record, so merge order across a restart
does not change the outcome. Attendee local-parts must match **exactly** once
separators are stripped; a prefix match would let `alexis@` attribute to `alex`.

## Duplicate bookings are a feature

Booking two flights for one person on one day is a deliberate hedge, and
"deduplicating" it silently drops a real ticket. So:

- Same flight, same date, **different confirmation codes** → two records, the
  second keyed `AA4912-2026-07-11#FAKE02`.
- Same person, same date, departures within four hours, **different codes** →
  one *backup group*, both flagged, neither dropped.
- Same **confirmation code** → legs of one itinerary, i.e. a connection. Never
  a backup pair, and eligible for connection analysis.

## The phase machine

```
scheduled ──(T-6h)──▶ watch ──(airborne signal)──▶ airborne
                                                      │
                                             (progress ≥ 50%)
                                                      ▼
   landed ◀──(near destination, or track lost past 85%)── halfway
```

Every milestone is emitted at most once. The `sent` list persists in
`monitor.json`, so a restart mid-flight does not replay takeoff.

Two guards worth knowing:

- **Takeoff needs evidence**: ≥1000 ft *and* either a climb rate over 300 fpm
  or ground speed over 140 kt. A stationary ground report is not a departure.
- **Delay needs corroboration**: elapsed time past a scheduled departure is not
  enough on its own. Regional and codeshare callsigns never appear on ADS-B, so
  "elapsed since scheduled" grows without bound for a flight that left on time.
  An elapsed-time bucket escalates only alongside an FAA airport delay or a
  pushed departure revision; otherwise it emits one informational
  "no position data" note.

## Push updates

`ingest_push` diffs a vendor update against persisted state and emits only what
changed. Three cases are handled deliberately:

- A revision **later** than schedule is a delay. Normal.
- A revision whose wall-clock time is already **more than five minutes past** is
  suppressed and logged as a data anomaly — that is bad vendor data, not news.
- A revision **more than 45 minutes earlier** than schedule is hedged, not
  asserted: "the airline reports … — unconfirmed; the airline may have bad
  data." Aircraft rarely leave three quarters of an hour early.

Each revised value is announced once, so an A→B→A schedule flip does not
re-alert on the return to A.

When several bookings share one physical flight, the update is diffed **once**
and its events fanned out, so two people on the same aircraft get consistent
text.

## Delivery

`DeliveryOutbox` gives each `(flight, event, recipient)` triple a stable
24-character delivery id. That id is the dedup key, so re-enqueueing an
identical event is a no-op even after delivery.

Failures back off exponentially (30 s doubling, capped at 30 min) **per
recipient**, so a broken Signal bridge never delays the Telegram copy.

`Poster` is one method — `post(text) -> bool`. The shipped implementation shells
out to `openclaw message send` with an argv list and no shell, so a hostile gate
value from a webhook can never be interpolated into a command.

## State

All under `state_dir` (default `~/.openclaw/clawflight/`), every file `0600`
inside a `0700` directory, every write atomic via temp file + `os.replace`.

| file | holds |
|---|---|
| `registry.json` | known flights, attribution, backup groups |
| `monitor.json` | phase, announced events, last known position, push fields |
| `outbox.json` | pending, failed, and acknowledged deliveries |
| `follows.json` | per-recipient follow and mute lists |
| `consent.json` | per-itinerary opt-in decisions and prompt timestamps |
| `subscriptions.json` | push subscription ids (push mode only) |

`monitor.json` is written by both the tick and, in push mode, the webhook
receiver — separate processes. Every read-modify-write holds an exclusive
`flock` and re-reads under it, so neither clobbers the other.

## What this package deliberately does not do

- **No drive-home ETA.** It needs a home address; that is a personal-location
  feature and it is out of scope.
- **No airline-site scraping.** It needs a warmed browser profile and real legal
  names, and it is ToS-grey.
- **No message bodies retained.** Ingestion keeps a source id and a SHA-256
  digest as provenance. The message stays in your mailbox.
- **No credentials in code or config.** Only environment-variable *names*.
