# EXTRACTION-PLAN — flight-tracker → clawflight

Authoritative build spec for extracting the private family flight-tracker into a
community-shippable OpenClaw add-on. Drafted 2026-07-28 from a full recon of the
engine (`~/Projects/flight-tracker`, 212 tests green @ 781229f), the Mac wiring
(`~/.openclaw/workspace/tools/flight-tracker/`), and the OpenClaw docs bundle
(`/opt/homebrew/lib/node_modules/openclaw/docs/`). Companion: [PII-AUDIT.md](PII-AUDIT.md).

**Prime directive: fresh repo, fresh history.** The engine repo's git history
contains real personal data in committed fixtures. Files are ported by copy +
scrub; the engine repo's history never enters clawflight.

## 0. Name and packaging shape

**Name: `clawflight`.** "claw" prefix matches the ecosystem (ClawHub, clawd);
"flight" says what it does; short and unclaimed. Rejected: `openclaw-flight-tracker`
(collides with a thousand "flight tracker" apps, and `openclaw-*` reads official),
`flightclaw` (reads worse aloud). ClawHub surfaces: skill slug `@<owner>/clawflight`;
if/when a native plugin ships it follows npm scoping `@<owner>/clawflight-plugin`
(`-plugin` is an approved suffix per plugins/architecture.md).

**Shape: ClawHub SKILL carrying a pure-Python engine + CLI, with cron recipes.**
Decisive doc facts (paths cite the OpenClaw docs bundle):

- A community plugin **cannot register cron jobs programmatically** —
  `scheduleSessionTurn` is bundled-only (plugins/sdk-overview.md:348) and `cron.add`
  RPC needs `operator.admin` while external plugins get `operator.write` only
  (automation/cron-jobs.md:186, plugins/sdk-runtime.md:299-301). Either way the
  user runs a documented `openclaw cron create …` — the plugin buys nothing here.
- Gateway keyed/blob state stores are **bundled-plugin-only** (plugins/sdk-runtime.md:760)
  — we'd own our persistence anyway, and the engine already has durable JSON state.
- Cron **command payloads** run with no model call and support silent exit /
  `NO_REPLY` (automation/cron-jobs.md:126-201) — perfect for a 1-2 min poll tick
  that usually does nothing. Cost to the user: zero tokens while idle.
- `openclaw message send` is one channel-agnostic outbound CLI across Telegram,
  iMessage, WhatsApp, Discord, Slack, Signal, Matrix, Teams, Google Chat, and
  every channel plugin, with channel-prefixed targets (cli/message.md:10-44).
- Skills natively ship on ClawHub (`clawhub skill publish`), carry files via
  `{baseDir}`, and support per-skill secret injection (`skills.entries.<name>.env`)
  (tools/skills.md, tools/creating-skills.md:149-170).

So: the engine stays pure-stdlib Python (its biggest asset — 212 offline tests),
the skill teaches the agent to run `clawflight` CLI verbs (setup, status, follow,
mute), and setup creates two cron jobs with command payloads. A native TS plugin
(gateway HTTP route to receive AeroDataBox webhooks without a user-managed tunnel,
`registerService` timer, `registerCli`) is a **v2 layer**, not v1.

## 1. Repo layout (target)

