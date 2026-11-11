# Changelog

Notable changes, and the findings behind them. Dates are when the work landed.

## 2026-11-11 — the fixture refresh follows the renumbered records (COR-376)

`scripts/refresh_fixtures.py` hardcoded the ebook record `S30C2949104`. It
still worked on 2026-11-11, because an old id's page answers 200 with the
work's current records, but only for as long as the library keeps serving old
ids. It now finds the ebook record by a live search ("dune", the EBOOK edition
titled "Dune"), matched on title rather than rank, and stops with a plain error
if that edition is missing. It also runs with a throwaway cookie jar, so it
never reads or writes the signed-in session's cookies and captures only what an
anonymous visitor sees.

The search and ebook-record fixtures are regenerated (`S246C…` ids, still only
the fields the parsers read). The hand-written holds and checkouts fixtures stay
synthetic; only their ids moved to the `S246C…` shape. The ebook tests read
their ids from the fixture by format, and the search test reads its match count
from the fixture, so the next refresh needs no test edit. The `S30C…` examples
in `docs/GATEWAY.md` and `docs/KINDLE.md` are marked as pre-renumbering.

## 2026-11-10 — the two library servers no longer clash (COR-375)

### What broke

With `spl` and `kcls` both registered, a Seattle call failed at random with
"login was rejected … Check the card number and PIN" although the PIN was
right, or with "still redirected to the login page after re-authenticating".
Run one after the other, every call worked.

### Why (probed live, 2026-11-10)

A linked Seattle + KCLS account has **one live session**. A sign-in at KCLS
ends the session Seattle's server holds (and the other way round): right after
a KCLS sign-in, Seattle's holds page redirects to the login page. Run one at a
time, that was hidden by the one silent re-login each request allows. In
parallel, a KCLS sign-in could land after Seattle's sign-in was accepted (302,
`bc_access_token` set) and before Seattle followed the redirect; the redirect
then ended on the login page, which the server read as a rejected PIN.

### The fix

- Every sign-in and every authenticated request takes one machine-wide lock,
  `~/.local/state/spl-mcp/session.lock` (`SPL_MCP_SESSION_LOCK`), shared by
  every registration whatever its library or cookie file. A sign-in can no
  longer land between another server's sign-in and its use of it. The cost:
  when calls alternate between the libraries, each switch signs in again
  (an accepted sign-in, never a failed one).
- A sign-in the library accepted whose session then ends at once (another
  device signing in) says so, "the PIN is fine", and is not retried. A wrong
  PIN is still reported once and never retried. Accepted means the POST
  answered with a redirect that sets `bc_access_token`; a rejected one
  re-renders the form with HTTP 200.


### What changed

- Every tool answer starts with a `library` field naming the library
  ("Seattle Public Library", "King County Library System"); the existing
  fields are unchanged. Every error starts with the library's name, and so does
  the health report. The server's MCP name is now `bibliocommons-library` and
  its instructions no longer say Seattle.
- `SPL_MCP_SLUG=kcls` alone now configures KCLS: its site, its OverDrive key
  (`kcls`), its Keychain service (`cornice-kcls`) and its own cookie file
  (`cookies-kcls.json`). Before, a second registration that set only the slug
  inherited Seattle's site, Seattle's OverDrive counts, Seattle's card and
  Seattle's cookie jar. Explicit settings still win; Seattle's defaults are
  unchanged. `SPL_MCP_LIBRARY_NAME` names any other library.
- A page whose only card names another library is now refused, not used.
  Before, a lone card was used whatever library it belonged to. `library.id`
  and `libId` are compared as strings.

### What the probe found at KCLS (2026-11-10)

Same software, same shapes: the login form (`name`, `user_pin`,
`authenticity_token`), the embedded page state, the record page's
`availability` block, the holds gateway (`gateway.bibliocommons.com/v2`). The
differences:

| | Seattle | KCLS |
|---|---|---|
| `library.id`, id prefix | 246, `S246C…` | 82, `S82C…` |
| OverDrive key | `spl` | `kcls` (`kingcounty` is an alias, same website id 12) |
| login page `destination` | `/user_dashboard` | `https://kcls.org` |
| login field hint | card number, PIN | "Username or Library Card Number", "Password or PIN" |
| branch codes | numeric | numeric, 58 branches incl. 24-hour lockers |
| ebooks | OverDrive | OverDrive, plus hoopla records filed as `EBOOK` with `materialType` PHYSICAL, not holdable |

The two are "connected libraries": a patron's linked accounts put both cards on
either library's pages, and each page lists only its own library's holds.

One transient: KCLS's record page for one title once reported `totalCopies: 0`
beside 20 holds, and 21 copies on every later read. The queue block reports
what the page says; it does not correct it.

## 2026-11-10 — the server starts again; Keychain login; health check; hold queue

### Why it broke

`pyproject.toml` asked for `mcp>=1.2`. `mcp` 2.0.0 shipped on 2026-07-28, the
day after the last change here, and removed `mcp.server.fastmcp`. Any fresh
install or upgrade since then resolves to 2.x and `spl-mcp` dies on import:
`ModuleNotFoundError: No module named 'mcp.server.fastmcp'`. Every tool is gone
at once. The test suite stayed green because no test imported `server.py`.

