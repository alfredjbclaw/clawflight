# fixtures

**Every file here is synthetic.** The people, addresses, confirmation codes,
bookings, seats and itineraries are invented for the test suite. Confirmation
codes deliberately use an obviously-fake `FAKEnn` form so they can never be
mistaken for a real PNR, and all addresses use RFC 2606 reserved domains
(`example.com`, `*.example`, `*.test`).

`tests/test_no_pii.py` enforces this: it greps the working tree for real-data
patterns and fails the build on a hit. Never paste a real confirmation email
into this directory — author a fixture instead.

## The fictional Kestrel family

| key     | display | notes                                                |
|---------|---------|------------------------------------------------------|
| `alex`  | Alex    | owner; appears as `ALEX KESTREL` and `ALEXANDRA MORGAN KESTREL` (name-variant case) |
| `sam`   | Sam     | appears as `SAM KESTREL` and `SAMUEL T KESTREL`      |
| `robin` | Robin   | attributed only by a possessive calendar title       |

`TAYLOR RIVERS` is a deliberate non-family traveler: flights naming them must
attribute to `unknown`.

## Files

| file | shape it covers |
|------|-----------------|
| `calendar_events.txt` | the indented calendar export: multi-leg trips, a second calendar copy of one leg, a schedule-change notice, a standalone cancellation, an award-receipt block with fare-rule boilerplate, a primary/backup same-day pair, an attendee-only attribution, a possessive title with window-only times, and non-flight events |
| `email_delta_receipt.txt` | receipt layout: `** Confirmation Number **`, day headers, a duplicated flight line to prove dedup, a separate seats block |
| `email_aa_trip_confirmation.txt` | trip-confirmation layout: record locator, greeting line, per-day airport/time blocks |
| `email_delta_schedule_change.txt` | change notice: only the *new* itinerary is parsed, never the original |
| `email_aa_current_flight.txt` | a status/offer email that must yield nothing |
| `inbox.mbox` | mailbox adapter input: one labelled single-leg booking, one labelled two-leg booking, one marketing message, one lookalike-subdomain spoof |
| `people.json` | the attribution table the fixtures are written against |
| `adsb_airborne.json` | adsb.lol position payload, including a `"ground"` altitude and a non-matching aircraft |
| `adsbdb_route.json` | adsbdb route lookup |
| `faa_nas.xml` | FAA NAS: ground delay, arrival/departure delay, closures, ground stop |
| `adb_notification.json` | AeroDataBox webhook: gate data, a revision, and a cancellation |