```
clawflight/
├── README.md                  pitch + quickstart
├── LICENSE                    ← pending owner decision
├── EXTRACTION-PLAN.md         this file
├── PII-AUDIT.md               scrub ledger + verification gate
├── clawflight/                Python package (engine port, pure stdlib)
│   ├── __init__.py
│   ├── models.py airports.py parse.py email_ingest.py registry.py
│   ├── monitor.py status.py aerodatabox.py webhook_receiver.py
│   ├── notify.py recipients.py consent.py connections.py runner.py audit.py
│   ├── people.py              NEW — config-driven person table (replaces hardcoded PERSON_TABLE)
│   ├── config.py              NEW — load/validate clawflight.json, state-dir resolution
│   ├── cli.py                 NEW — `clawflight` entry: setup/tick/sweep/status/follow/mute/doctor
│   ├── adapters/
│   │   ├── mailbox_imap.py    NEW — stdlib imaplib forwarding-address ingestion (DEFAULT)
│   │   ├── mailbox_mbox.py    NEW — file-drop/mbox adapter (offline + CI testing)
│   │   └── channel_openclaw.py NEW — Poster via `openclaw message send` subprocess
│   └── data/airports.csv      packaged (fixes engine's fixtures-at-import dependency)
├── fixtures/                  SYNTHETIC ONLY (authored; see PII-AUDIT §1)
├── tests/                     ported 212-suite (fictionalized) + new adapter tests
├── skill/
│   └── SKILL.md               ClawHub skill: agent instructions + cron recipes
├── docs/
│   ├── setup.md               forwarding address, IMAP creds, channel targets
│   ├── push-upgrade.md        AeroDataBox opt-in: key, webhook, ingress options
│   └── architecture.md
└── examples/
    ├── clawflight.example.json
    └── people.example.json
```

## 2. What ports AS-IS from the engine

Pure logic with no personal coupling — mechanical copy, only import-path and
fixture-name changes:

| Module | Notes |
|---|---|
| `models.py` | dataclasses, AIRLINE_ICAO, callsign/ident helpers |
| `airports.py` | haversine/progress/ETA |
| `status.py` | adsb.lol / adsbdb / FAA NAS parsers + FlightAware/FR24 links — the zero-signup core |
| `monitor.py` | phase state machine, delay buckets, push ingest, poll-cadence recommendation |
| `aerodatabox.py` | normalize + SubscriptionClient (HTTP injected) — ships, but opt-in at runtime |
| `webhook_receiver.py` | loopback-only server, secret path, healthz — opt-in at runtime |
| `notify.py` | classify/compose + DeliveryOutbox (recipient fan-out, salted delivery_id, backoff) |
| `connections.py` | tight/missed-connection detection |
| `runner.py` | run_once orchestrator |
| `audit.py` | read-only health audit → becomes `clawflight doctor` |
| `email_ingest.py` | trusted-sender gate + candidate extraction (no body retention) |

Also as-is: the offline-gate harness *pattern* from `smoke_p123.py` / `sim_p3_fanout.py`
(fictionalized) — these become `tests/test_gate_push.py` / `test_gate_fanout.py`.

## 3. What gets REWRITTEN

| Private form | clawflight form |
|---|---|
| `registry.py` hardcoded `PERSON_TABLE` (real family), `_person_from_text` email rules | `people.py`: person table loaded from `people.json` / config — `[{key, display, match_substrings, match_email_localparts}]`; `Registry` takes it as a constructor arg. Empty default; documented fictional example. |
| `recipients.py` `_DEFAULT_RECIPIENTS` (jacob@telegram-topic-1501), telegram-only fields | Empty default; `Recipient.channel = {"channel": "telegram", "to": "-100…:topic:123"}` — any `openclaw message send` target (cli/message.md:32-44). |
| `consent.py` `_OWNER_KEY="jacob"` | `config.owner` field. |
| 4 LaunchAgents (tick 60s / sweep hourly / receiver KeepAlive / cloudflared) | **OpenClaw cron, command payloads:** `clawflight setup` prints (or runs with confirmation) `openclaw cron create --every 2m --command "clawflight tick" --session isolated --delivery none` + hourly `clawflight sweep`. Tick keeps its instant idle-exit (T-2h→arr+2h window), so a 2 min cadence costs nothing while no flight is live. Receiver/tunnel exist only in push mode (docs/push-upgrade.md); v2 plugin replaces them with a gateway `registerHttpRoute`. |
| `ftcommon.telegram_post*` → `tools/telegram_alert.py`, topic 1501 | `adapters/channel_openclaw.py`: `Poster`/`poster_for` implemented over `openclaw message send --channel X --target Y` subprocess; critical events map to the channel's notify semantics (silent flag was Telegram-only — adapter concern). Delivery keeps the engine outbox (durable retry/ack) — the adapter is only the send call. |
| `sweep._a_mail_scan` (gws CLI, `gws-owner-mail` profile, owner@example.com) + spark lanes | `adapters/mailbox_imap.py`: generic **forwarding-address ingestion** — user makes/forwards to a mailbox (e.g. `family-flights@gmail com` or a plus-address), clawflight polls it via stdlib `imaplib` + `email` parsing, feeds `email_ingest.py` → `parse.py` → `registry.merge` with `mail:<msg-id>` source tags. The gws path and spark calendar lane become *private adapters in Alfred's workspace* conforming to the same `MailboxAdapter` interface — referenced in docs as "bring your own adapter", not shipped. |
| `ftcommon.arrival_drive_home` (Google Routes + home addresses) | **Dropped from v1** (personal-location feature). Possible later as opt-in per-airport config. |
| State paths hardcoded to `~/.openclaw/flight-tracker/` | `config.py` resolves state dir (default `~/.openclaw/clawflight/`, overridable), creates 0600 files; secrets via `skills.entries.clawflight.env` injection or a `clawflight.env` file. |

