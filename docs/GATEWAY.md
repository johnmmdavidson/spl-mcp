# The BiblioCommons holds gateway

How `place_hold`, `cancel_hold`, `suspend_hold`, and `reactivate_hold` talk to
BiblioCommons.

## How this contract was obtained

The build brief expected this to be reverse-engineered by placing a real hold in
a browser with devtools open and copying the request. That turned out to be
unnecessary. **The contract was recovered by reading BiblioCommons' own shipped
JavaScript bundle** (`app.main_*.js`, served from `cdn-nerf.bibliocommons.com`),
which contains the API map and the action creators in readable, if minified,
form.

That is strictly better than the capture approach:

- it is **non-mutating** — no test hold was placed on anyone's account;
- it gives the **complete** field list, including optional fields a single
  captured request would never have shown;
- it disambiguates fields a capture would have left guessable (see
  `suspended` vs `expiryDate` below).

The relevant source, de-minified:

```js
var api = {
  placeHold:  (body, http) => http.post  (http.route`/libraries/${sub}/holds`, body),
  cancelHold: (body, http) => http.delete(http.route`/libraries/${sub}/holds`, body),
  getHolds:   (q,    http) => http.get   (http.route`/libraries/${sub}/holds`, {query: q}),
  updateHolds: ({holdIds, messagingId, accountId, suspended, expiryDate, location}, http) =>
    http.patch(http.route`/libraries/${sub}/holds`,
               {holdIds, messagingId, accountId, suspended, location, expiryDate}),
}
```

## Transport

From the same bundle's HTTP client (`getFullUrl` / `_request`):

| | |
|---|---|
| Base URL | `app.apiGatewayURL` from the page state — currently `https://gateway.bibliocommons.com/v2` |
| Path | `/libraries/{librarySubdomain}/holds` — `librarySubdomain` is the library slug, e.g. `seattle` |
| Query | `locale=en-US` |
| Headers | `Accept: application/json`; `Content-Type: application/json` for bodies |
| Auth | Session cookies (`withCredentials`), **plus** `X-Session-Id` and `X-Access-Token` when the page state has them |
| Method | Everything targets the same path; the **HTTP verb** selects the operation |

`X-Session-Id` and `X-Access-Token` come from `auth.sessionId` and
`auth.authToken` in the embedded page state. They are `null` when logged out, so
this server reads them from an authenticated page fetch and sends them only when
present.

Because the base URL and tokens are read from live page state rather than
hardcoded, a change on BiblioCommons' side is picked up automatically.

## `accountId`

Every operation needs the account id for the current user **at this library**.
The bundle's selector resolves it as:

1. `auth.currentUserId` → the logged-in user id
2. `entities.users[<currentUserId>].accounts` → account ids belonging to them
3. `entities.accounts[<id>]` → pick the one matching `library.id`

Two details here are easy to get wrong, and both were caught only by running it
against a real account:

- **`users[].accounts` holds integers, while `entities.accounts` is keyed by
  strings.** Comparing them directly makes every lookup miss.
- **The library field on an account is `libId`**, not `libraryId`.

A patron can hold cards at several BiblioCommons libraries, and the page state
carries all of them. Getting this wrong sends a hold for one library using
another library's card, which the gateway answers with a bare **HTTP 500**.

Accordingly this server **never falls back to "the first account"**. If it
cannot identify exactly one account matching `library.id`, it raises and sends
nothing.

## Reading inactive holds

Cancelled and expired holds are *not* in the `/v2/holds` page state —
`borrowing.inactiveHolds` is an unpopulated lazy-load container
(`{"fetching": false, "pagination": {}}`). They need their own gateway GET, and
`accountId` is a **required query parameter**:

```
GET {api_base}/libraries/{slug}/holds?locale=en-US&accountId={id}&status=CANCELLED
GET {api_base}/libraries/{slug}/holds?locale=en-US&accountId={id}&status=EXPIRED
```

Omitting `accountId` returns `422` with
`{"error":{"fieldErrors":{"accountId":"Required"}}}`.

Note that a cancelled hold is **re-keyed**: it comes back with a synthetic id
like `-3440006629724615101-0-ephemeral`, not the `holdsId` it had while active.
Don't try to match cancelled holds by their original id.

## Operations

### Place a hold — `POST`

```json
{
  "metadataId": "S30C3420266",
  "materialType": "PHYSICAL",
  "accountId": "…",
  "enableSingleClickHolds": false,
  "materialParams": {
    "branchId": "CEN",
    "errorMessageLocale": "en-US"
  }
}
```

`materialParams` differs by material type, mirroring the two dialogs in
BiblioCommons' own UI. For a **digital** item:

```json
{
  "metadataId": "S30C2949104",
  "materialType": "DIGITAL",
  "accountId": 4000000030,
  "enableSingleClickHolds": false,
  "materialParams": { "email": "you@example.com", "format": null }
}
```

Digital titles have `requiresEmailDuringHold: true` — OverDrive emails you when
the loan is ready. The address does not need to be asked for: it is already on
the account at `entities.accounts[].digitalNotificationEmail`, which is exactly
what the library's own dialog pre-fills the field with. `format` may be null
unless the title sets `requiresFormatDuringHold`.

There is a second variant (`placeItemHold`) that adds `materialParams.itemIds`
for item-level holds. This server does not use it — SPL's physical policy has
`itemLevelHoldsPredetermined: true`, so a bib-level hold is correct.

