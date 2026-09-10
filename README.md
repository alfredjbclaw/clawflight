# clawflight

**Family flight alerts in your group chat, powered by a forwarding address.**

Forward your airline confirmation emails to a mailbox clawflight watches (or
point it at an inbox over IMAP) and it does the rest: parses the itinerary,
works out whose flight it is, watches the flight with free public data feeds,
and posts alerts to whatever chat your family already uses — takeoff, landing,
delays, schedule changes, cancellations, tight connections.

```
airline email ─▶ forwarding address / IMAP mailbox ─┐
calendar export (optional adapter) ─────────────────┤
                                                    ▼
                                     parse ▸ attribute ▸ registry
                                                    ▼
                     watch loop (OpenClaw cron): adsb.lol + adsbdb + FAA NAS
                            (opt-in: AeroDataBox push webhook)
                                                    ▼
                                 events ▸ per-recipient fan-out ▸ outbox
                                                    ▼
                               your group chat (any OpenClaw channel)
```

## Why it looks like this

- **Zero signup by default.** The stock configuration uses only keyless public
  feeds — [adsb.lol](https://adsb.lol) positions, [adsbdb](https://adsbdb.com)
  routes, and FAA NAS airport status. No API keys, no webhooks, no tunnels, no
  card. An [opt-in upgrade](docs/push-upgrade.md) adds airline-side gate and
  schedule data.
- **Any channel.** Alerts go through `openclaw message send`, so Telegram,
  iMessage, WhatsApp, Discord, Slack, Signal and every channel plugin work with
  the same two lines of config.
- **Per-person attribution.** Flights are matched to the family member
  travelling — from passenger names, possessive calendar titles, or attendee
  addresses — and each person can follow or mute any flight.
- **Backup bookings survive.** Two flights, one person, one day is a deliberate
  hedge. clawflight flags it as a primary/backup pair; it never "deduplicates"
  a real ticket away.
- **Nothing costs tokens while idle.** The cron jobs are command payloads with
  no model call, and `tick` exits immediately when nobody is within six hours of
  a departure.
- **Offline-testable.** Pure standard library, zero runtime dependencies, and a
  380-test suite that runs with no network and no credentials.

## Quickstart

```sh
pip install clawflight
mkdir -p ~/.openclaw/clawflight
cp examples/clawflight.example.json ~/.openclaw/clawflight/clawflight.json
$EDITOR ~/.openclaw/clawflight/clawflight.json    # people, recipients, mailbox
clawflight doctor                                 # says exactly what is missing
clawflight setup                                  # prints the two cron jobs
```

Full walkthrough: **[docs/setup.md](docs/setup.md)**.

Try it without configuring anything — this drives the whole pipeline over the
synthetic fixtures, offline:

```sh
make demo
```

## Everyday use

```sh
clawflight status                                   # what is tracked
clawflight follow DL767-2026-07-16 --recipient sam  # sam wants this one too
clawflight mute DL767-2026-07-16                    # stop hearing about it
clawflight doctor                                   # validate config, audit state
```

Installed as an OpenClaw skill, your agent drives those for you: *"follow
Robin's flight"*, *"mute DL767"*, *"what flights are coming up?"*

## Configuration in one glance

```json
{
  "owner": "alex",
  "people": [
    { "key": "alex", "display": "Alex",
      "match_substrings": ["alex kestrel", "alexandra morgan kestrel"],
      "match_email_localparts": ["alex", "alex.kestrel"] }
  ],
  "recipients": [
    { "key": "alex", "name": "Alex", "follow_all": true,
      "channel": { "channel": "telegram", "to": "-1001234567890:topic:42" } }
  ],
  "mailbox": {
    "adapter": "imap",
    "host": "imap.gmail.com",
    "username": "family-flights@example.com",
    "password_env": "CLAWFLIGHT_IMAP_PASSWORD",
    "trusted_senders": ["delta.com", "aa.com", "united.com"]
  }
}
```

Secrets are referenced by environment-variable **name** only; no field in the
schema accepts a credential value. Defaults are empty: a fresh install
attributes nothing and notifies nobody until you say who is who.

## What it will not do

No drive-home ETA (that needs a home address). No airline-site scraping. No
retained message bodies — ingestion keeps a source id and a digest, and the
message stays in your mailbox. No telemetry.

See **[docs/privacy.md](docs/privacy.md)** for exactly what is stored, and
**[docs/architecture.md](docs/architecture.md)** for how it works.

## Documentation

| | |
|---|---|
| [docs/setup.md](docs/setup.md) | install, configure, schedule, troubleshoot |
| [docs/architecture.md](docs/architecture.md) | modules, attribution, phase machine, delivery |
| [docs/adapters.md](docs/adapters.md) | bring your own mailbox or channel |
| [docs/push-upgrade.md](docs/push-upgrade.md) | the opt-in AeroDataBox path |
| [docs/privacy.md](docs/privacy.md) | what is stored, read, and sent where |
| [EXTRACTION-PLAN.md](EXTRACTION-PLAN.md) | the build spec this repo was extracted against |

## Development

```sh
python3 -m pytest tests -q     # 380+ tests, offline, no credentials
make gate                      # tests + the PII history scan
```

Everything in `fixtures/` is synthetic and must stay that way — see
[fixtures/README.md](fixtures/README.md). `tests/test_no_pii.py` fails the build
if real personal data appears in the working tree or in git history.

## Status

**Pre-release.** The engine is battle-tested privately; this repository is the
sanitized public extraction. The API may still move before 1.0.

## License

See [LICENSE](LICENSE).