## 4. Modes: polling-only DEFAULT, push opt-in

**Default = zero-signup polling.** adsb.lol + adsbdb + FAA NAS only — no keys, no
webhook, no tunnel, no card. The engine already supports this end-to-end (tick's
poll path predates push). Milestones (takeoff/halfway/landing), FAA ground-stop/
delay-program alerts, sched-dep-passed delay buckets, stale-data — all work.
Honestly documented limitation: airline-side gate changes and schedule changes
arrive only via ingested emails, not live push.

**Opt-in upgrade = AeroDataBox push** (RapidAPI key, free tier ~600 units/mo,
~4-8 credits per flight measured): gate changes, schedule changes, cancellations
pushed in real time. Requires public ingress to the webhook receiver — v1 documents
the user's options (cloudflared, Tailscale Funnel, any reverse proxy) in
docs/push-upgrade.md; v2 plugin makes the gateway itself the ingress via
`registerHttpRoute`. Credit top-up logic ports from sweep.

## 5. Hard EXCLUSIONS from v1

1. **Delta My Trips scraper** (`delta_mytrips.py` + browser-control driver) — OMIT
   entirely (recommendation; Jacob may prefer ship-disabled). Reasons: hard
   dependency on Playwright + a warmed managed-Chrome profile (un-shippable),
   scraping an airline site is ToS-gray for a community package, and it needs real
   legal names (`traveler-names.json`). Stays a private adapter in Alfred's wiring.
   The engine keeps no My Trips code, so there is nothing to disable — clean omit.
2. **All PII and real-data fixtures** — see PII-AUDIT.md; fixtures are authored
   synthetic, docs/specs/contracts do not port.
3. **Sendblue / iMessage-specific and Mac-specific paths** — no LaunchAgents, no
   `/usr/local/bin/spark`, no gws profiles, no `telegram_alert.py`, no cloudflared
   plist. iMessage still *works* as an alert channel — via OpenClaw's channel
   layer, not via any code in this repo.
4. **Spark calendar lane** — Jacob-machine-specific CLI; the calendar-ingestion
   *interface* survives (a `MailboxAdapter` sibling), the spark implementation
   stays private.
5. **Drive-home ETA** (Google Routes + home addresses).
6. **SWIM/SCDS** — never wired; not carried.

## 6. Config surface (end user)

`~/.openclaw/clawflight/clawflight.json` (JSON5-tolerant loader; example shipped):

```json5
{
  owner: "alex",                       // person key; gets unknown-person flights + consent T-48h
  people: [                            // attribution table (was PERSON_TABLE)
    { key: "alex", display: "Alex", match_substrings: ["alex kestrel"], match_email_localparts: ["alex.kestrel"] },
    { key: "sam",  display: "Sam",  match_substrings: ["sam kestrel"] }
  ],
  recipients: [                        // who gets alerts, on which channel
    { key: "alex", name: "Alex", active: true, follow_all: true,
      channel: { channel: "telegram", to: "-1001234:topic:42" } },
    { key: "sam", name: "Sam", active: true, auto_follow_own: true,
      channel: { channel: "whatsapp", to: "+15551234567" } }
  ],
  mailbox: {                           // forwarding-address ingestion
    adapter: "imap",                   // imap | mbox | none
    host: "imap.gmail.com", username: "family-flights@example.com",
    password_env: "CLAWFLIGHT_IMAP_PASSWORD",
    folder: "INBOX", poll_trusted_senders_only: true
  },
  push: {                              // OPT-IN AeroDataBox upgrade; absent = polling-only
    enabled: false,
    rapidapi_key_env: "CLAWFLIGHT_RAPIDAPI_KEY",
    webhook_url: "", webhook_secret_env: "CLAWFLIGHT_WEBHOOK_SECRET"
  },
  horizon_days: 3, state_dir: "~/.openclaw/clawflight"
}
```

