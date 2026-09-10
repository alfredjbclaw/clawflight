# The push upgrade (optional)

The default configuration is **polling-only**: adsb.lol, adsbdb and FAA NAS,
none of which need a key, an account, or a card. Everything in the README works
that way.

This document is about the one thing polling cannot give you: **airline-side
data**. Gate assignments, gate changes, same-day schedule changes and
cancellations exist in the airline's own system, not in an ADS-B feed. Polling
learns about them only when the airline emails you.

If that gap does not bother you, stop reading. Push mode costs a signup, a
webhook, and public ingress to a service on your machine — a real increase in
attack surface for a real but narrow gain.

## What you get

| event | polling-only | with push |
|---|---|---|
| takeoff / halfway / landing | ✅ | ✅ |
| FAA ground stops and delay programmes | ✅ | ✅ |
| schedule change, cancellation | via ingested email | ✅ real time |
| gate assignment and gate change | ❌ | ✅ |
| baggage belt | ❌ | ✅ |

## What it costs

- A RapidAPI account and an AeroDataBox subscription. The free tier is enough
  for a household: subscriptions are billed in credits, and a tracked flight
  costs a handful.
- **Public ingress to a loopback HTTP server on your machine.** This is the real
  cost, and the reason push is opt-in.

## 1. Get a key

Subscribe to AeroDataBox on RapidAPI and copy the key. Store it by name only:

```sh
openclaw config set skills.entries.clawflight.env.CLAWFLIGHT_RAPIDAPI_KEY '<key>'
```

## 2. Generate a webhook secret

The secret is the URL path. Anyone who learns it can post fabricated flight
updates into your chat, so generate a real one and never paste it anywhere
public — including into an issue report.

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
openclaw config set skills.entries.clawflight.env.CLAWFLIGHT_WEBHOOK_SECRET '<secret>'
```

The receiver compares it in constant time and answers `404` to everything else.

## 3. Expose the receiver

The receiver binds `127.0.0.1` only — always, with no option to change it. To
reach it, the vendor needs ingress you provide. Pick one:

**Cloudflare Tunnel** — no inbound firewall change, survives a dynamic IP.

```sh
cloudflared tunnel --url http://127.0.0.1:8787
```

**Tailscale Funnel** — if you already run Tailscale.

```sh
tailscale funnel 8787
```

**Your own reverse proxy** — terminate TLS and proxy to `127.0.0.1:8787`. If
your ingress preserves a path prefix, set `push.path_prefix` to match; the
receiver accepts `<prefix>/hook/<secret>` and answers `/healthz` either way.

Whatever you choose, the public URL must be **HTTPS**. The secret is in the
path, and plain HTTP leaks it to every hop.

## 4. Turn it on

```json
"push": {
  "enabled": true,
  "rapidapi_key_env": "CLAWFLIGHT_RAPIDAPI_KEY",
  "webhook_url": "https://your-tunnel.example.com/hook/<secret>",
  "webhook_secret_env": "CLAWFLIGHT_WEBHOOK_SECRET",
  "receiver_port": 8787
}
```

```sh
clawflight doctor
```

`doctor` refuses to call push healthy until the key, the secret and the URL are
all present.

## 5. Verify

```sh
curl -fsS https://your-tunnel.example.com/healthz && echo ok
```

A `200` means the vendor can reach you. The next tracked flight subscribes
automatically; `subscriptions.json` records the ids.

## Operating notes

**Credits.** The hourly sweep reconciles vendor-side subscriptions against the
ones it tracks and unsubscribes leaks — an abandoned subscription otherwise
burns credits for a flight nobody is watching. `SubscriptionClient.balance()`
reports what is left.

**Untrusted input.** Everything in a webhook body is treated as hostile:
vendor strings are clamped to 64 characters before they can reach a message,
bodies over 1 MiB are rejected with `413`, non-JSON gets `400`, and a failing
callback returns `500` so the vendor retries rather than dropping the update.

**Bad vendor data.** Airlines do sometimes publish nonsense. A revision already
in the past is suppressed and logged; a revision more than 45 minutes *earlier*
than schedule is delivered hedged rather than asserted. See
[architecture.md](architecture.md#push-updates).

**Push does not replace polling.** Both run. Polling is the floor; push adds
airline-side facts on top. If the tunnel dies, alerts keep coming.

## Turning it off

```json
"push": { "enabled": false }
```

Stop the receiver and the tunnel. Nothing else changes: polling continues, the
registry is untouched, and existing vendor subscriptions lapse.

## A note on the roadmap

A native OpenClaw TS plugin could register an HTTP route on the gateway itself,
which would remove the tunnel entirely — the gateway is already reachable. That
is the intended shape of push mode and is why this document is careful to
present the tunnel as today's workaround rather than the design.
