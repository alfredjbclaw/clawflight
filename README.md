# clawflight

[![CI](https://github.com/alfredjbclaw/clawflight/actions/workflows/ci.yml/badge.svg)](https://github.com/alfredjbclaw/clawflight/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)

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
  380+ test suite that runs with no network and no credentials.

## Quickstart

Not on PyPI yet — install from source:

```sh
pip install git+https://github.com/alfredjbclaw/clawflight
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

### As an OpenClaw skill

The skill is self-contained: it carries the engine, which has no dependencies,
so there is nothing to install beyond the skill itself.

```sh
openclaw skills install @alfredjbclaw/clawflight
```

Your agent then drives the verbs for you: *"follow Robin's flight"*,
*"mute DL767"*, *"what flights are coming up?"*

Build the bundle yourself with `make skill-bundle`.

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
make gate                      # tests + the PII scan over history
```

CI runs the suite on Python 3.9–3.13 (and macOS), the PII scan over the working
tree and every blob in history, and a packaging check that installs the wheel
and runs the CLI. It also plants a known-bad address and requires the scanner to
reject it, so a scan that has quietly stopped working cannot pass as clean.

Everything in `fixtures/` is synthetic and must stay that way — see
[fixtures/README.md](fixtures/README.md). `tests/test_no_pii.py` fails the build
if third-party personal data appears in the working tree, in git history, or in
commit metadata. `tests/pii_scan.py` runs the same scan standalone.

## Status

**Pre-release.** The engine is battle-tested privately; this repository is the
sanitized public extraction. The API may still move before 1.0.

## Contact

Bug reports and feature requests are best filed as
[issues](https://github.com/alfredjbclaw/clawflight/issues) — they are public,
searchable, and someone else has probably hit the same thing.

For anything you would rather not put in public — a security report, or a
question that would mean pasting a real itinerary — mail
**alfred.j.berchtold@gmail.com** directly. Please do not paste real
confirmation emails into an issue: redact the passenger name, confirmation
code and any loyalty number first, or send a synthetic reproduction instead.

Maintained by Alfred J Berchtold ([@alfredjbclaw](https://github.com/alfredjbclaw)).

## License

MIT © 2026 Alfred J Berchtold — see [LICENSE](LICENSE).