Secrets only ever by env-var name (injected via `skills.entries.clawflight.env` or
the user's shell), never inline. Follow/mute state stays in `follows.json` as today.

## 7. Test strategy

- **Port the full suite** (19 files, 212 pytest-reported / 194 functions) with
  fictionalized names/PNRs per PII-AUDIT §3. The suite stays offline, stdlib+pytest,
  `python3 -m pytest -q` as the gate. Target: ≥212 after port (renames must not
  drop tests).
- **New adapter tests:** `people.py` config-driven attribution (the rewritten
  test_registry attribution cases become its proof), `config.py` load/validate/
  defaults, `mailbox_imap` against a stdlib-faked IMAP server + mbox fixture,
  `channel_openclaw` against a stubbed subprocess (asserts argv, never spawns),
  CLI smoke (`clawflight tick --dry-run`, `doctor`) via `tmp_path` state dirs.
- **Gate harnesses:** port smoke_p123/sim_p3_fanout patterns as hermetic pytest
  tests (push pipeline end-to-end with fixture updates; multi-recipient fan-out
  with distinct delivery_ids and independent acks).
- **PII verification gate** (PII-AUDIT §6 greps) wired as a test:
  `tests/test_no_pii.py` greps the repo for the blocklist — CI-enforceable and
  keeps future contributions clean.

## 8. Phased build plan (sol-harness driver pattern)

Each phase = items with write-scopes + validate commands; repo must be green
(`python3 -m pytest -q`) after every item. Estimated 7 phases, ~24 items.

**Phase 0 — Scaffold + synthetic fixtures** (this repo's second commit onward)
- 0.1 Package skeleton, pyproject (pytest-only dev dep), CI-less Makefile.
  *Write:* `clawflight/`, `pyproject.toml`. *Validate:* `python3 -c "import clawflight"`.
- 0.2 Author synthetic fixtures (spark_events replacement, 4 airline emails,
  keep public feed fixtures), fictional Kestrel family.
  *Write:* `fixtures/`. *Validate:* `tests/test_no_pii.py` (authored same item).
- 0.3 `airports.csv` → `clawflight/data/`, loader indirection.
  *Write:* `clawflight/data/`, `airports.py`. *Validate:* pytest subset.

**Phase 1 — Engine port, person-neutral core**
- 1.1 models/airports/status/connections + tests. 1.2 monitor + tests.
- 1.3 notify (outbox) + tests. 1.4 runner/audit + tests.
  *Write:* `clawflight/*.py`, `tests/`. *Validate:* `python3 -m pytest -q` green, count parity per file.

**Phase 2 — Config layer + attribution genericization**
- 2.1 `people.py` + `config.py` (+ examples/). 2.2 registry port with injected
  person table; attribution tests rewritten as adapter tests. 2.3 recipients port
  (generic channel dict, empty defaults); consent port (`owner` from config).
  *Validate:* pytest green; grep gate; `clawflight doctor` runs on empty config.

**Phase 3 — Ingestion**
- 3.1 email_ingest/parse port (synthetic fixtures). 3.2 `mailbox_imap` +
  `mailbox_mbox` adapters + faked-IMAP tests. 3.3 MailboxAdapter interface doc +
  private-adapter guide (gws/spark stay out).
  *Validate:* pytest; `clawflight sweep --dry-run --mailbox mbox:fixtures/inbox.mbox`.

**Phase 4 — Delivery**
- 4.1 `channel_openclaw` Poster + stubbed-subprocess tests. 4.2 routed outbox
  wiring in CLI paths; fan-out gate test (sim_p3 port).
  *Validate:* pytest; fan-out gate 7/7 equivalents.

**Phase 5 — Runtime CLI + cron**
- 5.1 `cli.py`: tick/sweep/status/follow/mute/doctor (subsumes sweep's CLI shape,
  keeps idle-exit tick). 5.2 `clawflight setup`: interactive config bootstrap +
  cron job creation (prints `openclaw cron create` commands; `--apply` runs them).
  *Validate:* CLI smoke tests; `clawflight tick --dry-run` on fixture state;
  cron recipe strings asserted in tests.

**Phase 6 — Push opt-in**
- 6.1 aerodatabox + webhook_receiver port; credit top-up from sweep genericized.
  6.2 push gate test (smoke_p123 port); docs/push-upgrade.md incl. ingress options.
  *Validate:* pytest; push gate green with `push.enabled=false` default untouched.

**Phase 7 — Packaging + docs (ClawHub)**
- 7.1 `skill/SKILL.md` (frontmatter: name `clawflight`, description <160 chars;
  `metadata.openclaw.requires.bins: [python3]`; `{baseDir}` invocation of the CLI;
  agent instructions for follow/mute/status conversational binding — this is where
  the private system's "Phase 4" lands for free, since the agent IS the
  conversational layer). 7.2 README quickstart + docs/setup.md + examples.
  7.3 Publish dry-run: `clawhub skill publish --dry-run`, PII gate, LICENSE check.
  *Validate:* `clawhub skill publish ./skill --dry-run`; `tests/test_no_pii.py`;
  manual checklist in PII-AUDIT §6.

**v2 (out of scope, noted):** TS plugin — `registerHttpRoute` (push ingress via
gateway, no tunnel), `registerService` (in-process scheduler replacing cron),
`registerCli`. Requires the plugin SDK toolchain (typebox, built dist, compat/build
metadata per plugins/sdk-setup.md:68-83) and gateway-restart install UX.

## 9. ClawHub packaging & docs requirements (from docs recon)

- Publish as a **skill**: `clawhub login` → `clawhub skill publish ./skill`
  (clawhub/cli.md:60-96). Public page `clawhub.ai/<owner>/clawflight`. Version from
  metadata or `--version`.
- Post-publish: automated security scan; release hidden until review/verification
  completes (clawhub/publishing.md:48-55). Non-clean scans force users through
  `--acknowledge-clawhub-risk` — keep the package boring: no exec-tricks, no
  network at import, secrets by env name only.
- Community listing checklist (plugins/community.md:56-63): ClawHub-published,
  **public GitHub repo**, setup/usage docs, active-maintenance signal, clear owner.
  No license requirement is stated in the docs bundle, but the public-repo
  requirement makes one necessary in practice → Jacob decision (MIT vs Apache-2.0).
- Install UX to document: `openclaw skills install @<owner>/clawflight`, set
  `skills.entries.clawflight.env`, run `clawflight setup`.
- Beta obligation for plugin authors (v2 only): watch beta tags, test window,
  Discord plugin-forum thread (plugins/building-plugins.md:348-361).

## 10. Open decisions for Jacob

1. **Name** — `clawflight` (recommended) or alternative.
2. ~~**License**~~ — **DECIDED: MIT.** Copyright is held as "the clawflight
   authors" rather than a legal name, so no personal name enters the public
   repository. Substitute a legal name if you would rather hold it personally.
3. **Delta My Trips** — omit from v1 entirely (recommended) vs ship-disabled stub.
4. ~~**Publish owner**~~ — **DECIDED: both handles**, `@alfredjbclaw` and
   `@helooo789`. Neither ClawHub account is logged in yet (`clawhub login`), so
   the slug is not reserved.
5. **Fictional-family naming** in fixtures ("Kestrel" placeholder — any preference).
6. **v2 plugin commitment** — whether the TS push-ingress plugin is on the roadmap
   (affects how loudly docs promise push mode ergonomics).
