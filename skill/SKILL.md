---
name: clawflight
description: Track family flights and post alerts to a group chat. Use for follow/mute a flight, what flights are tracked, flight status, or clawflight setup.
metadata:
  openclaw:
    requires:
      bins: [python3, clawflight]
---

# clawflight

Family flight alerts from a forwarding address. Airline confirmations are parsed,
matched to the family member travelling, watched with free public feeds, and
posted to whatever chat the family already uses.

Two cron jobs do the work unattended. You are the conversational layer: turn
what the user says into one CLI verb, run it, and report the result.

## Run the CLI

`clawflight` is on PATH. Add `--json` for machine-readable output when you need
to read fields rather than show text.

```sh
clawflight status                                   # what is tracked
clawflight --json status                            # same, as JSON
clawflight follow <flight-id> --recipient <key>     # subscribe someone
clawflight unfollow <flight-id> --recipient <key>
clawflight mute <flight-id> --recipient <key>       # stop alerts for one flight
clawflight unmute <flight-id> --recipient <key>
clawflight doctor                                   # validate config, audit state
clawflight setup                                    # print the cron recipes
```

`--recipient` defaults to the configured owner. Every verb accepts `--config`
and `--state-dir`.

## Mapping what people say

| the user says | run |
|---|---|
| "what flights are tracked?" / "any flights coming up?" | `clawflight --json status` |
| "follow DL767" / "I want alerts for Robin's flight" | resolve to a flight id, then `clawflight follow <id>` |
| "mute AA1203" / "stop telling me about that flight" | `clawflight mute <id>` |
| "unmute" / "start telling me again" | `clawflight unmute <id>` |
| "is clawflight working?" / "why am I not getting alerts?" | `clawflight doctor` |
| "set up clawflight" / "add the cron jobs" | `clawflight setup`, then show the commands |

### Resolving a flight id

Ids look like `DL767-2026-07-16`, and a second booking on the same flight gets
`#<confirmation>` appended: `DL767-2026-07-16#FAKE02`.

Users say "DL767" or "Robin's flight". Run `clawflight --json status`, match on
`flight`, `traveler`, `date` or `route`, and use the row's `flight_id`.

- **One match** — act, then confirm what you did.
- **Several** — list them with date and route and ask which. Never guess: muting
  the wrong flight silently stops the alerts someone was relying on.
- **None** — say so plainly. It usually means the confirmation email has not
  been ingested yet; suggest `clawflight doctor`.

## Rules

**Never invent flight data.** Everything you report comes from a command you
just ran. If `status` is empty, the answer is "nothing is tracked yet" — not a
guess from the conversation.

**Mute and follow are per person.** Ask whose alerts to change if it is
ambiguous, and say which recipient you changed when you report back.

**Do not paste secrets.** Config holds environment-variable *names*. If a user
offers a mailbox password or an API key in chat, tell them to set it with
`openclaw config set skills.entries.clawflight.env.<VAR>` instead, and do not
repeat the value back.

**Do not edit config files directly.** Point users at
`~/.openclaw/clawflight/clawflight.json` and `clawflight doctor`, which reports
exactly what is missing.

**Report failures with their reason.** `doctor` exits non-zero and prints the
cause. Relay that, do not summarise it as "something went wrong".

## Setup, in order

1. `clawflight doctor` — see what is missing.
2. Edit `~/.openclaw/clawflight/clawflight.json` (see `docs/setup.md`):
   `owner`, `people`, `recipients`, `mailbox`.
3. Set the mailbox password:
   `openclaw config set skills.entries.clawflight.env.CLAWFLIGHT_IMAP_PASSWORD '<app-password>'`
4. `clawflight doctor` again — it must exit 0.
5. `clawflight setup` and create the two cron jobs it prints.

The jobs use command payloads, so they make no model call and cost nothing while
idle:

```sh
openclaw cron create --name clawflight-tick  --cron "*/2 * * * *" \
  --command "clawflight tick"  --session isolated --delivery none
openclaw cron create --name clawflight-sweep --cron "17 * * * *" \
  --command "clawflight sweep" --session isolated --delivery none
```

## What it alerts on

Takeoff, halfway, landing and arrival; FAA ground stops and delay programmes;
schedule changes and cancellations found in ingested email; tight and missed
connections; a trip card at the start of a travel day.

Two behaviours worth explaining when they come up:

- **Backup bookings are kept, not merged.** Two flights for one person on one
  day with different confirmation codes are treated as an intentional
  primary/backup pair. Both are tracked and flagged.
- **Gate changes need the push upgrade.** The default configuration uses only
  keyless public feeds, which have no airline-side gate data. Live gate
  information requires the opt-in AeroDataBox setup in `docs/push-upgrade.md`.

## Troubleshooting

| symptom | first move |
|---|---|
| nothing tracked | `clawflight sweep --dry-run` — is the sender in `trusted_senders`? |
| traveler shows "Unknown" | add the airline's spelling of the name to that person's `match_substrings` |
| no messages arriving | `clawflight doctor` — `openclaw` on PATH, and does the recipient have a `channel`? |
| deliveries failing | `doctor` reports outbox failures; the outbox retries with backoff, so check the channel target first |
