# clawflight design invariants

This file is the fixed contract every change in this repository is checked against.
Read it before starting work. If your change alters or adds an invariant, update this
file in the same diff.

## I1 — Pure standard library

The engine has zero runtime third-party dependencies and `pyproject.toml`
`dependencies = []` stays empty. HTTP is `urllib.request`, not `requests`. JSON is
`json`. Servers are `http.server`. This is a load-bearing property — the package is
installed by households onto machines nobody audits — not a preference. `dev` extras
may carry `pytest` and nothing else.

## I2 — No real personal data, anywhere, ever

Everything in `fixtures/`, `examples/`, `tests/` and every docstring is synthetic:
invented names, `@example.com` addresses, obviously-fake PNRs and phone numbers.
Nothing is copied from a real mailbox or from any private sibling project.
`python3 tests/pii_scan.py` and `bash tests/history_scan.sh` scan the working tree and
every blob in publishable history, and both must stay clean.

## I3 — The edges are injected; tests never touch the network or spawn processes

The core never talks to a mailbox, a chat network, or an HTTP endpoint directly. It
talks to two small interfaces: `MailboxAdapter` (yields `MailboxMessage`) and `Poster`
(one `post(text, priority="info") -> bool`). Every adapter takes its transport as an injected callable
with a default:

* `OpenClawPoster(runner=...)` — a `Runner` is `(argv, timeout) -> int`.
* any new poster — same shape: inject the opener/transport, default to the real one.

Tests assert on the **argv or request shape** the adapter builds. A test may never
spawn a process, open a socket, or reference a live hostname. CI runs the whole suite
with sockets blocked (`tests/no_network_plugin.py`). Every transport call is bounded by
an explicit timeout.

## I4 — Parsers are total functions over untrusted text

`parse_*` and the email-ingest entry points take arbitrary bytes from a mailbox.
Malformed input yields an empty result, never an exception, and never a partial write.
Bodies are size-capped before any regex runs. One malformed leg inside a message must
not discard the legs around it that parsed cleanly.

## I5 — A leg without a departure time is not trackable

`monitor.Monitor.assess` promotes `scheduled -> watch` only when
`record.leg.sched_dep_iso` parses to an epoch. Any ingestion path that produces a
`FlightLeg` is therefore responsible for populating `sched_dep_iso` whenever the source
text contains a clock time and the origin airport resolves to a timezone. A path that
hardcodes `sched_dep_iso=None` silently produces flights that are never watched.

## I6 — Unresolved data is surfaced, never silently nulled

An unmapped city, an unknown airport, or a missing timezone is a warning the user or
the log can see. It is never a silently-emitted leg with `origin=None` that looks
tracked and is not. `airport_timezone` returning `None` is a surfaced warning by
contract, not a skip.

## I7 — Alerts are rationed by phase, by hysteresis, and by `_once`

Three independent mechanisms keep the message stream quiet, and a user-visible alert
must respect all three:

1. **Phase.** Every alert is gated on the flight's phase. Nothing notifies after
   `landed`/`done`. Both the poll path (`Monitor.assess`) and the push path
   (`ingest_push`) carry the same guard — a guard in one and not the other is a defect.
2. **Hysteresis.** A repeated-value alert compares against the **last value the user was
   actually told**, never against the original schedule, and only fires when the new
   value moves materially from that. Wobble inside the floor is silence.
3. **`_once`.** Advisory/diagnostic events are bounded to one per flight via the `sent`
   marker list.

One fact produces one message. Two events describing the same change on the same tick
are collapsed before delivery.

## I8 — Alert text is about the flight, not about clawflight

User-visible message text describes what is happening to the aircraft, the gate, or the
schedule. It does not describe clawflight's own plumbing — feed freshness, push-tier
quietness, poller state. Diagnostics belong in the log or below the user-visible
threshold (`critical=False` and, where warranted, not emitted at all).

## I9 — Every documented command works from the documented install path

A command printed in `README.md` or `docs/` must succeed for a user who followed the
install instructions immediately above it. An instruction that only works from a git
clone must say so. `clawflight doctor` run after the README Quickstart, verbatim, ends
with no errors.

## I10 — State is one directory, and overriding it is coherent

Config and state resolve together. Overriding the state directory (`CLAWFLIGHT_STATE_DIR`
or `--state-dir`) without also naming a config (`CLAWFLIGHT_CONFIG`) must not split the
two across different directories. `monitor.json` is shared between the poll tick and the
push receiver in separate processes, so every read-modify-write runs under the exclusive
file lock and reloads from disk inside it.

## I11 — Secrets are referenced, never stored and never logged

No config field accepts a credential value; credentials are named by environment
variable (`password_env`, `CLAWFLIGHT_WEBHOOK_SECRET`) and read at use. A token is never
written to state, never printed by `doctor`, `status` or `config show`, and never
appears in a log line or an exception message.

## I12 — Every behaviour change carries a test that asserts the user-visible outcome

Tests assert what the user receives — the number of messages, their text, the argv or
request that carries them — not internal constants. A threshold is an implementation
detail; "this 9-value wobble produces at most 2 alerts" is the contract.
