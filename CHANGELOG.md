# Changelog

Notable changes, and the findings behind them. Dates are when the work landed.

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
