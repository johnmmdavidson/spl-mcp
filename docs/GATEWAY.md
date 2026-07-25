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

Several operations need the account id for the current user at this library.
The bundle's selector resolves it as:

1. `auth.currentUserId` → the logged-in user id
2. `entities.users[<currentUserId>].accounts` → account ids belonging to them
3. `entities.accounts[<id>]` → pick the one matching `library.id`

This server follows the same path, and fails loudly if it cannot resolve an
account rather than sending a request without one.

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

There is a second variant (`placeItemHold`) that adds `materialParams.itemIds`
for item-level holds. This server does not use it — SPL's physical policy has
`itemLevelHoldsPredetermined: true`, so a bib-level hold is correct.

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
  "accountId": "…",
  "holdIds": ["70000001"],
  "suspended": { "status": true, "endDate": "2026-09-01" }
}
```

Reactivating sends `{"status": false}` and no `endDate`.

> **The field that a request capture would have gotten wrong.** The same `PATCH`
> endpoint also accepts a top-level `expiryDate`, which is the date the *hold
> itself* expires — not the date a pause ends. The pause end date lives at
> `suspended.endDate`. Setting `expiryDate` when you meant to schedule a
> reactivation would silently change when the hold lapses. This server never
> sends `expiryDate`.

## Verification

Every mutation in this server is followed by a re-read of `/v2/holds` to confirm
the change actually landed, because a gateway `200` is not on its own proof that
the hold moved. The tool result reports what the re-read saw.

## Status

Derived from source and implemented; **the mutating paths have not yet been
exercised against a live account.** The read paths (`getHolds`) are confirmed
working. Until a real mutation has been run once, treat `place_hold`,
`cancel_hold`, `suspend_hold`, and `reactivate_hold` as untested code — which is
also why `read_only` ships engaged.