Every dependency is now capped below its next major version, and
`tests/test_server.py` imports the server and lists its tools. Under `mcp`
2.3.0 that test fails at collection with the error above.

### The library renumbered its records

Seattle's bib ids moved from `S30C…` to `S246C…` (`library.id` is now 246).
An old id's record page still answers 200, but carries the work under its new
ids and names the new one in `catalogBibs.catalogBib.id`. `get_title` and
`check_ebook_formats` now say so in one line, listing the current ids, rather
than a parse error. They do not map an old id to a new one: the page names
the work, not which edition the old id was. `check.py` takes its sample id
from a live search instead of hardcoding one.

### Hold queue, before placing

`get_title` gains a `queue` block. BiblioCommons publishes, per edition, the
number of holds (`heldCopies`) and copies (`totalCopies`): "16 holds on 16
copies" for The Road's ebook on 2026-11-10. It does **not** publish where a new
hold would land. The record page, the public availability endpoint
(`gateway…/v2/libraries/seattle/bibs/{id}/availability`, fields
`heldCopies availableCopies totalCopies onOrderCopies volumesCount status …`)
and the search page carry no position field; `holdsPosition` exists only on a
hold already placed. `position_if_placed` is therefore always null, with a note
saying so. For ebooks, OverDrive's `estimatedWaitDays` was already reported by
`check_ebook_formats`.

### Credentials, health, errors

- Card number and PIN are read from the macOS Keychain (service `cornice-spl`)
  after the env vars and before `secrets.toml`.
- `health_check` tool and `spl-mcp-health` command.
- One plain line for an unreachable library, worded in one place in the
  session. A write that failed after it may have reached the library says it
  may have taken effect. The OverDrive lookup now treats an unreachable
  OverDrive as "could not determine", not as an error.
- HTTP errors read "the library refused GET … with HTTP 503".

## 2026-07-27 — `list_checkouts` and `list_ready_to_borrow`

Two read-only tools. Tool count 8 → 10.

### `list_checkouts()`

Everything currently borrowed, physical and digital, soonest due first, from one
authenticated `GET /v2/checkedout`.

**Endpoint discovery.** `/v2/checkouts` and `/v2/borrowing/checkedout` both
answer HTTP 200 with a well-formed app state — and an empty
`entities.checkouts`. Only `/v2/checkedout` is populated. Anything that probed
for a 200 status would have settled on a URL that reports "nothing checked out"
forever.

**Three fields the request asked for do not exist**, and are not invented:

| Asked for | Reality |
|---|---|
| `checkout_date` | Not published anywhere in the payload |
| `renewals_remaining` | Not published; only `times_renewed` exists |
| `renewable` flag | Not published; derived from `actions` |

Renewability is `actions.contains("renew")` — which is precisely how
BiblioCommons' own interface decides whether to render a Renew button. This is
the third time this codebase has hit the same pattern: **`actions` is the
capability signal, and the policy booleans are not to be trusted** (see the
`supportsSuspendHold` finding in `docs/BUILD_BRIEF.md`, A4b).

**The id shape is not a discriminator here.** In the holds payload, digital ids
are UUIDs. In checkouts it is the reverse: a digital loan's `checkoutId` is a
*negative integer* and the UUID lives in a separate `digitalId` field.
`materialType` is the only reliable signal, in both payloads.

Author and format code are absent from the checkout entity and are joined from
the linked bib in `entities.bibs`, which the same page already carries.

Digital loans expose `download_url` / `read_online_url` only once a format has
been chosen; until then both are null and the loan offers `updateFormat`.

### `list_ready_to_borrow()`

Holds awaiting action, annotated with what to do. Read-only.

**`pickupByDate` is a dedicated field, and `expiryDate` is not the pickup
deadline.** The brief for this work stated the opposite — that on a
`READY_FOR_PICKUP` hold, `expiryDate` should be relabelled as the pickup
deadline. It should not. The library publishes a separate `pickupByDate`, and
its own UI renders that field only for ready holds while deliberately *hiding*
`expiryDate` in that state, because `expiryDate` keeps its meaning of "when the
hold lapses in the queue" — typically months out. Conflating them would have
reported a next-year date for a book the branch reshelves within days. Both are
returned, separately named.

`IN_TRANSIT` — observed live, a hold on its way to the branch — is excluded from
"ready", since it cannot be collected yet. It still appears in `list_holds()`.

`eresourceUrl` is reported when present but is null on SPL's OverDrive titles,
so a `record_url` is always supplied as somewhere the item can be acted on.

Digital checkout is **not** implemented: this task was read-only throughout.

### `list_holds()` gains a `ready` block

Additive only. `summary`, `physical` and `digital` are unchanged, so existing
callers keep working.

### Verification

`list_checkouts` was verified against a live account. `list_ready_to_borrow`
was **not** — the account had no `READY_FOR_PICKUP` holds at the time, so its
field mapping is derived from the frontend source and exercised against
fixtures, not observed end to end. Worth re-checking the first time a hold
actually becomes ready.
