---
name: clawflight
version: 0.2.1
description: >-
  Watch family flights and alert a group chat — takeoff, landing, delays, gate
  and schedule changes, tight connections. Use to track, follow or mute a
  flight, see upcoming flights, or set up alerts. No API key.
homepage: https://github.com/alfredjbclaw/clawflight
metadata:
  openclaw:
    requires:
      bins: [python3]
---

# clawflight

Family flight alerts from a forwarding address. Airline confirmations are parsed,
matched to the family member travelling, watched with free public feeds, and
posted to whatever chat the family already uses.

Two cron jobs do the work unattended. You are the conversational layer: turn
what the user says into one CLI verb, run it, and report the result.

## Run the CLI

The engine ships inside this skill. It is pure standard library, so there is
nothing to install — run the bundled launcher directly:

```sh
{baseDir}/clawflight status                                  # what is tracked
{baseDir}/clawflight --json status                           # same, as JSON
{baseDir}/clawflight follow <flight-id> --recipient <key>    # subscribe someone
{baseDir}/clawflight unfollow <flight-id> --recipient <key>
{baseDir}/clawflight mute <flight-id> --recipient <key>      # stop alerts
{baseDir}/clawflight unmute <flight-id> --recipient <key>
{baseDir}/clawflight doctor                                  # validate + audit
{baseDir}/clawflight setup                                   # print cron recipes
```

Setting it up and changing it, all without editing a file:

```sh
{baseDir}/clawflight person add sam --name Sam --match "sam kestrel"
{baseDir}/clawflight person list
{baseDir}/clawflight recipient add sam --name Sam --channel telegram --to "-100…"
{baseDir}/clawflight recipient list
{baseDir}/clawflight flight add DL767 --date 2026-09-12 \
    --from JFK --to LAX --depart 16:55 --arrive 20:20 --person sam
{baseDir}/clawflight flight remove DL767-2026-09-12
{baseDir}/clawflight config set owner sam
{baseDir}/clawflight config show
{baseDir}/clawflight config keys        # what is settable
```

If the user has also installed the package (`pip install
git+https://github.com/alfredjbclaw/clawflight`), a bare `clawflight` on PATH
works too and is the same program. Prefer the bundled launcher: it is always
present and always matches these instructions.

Add `--json` when you need to read fields rather than show text.
`--recipient` defaults to the configured owner. Every verb accepts `--config`
and `--state-dir`.

## Mapping what people say

| the user says | run |
|---|---|
| "what flights are tracked?" / "any flights coming up?" | `{baseDir}/clawflight --json status` |
| "follow DL767" / "I want alerts for Robin's flight" | resolve to a flight id, then `{baseDir}/clawflight follow <id>` |
| "mute AA1203" / "stop telling me about that flight" | `{baseDir}/clawflight mute <id>` |
| "unmute" / "start telling me again" | `{baseDir}/clawflight unmute <id>` |
| "is clawflight working?" / "why am I not getting alerts?" | `{baseDir}/clawflight doctor` |
| "set up clawflight" / "add the cron jobs" | `{baseDir}/clawflight setup`, then show the commands |
| "add my flight DL767 on the 12th" | `{baseDir}/clawflight flight add DL767 --date … --from … --to … --depart … --person …` |
| "stop tracking that flight" / "delete it" | `{baseDir}/clawflight flight remove <id>` — different from mute, this forgets it |
| "add my sister" / "track flights for Robin" | `{baseDir}/clawflight person add robin --name Robin --match "robin kestrel"` |
| "send alerts to our group chat" | `{baseDir}/clawflight recipient add … --channel … --to …` |
| "who gets alerts?" / "what channels" | `{baseDir}/clawflight recipient list` |

### Resolving a flight id

Ids look like `DL767-2026-07-16`, and a second booking on the same flight gets
`#<confirmation>` appended: `DL767-2026-07-16#FAKE02`.

Users say "DL767" or "Robin's flight". Run `{baseDir}/clawflight --json status`, match on
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

**Use the commands, not the config file.** `person`, `recipient`, `config set`
and `flight` all write it for you, validate the input, and refuse a credential.
Editing the JSON by hand loses its comments and skips those checks.

**`mute` and `remove` are different.** `mute` stops alerts for one recipient
and is reversible; `flight remove` forgets the flight entirely. Ask which one
they mean if it is ambiguous — silently forgetting a booking somebody is
relying on is the worst outcome here.

**A flight with no `--depart` is never watched.** The watch window opens six
hours before departure, so with no departure time nothing ever fires. The
command warns; relay that warning rather than reporting success.

**Report failures with their reason.** `doctor` exits non-zero and prints the
cause. Relay that, do not summarise it as "something went wrong".

## Setup, in order

Everything here is a command. Do not hand-edit the config file.

1. `{baseDir}/clawflight doctor` — see what is missing.
2. Add each traveller. `--match` is how an **airline** spells them, which is
   often not their display name, so ask and add every variant that appears on a
   ticket:
   `{baseDir}/clawflight person add sam --name Sam --match "sam kestrel" --match "samuel t kestrel"`
3. Set the owner — they receive flights nobody could be matched to:
   `{baseDir}/clawflight config set owner sam`
4. Add where alerts go. `--follow-all` means "every flight for everyone", which
   is usually what one family chat wants:
   `{baseDir}/clawflight recipient add family --name Family --channel telegram --to "-100…" --follow-all`
5. Optional — a mailbox, so confirmations are ingested automatically:
   `{baseDir}/clawflight config set mailbox.adapter imap`, then `mailbox.host`,
   `mailbox.username`, `mailbox.trusted_senders`, and the password by NAME:
   `openclaw config set skills.entries.clawflight.env.CLAWFLIGHT_IMAP_PASSWORD '<app-password>'`
6. `{baseDir}/clawflight doctor` again — it must exit 0.
7. `{baseDir}/clawflight setup` and create the two cron jobs it prints.

**No mailbox is needed to start.** Adding a flight by hand works immediately
and is the fastest way to show someone it works.

The jobs use command payloads, so they make no model call and cost nothing
while idle. `setup` prints them with the correct absolute path already filled
in — show what it printed rather than retyping this shape:

```sh
openclaw cron create --name clawflight-tick  --cron "*/2 * * * *" \
  --command "<path>/clawflight tick"  --session isolated --delivery none
openclaw cron create --name clawflight-sweep --cron "17 * * * *" \
  --command "<path>/clawflight sweep" --session isolated --delivery none
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
| nothing tracked | add one by hand: `flight add … --depart HH:MM`. If mail should be arriving, `sweep --dry-run` — is the sender in `trusted_senders`? |
| flight added but no alerts | it needs `--from` and `--depart`; without them the watch window never opens |
| traveler shows "Unknown" | pass `--person <key>` when adding, or add the airline's spelling to `match_substrings` |
| traveler shows "Unknown" | add the airline's spelling of the name to that person's `match_substrings` |
| no messages arriving | `{baseDir}/clawflight doctor` — `openclaw` on PATH, and does the recipient have a `channel`? |
| deliveries failing | `doctor` reports outbox failures; the outbox retries with backoff, so check the channel target first |
