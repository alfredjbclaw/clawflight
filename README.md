# clawflight

**Family flight alerts in your group chat, powered by a forwarding address.**

Forward your airline confirmation emails to a mailbox clawflight watches (or point it
at an inbox over IMAP) and it does the rest: parses the itinerary, figures out whose
flight it is, watches the flight with free public data feeds, and posts alerts to
whatever chat your family already uses — takeoff, landing, delays, gate changes,
schedule changes, cancellations.

- **Zero signup by default.** The stock configuration uses only keyless public
  feeds — [adsb.lol](https://adsb.lol) live positions, [adsbdb](https://adsbdb.com)
  routes, and FAA NAS airport status. No API keys, no webhooks, no tunnels, no
  credit card. Optional upgrade: an AeroDataBox key turns on true push alerts
  (airline-side gate/schedule/cancellation data, free tier available).
- **Any channel.** Alerts go through your OpenClaw channel layer — Telegram,
  iMessage, WhatsApp, Discord, Slack — whatever your family group chat runs on.
- **Per-person attribution.** Flights are matched to the family member traveling
  (from passenger names, calendar ownership, or email sender), and each person
  can follow or mute any flight ("follow DL939", "mute AA1203").
- **Backup-booking aware.** Two flights, same person, same day is treated as an
  intentional primary/backup pair — flagged, never "deduplicated" away.
- **Offline-testable engine.** The core is pure-stdlib Python with a 200+ test
  suite that runs with no network and no credentials.

## Status

Pre-release scaffold. The engine exists and is battle-tested privately; this repo
is the extraction into a community-shippable OpenClaw add-on. See
[EXTRACTION-PLAN.md](EXTRACTION-PLAN.md) for the build spec and current state.

## How it will work

```
airline email ──▶ forwarding address / IMAP mailbox ──┐
calendar (optional adapter) ──────────────────────────┤
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

## License

TBD — see [LICENSE](LICENSE).
