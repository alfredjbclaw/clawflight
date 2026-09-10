# Privacy and data handling

clawflight processes other people's travel plans. That is inherently sensitive,
and this document states exactly what it does with them.

## What is stored

| stored | not stored |
|---|---|
| flight number, date, route, seat, confirmation code | message bodies |
| the traveler's configured `display` name | email addresses of senders or attendees |
| a source id and SHA-256 digest as provenance | the message the digest was taken from |
| phase, announced events, last position | any location that is not an aircraft position |
| composed alert text, pending until delivered | credentials of any kind |

Ingestion is deliberately lossy. `email_ingest.py` produces an
`EmailItineraryCandidate` carrying bounded fields plus an `EmailEvidence` whose
entire content is `(source_id, sender_identity, observed_at, source_kind,
digest)`. The body never enters the durable queue — it stays in your mailbox,
where you already control it.

The sender address is used to decide trust and is *not* written to
`registry.json`; provenance there is `mail:<source-id>:<digest>`.

## What is read

Only messages from senders in `mailbox.trusted_senders`, which you write
yourself. A base domain matches itself and true subdomains only, so
`delta.com` matches `notify.delta.com` and never `delta.com.phish.example`.
Everything else is skipped unparsed.

Use a dedicated forwarding address or a filtered label. clawflight has no reason
to see the rest of your mail, and the narrower the mailbox the smaller the
blast radius of a bug.

## Secrets

Config files hold the **name** of an environment variable, never a value.
There is no field anywhere in the schema that accepts a credential, and
`tests/test_no_pii.py` fails the build if one is committed.

The IMAP password is read at call time, passed straight to `login`, and never
stored on the adapter, logged, or included in an exception message.

## File permissions

Everything under `state_dir` is written `0600` inside a `0700` directory, via a
temp file and `os.replace`, so a partial write is never visible.

## Consenting adults

`consent.py` exists because tracking someone's flight is something they should
get a say in. It splits alerts in two:

- **Baseline** — cancellations, major delays, arrival. Always eligible: these
  are the things a family member needs to know.
- **Deep travel-day stream** — the minute-by-minute progress. Gated behind an
  unexpired opt-in for that specific itinerary.

Prompts are scheduled at T-48h (owner) and T-24h (everyone), claimed
idempotently so a scheduler retry cannot double-prompt, and expire at arrival.

The engine provides the ledger; wiring the prompt into your chat is your
choice. If you are tracking an adult who has not agreed to it, no amount of
software design makes that okay.

## Third parties

Polling-only mode contacts adsb.lol, adsbdb and the FAA NAS feed. Those requests
carry a flight callsign — a public identifier already broadcast by the aircraft
— and nothing about who is on board.

Push mode additionally sends flight numbers to AeroDataBox via RapidAPI, under
their terms, and receives webhooks at a URL you control.

No telemetry, analytics, or crash reporting of any kind is included.

## Deleting data

```sh
rm -rf ~/.openclaw/clawflight    # everything clawflight knows
```

Removing one flight: `clawflight status` for the id, then delete its entry from
`registry.json` and `monitor.json`. Completed flights are pruned automatically
30 days after departure, and acknowledged deliveries after 14 days.

## For contributors

`fixtures/` is entirely synthetic and must stay that way. Never paste a real
confirmation email into this repository — author a fixture instead.
`tests/test_no_pii.py` scans the working tree and git history on every run;
`tests/history_scan.sh` is the standalone form to run before publishing.