Recovering the digital shape needed a lazily-loaded chunk rather than the main
bundle: webpack chunk 15,
`app.app-components-Workflow-WorkflowContainer_<hash>.js`, whose hash is in the
chunk map inside `app.main_*.js`.

Success looks like `{"successCount": 1, "entities": {"holds": {…}}}`, and the
new hold's `holdsPosition` is the queue position.

### Cancel a hold — `DELETE`

```json
{
  "accountId": "…",
  "holdIds": ["70000001"],
  "metadataIds": ["S30C3420266"]
}
```

Batch-capable — hence the arrays. Responses carry a `failures` array; an empty
one means every id succeeded.

### Suspend / reactivate a hold — `PATCH`

`suspended` is an **object**, not a boolean:

```json
{
  "accountId": 4000000030,
  "holdIds": ["70000009"],
  "suspended": { "status": true, "endDate": "2026-08-24" }
}
```

Reactivating sends `{"status": false}` and no `endDate`.

> **`endDate` is mandatory in practice.** A suspend sent without one is answered
> with **HTTP 200 and an empty `failures` array** — and the hold's status never
> changes. The gateway reports success for an operation it did not perform.
> This is why `suspend_hold` requires `until`, and why every mutation here
> re-reads the holds list rather than trusting the response.

> **The field a request capture would have gotten wrong.** The same `PATCH`
> endpoint also accepts a top-level `expiryDate`, which is the date the *hold
> itself* expires — not the date a pause ends. The pause end date lives at
> `suspended.endDate`. Setting `expiryDate` when you meant to schedule a
> reactivation would silently change when the hold lapses. This server never
> sends `expiryDate`.

While suspended, the hold gains a `suspendEndDate` field and its `actions`
become `["cancel", "updateSuspend", "activate"]` — note **`activate`**, not
`reactivate`. Reactivating clears `suspendEndDate` and restores
`["cancel", "suspend"]`.

Suspending also causes BiblioCommons to push the hold's own `expiryDate` out
(observed: `2027-04-30` → `2027-05-31`), which reverts on reactivation. That is
the library's behaviour, not something this server sends.

## Verification

Every mutation in this server is followed by a re-read of `/v2/holds` to confirm
the change actually landed, because a gateway `200` is not on its own proof that
the hold moved. The tool result reports what the re-read saw.

## Status

**Verified end to end against a live account** on 2026-07-24, one confirmed step
at a time, on a title with all copies checked out so no physical copy was pulled
from a shelf:

| Step | Result |
|---|---|
| `place_hold` | Hold created, queue position 17, `NOT_YET_AVAILABLE` |
| `list_holds` | Appeared as `PHYSICAL` at the requested branch |
| `suspend_hold` (no `endDate`) | **Silent no-op** — 200, no `failures`, status unchanged |
| `suspend_hold` (`endDate` set) | `SUSPENDED`, `suspendEndDate` set, position 17 kept |
| `reactivate_hold` | Back to `NOT_YET_AVAILABLE`, position 17 kept |
| `cancel_hold` | Removed from active holds; appears under `status=CANCELLED` |

A second run covered the **digital** path on an ebook with 73 holders ahead, so
no loan was triggered:

| Step | Result |
|---|---|
| `place_hold` (DIGITAL) | Hold created, queue position 75, UUID id, `actions: [cancel, suspend]` |
| `cancel_hold` (DIGITAL) | Removed from active holds |

Both test holds were cancelled; no state was left on the account.

| `suspend_hold` (DIGITAL) | `SUSPENDED`, position 75 kept, **no end date recorded** |
| `reactivate_hold` (DIGITAL) | Back to `NOT_YET_AVAILABLE`, position 75 kept |

### Scheduled resume works for physical holds only

Suspending keeps queue position for both material types. Whether the hold
*resumes on its own* does not:

| | `…StartDateEdit` | `…EndDateEdit` | Result |
|---|---|---|---|
| Physical (`BK`) | false | **true** | `endDate` stored as `suspendEndDate`; resumes automatically |
| Digital (OverDrive) | false | false | `endDate` accepted, **not recorded**; stays paused until reactivated |

The library's own UI branches on exactly these two flags:

```js
supportsSuspendHoldStartDateEdit || supportsSuspendHoldEndDateEdit
  ? openDatePickerDialog()                                   // physical
  : pauseOrResumeHolds({ suspended: {                        // digital
      startDate: null, endDate: moment().add(1,'day').format(), status: true } })
```

Two things follow. First, `endDate` is *always* sent — even on the path that
never shows the user a date — which is why a suspend omitting it silently
no-ops. Second, on digital titles the date is a formality: BiblioCommons itself
sends "tomorrow" and never surfaces it again.

So `suspend_hold` reports the date the library **recorded**, read back from the
holds list, rather than the one requested. On a digital hold it says plainly
that the requested date has no effect and the hold stays paused until
reactivated. Promising an automatic resume that will never happen is the worst
available answer for someone managing a queue.

Three defects were found and fixed in the process: the `accountId` resolution
bug described above, the mandatory `endDate`, and a missing `suspendEndDate`
field on the hold model. `read_only` still ships engaged — that is a deliberate
default for a tool an agent can call, not a statement about maturity.
