# Setup

No API key, no credit card. You need Python 3.9+ and a working OpenClaw
install. A forwarding mailbox is **optional** — it automates ingestion, but you
can add flights by hand and be running in a minute.

## 1. Install

As an OpenClaw skill, which carries the engine and needs nothing else:

```sh
openclaw skills install @alfredjbclaw/clawflight
```

Or as a package (not on PyPI yet, so from source):

```sh
pip install git+https://github.com/alfredjbclaw/clawflight
clawflight --version
```

From a checkout: `pip install -e .`

## 2. Optional: a forwarding address

clawflight reads airline confirmations. It never reads the rest of your mail.
The cleanest arrangement is a mailbox that only ever receives them:

- **A dedicated address** — `family-flights@example.com`. Everyone in the
  family forwards their confirmations there.
- **A plus-address on an existing account** — `you+flights@example.com`, with a
  filter that files anything matching an airline sender into its own label, and
  clawflight pointed at that label as its `folder`.
- **A local file drop** — no server at all. Save `.eml` files into a folder and
  set `"adapter": "mbox"` with `"path"` pointing at it.

Whichever you choose, clawflight only ever *reads*, and only messages from
senders you list.

## 3. Configure it

Every setting is a command — you never have to open the JSON file, and the
commands validate what you give them and refuse to store a credential.

```sh
clawflight person add sam --name Sam --match "sam kestrel"
clawflight config set owner sam
clawflight recipient add family --name Family \
    --channel telegram --to "-1001234567890" --follow-all
clawflight config keys      # everything that is settable
```

Prefer a file? `examples/clawflight.example.json` is a fully commented one;
copy it to `~/.openclaw/clawflight/clawflight.json`. Note that the commands
rewrite it as plain JSON, so hand-written comments are lost on the first
`config set`.

Four things to fill in.

### people — who is being tracked

```json
"people": [
  { "key": "alex", "display": "Alex",
    "match_substrings": ["alex kestrel", "alexandra morgan kestrel"],
    "match_email_localparts": ["alex", "alex.kestrel"] }
]
```

List **every spelling an airline might print**. Airlines are inconsistent about
middle names and initials, and a name that matches nothing is attributed to
`unknown` rather than to the wrong person — which is the safe failure, but it
also means that flight reaches only `follow_all` recipients.

Attendee local-parts must match exactly once `. - _ +` are stripped, so
`a.kestrel` also matches `akestrel@` but never `alexis@`.

### recipients — who gets told, and where

```json
"recipients": [
  { "key": "alex", "name": "Alex", "follow_all": true,
    "channel": { "channel": "telegram", "to": "-1001234567890:topic:42" } }
]
```

`channel.to` is whatever `openclaw message send --channel X --target Y` accepts,
so every OpenClaw channel works. Check yours first:

```sh
openclaw message send --channel telegram --target '-1001234567890:topic:42' \
  --message 'clawflight test'
```

- `follow_all: true` — every flight for everybody. Good for one household chat.
- `auto_follow_own: true` (the default) — your own flights only.
- Anything else is opt-in per flight: `clawflight follow DL767-2026-07-16`.

**The default recipient list is empty.** A fresh install notifies nobody until
you fill this in, and `clawflight doctor` reports that as an error.

### mailbox — where confirmations arrive

```json
"mailbox": {
  "adapter": "imap",
  "host": "imap.gmail.com",
  "username": "family-flights@example.com",
  "password_env": "CLAWFLIGHT_IMAP_PASSWORD",
  "folder": "INBOX",
  "trusted_senders": ["delta.com", "aa.com", "united.com"]
}
```

`trusted_senders` holds exact addresses and base domains. A base domain matches
itself and true subdomains only: `delta.com` matches `notify.delta.com` and
never `delta.com.phish.example`. **Nothing from any other sender is parsed at
all** — this is the whole trust boundary, so keep the list short.

### owner

The person key that receives flights nobody could be matched to. Must be one of
your `people`.

## 4. Supply the mailbox password

The config file holds the **name** of an environment variable, never a value.
Use an app-specific password, not your account password.

Per-skill injection (recommended — the value lives in OpenClaw's config, not in
your shell history):

```sh
openclaw config set skills.entries.clawflight.env.CLAWFLIGHT_IMAP_PASSWORD '<app-password>'
```

Or a file only you can read:

```sh
umask 077
printf 'CLAWFLIGHT_IMAP_PASSWORD=%s\n' '<app-password>' > ~/.openclaw/clawflight/clawflight.env
```

## 5. Check it

```sh
clawflight doctor
```

`doctor` validates the config, audits any state, and confirms `openclaw` is on
`PATH`. It exits non-zero while anything is still wrong and prints exactly what.
Nothing it does touches the network or writes to state.

## 6. Schedule it

```sh
clawflight setup
```

prints the two cron jobs to create:

```sh
openclaw cron create --name clawflight-tick  --cron "*/2 * * * *" \
  --command "clawflight tick"  --session isolated --delivery none
openclaw cron create --name clawflight-sweep --cron "17 * * * *" \
  --command "clawflight sweep" --session isolated --delivery none
```

Run them yourself, or `clawflight setup --apply` to run them for you (it
refuses while the config still has errors).

These are **command payloads**: no model call, so an idle tick costs zero
tokens. And `tick` exits immediately when no flight is within six hours of
departure, so a two-minute cadence is free while nobody is flying.

| job | cadence | does |
|---|---|---|
| `tick` | every 2 min | poll watched flights, emit events, deliver |
| `sweep` | hourly | ingest mail, promote landed flights, prune, retry failed deliveries |

## 7. Add a flight without any email

The fastest way to see it work — no mailbox required:

```sh
clawflight flight add DL767 --date 2026-09-12 \
    --from JFK --to LAX --depart 16:55 --arrive 20:20 --person sam
```

`--from` and `--depart` matter: the watch window opens six hours before
departure, so a flight with no departure time is never watched at all. The
command warns you if you leave it out.

`clawflight flight remove <id>` forgets a flight. That is different from
`mute`, which only silences it for one recipient and can be undone.

## 8. Use it

```sh
clawflight status                                  # what is being tracked
clawflight follow DL767-2026-09-12 --recipient sam # sam wants this one too
clawflight mute  DL767-2026-09-12                  # stop hearing about it
clawflight unmute DL767-2026-09-12
```

Because clawflight is a skill, your agent drives these verbs for you: "follow
Robin's flight", "mute DL767", "what's tracked?" all work in chat.

## What you get by default

Takeoff, halfway, landing, arrival; FAA ground stops and delay programmes at
either airport; schedule changes and cancellations found in ingested email;
tight and missed connections; a trip card at the start of each travel day.

**Honest limitation:** airline-side gate changes and same-day schedule changes
reach you only when the airline emails them. Live gate data needs the opt-in
push upgrade — see [push-upgrade.md](push-upgrade.md).

## Troubleshooting

| symptom | check |
|---|---|
| nothing is tracked | add one by hand: `clawflight flight add … --from … --depart …` |
| a flight is tracked but silent | it needs `--from` and `--depart`; without a departure time the watch window never opens |
| nothing is ingested from mail | `clawflight sweep --dry-run` — is the sender in `trusted_senders`? |
| flights say "Unknown" | pass `--person <key>` when adding, or add the airline's spelling to `match_substrings` |
| no messages arrive | `clawflight doctor` — is `openclaw` on `PATH`, and does the recipient have a `channel`? |
| deliveries keep failing | `clawflight doctor` reports outbox failures; the outbox retries with exponential backoff |
| a flight will not stop alerting | `clawflight mute <flight-id>` |

State lives in `~/.openclaw/clawflight/` as `0600` files in a `0700` directory.
Deleting `monitor.json` resets what has already been announced; deleting
`registry.json` forgets every flight.
