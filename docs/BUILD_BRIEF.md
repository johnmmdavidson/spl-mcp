# SPL MCP Server — Build Brief

**Author:** Architecture discovery + live spike, 2026-07-25.
**Status:** Built. See the [Addendum](#addendum-what-the-build-found) for where
the build's findings differ from this brief — several assumptions here turned
out to be wrong or unnecessarily pessimistic, and the code follows the findings,
not this document.

---

## 1. What this is

An MCP server that gives a personal "librarian" AI agent the ability to search
the Seattle Public Library catalog and manage holds. Single user, personal,
home-network only. The agent (an MCP client) runs on the same Pi and spawns this
server over stdio.

Capabilities:

- `search_titles(query)` — search the catalog, results grouped across formats.
- `check_ebook_formats(title_id)` — for ebook results, confirm Kindle
  compatibility.
- `list_holds()` — all current holds: physical (full detail) + digital
  (ebook/audiobook, status only).
- `place_hold` / `cancel_hold` / `suspend_hold` / `reactivate_hold` —
  **physical items only** (product decision).

Non-goals: multi-user, public exposure, placing/cancelling digital holds,
anything requiring the OverDrive partner API.

---

## 2. Architecture

| Concern | Decision | Why |
|---|---|---|
| Language | **Python 3.11+** | Mature MCP SDK; great HTTP/parse libs; fine on ARM |
| MCP | Official **`mcp`** Python SDK, `FastMCP`, **stdio** transport | Server is co-located with the agent and spawned by it |
| HTTP | **`httpx`** with a persistent cookie jar | Everything is plain HTTP |
| Browser | **NONE. No Playwright, no headless Chromium.** | Validated unnecessary. Removes the single biggest Pi/ARM risk |
| HTML parse | Extract embedded JSON from a `<script>` tag, then `json.loads` — **do not scrape the DOM** | The page ships its full app state as JSON |
| Auth | Rails form login → session cookie, replayed via httpx | No CAPTCHA / bot-wall present |
| Session | Persist cookies to disk; reuse across calls; re-login on expiry | Survives server restarts |
| Secrets | Local file on the Pi, never in repo | |
| Concurrency | One `asyncio.Lock` around authenticated calls | A shared session isn't concurrency-safe |

The headline: **the entire server is one HTTP client.**

---

## 3. Spike findings

Verified against live `seattle.bibliocommons.com` on 2026-07-25.
BiblioCommons app version `9.36.0`.

### 3.1 Search / detail pages carry full app state as JSON
Every search and record page embeds a `<script data-iso-key="_0">` node
containing the entire Redux-style app state (~443 KB on a search page).

**Gotcha:** there are **two** elements with `data-iso-key="_0"` — a `<div>`
(rendered HTML) and a `<script>` (the JSON). **Select the `<script>` one.**

### 3.2 Search results are pre-grouped across formats
`search.catalogSearch.results` is an array of:
```
{ "representative": "<bibId>", "manifestations": ["<bibId>", ...] }
```
**Format grouping is native in the data. Do not build grouping logic.**

### 3.3 Each bib resolves to rich per-format data
`entities.bibs[<bibId>]` carries `briefInfo`, `availability`, and `policy`
(including `holdable` and the suspend-support flags).

Format codes observed: `BK`, `BOOK_CD`, `LPRINT`, `EBOOK`, `EAUDIOBOOK`. Treat
as an open enum — map known ones, pass through unknown codes.

### 3.4 Auth model — plain Rails login, no bot protection
Login form fields: `name` (card number), `user_pin`, `authenticity_token`
(scraped from the login page GET), `remember_me`, `utf8`, `local`, `commit`.

**No CAPTCHA, no DataDome, no Cloudflare challenge, no PerimeterX.**

### 3.5 Holds page — one authenticated fetch gets physical + digital
`GET /v2/holds` → `entities.holds`, keyed by `holdsId`. Uniform schema,
discriminated by `materialType`. Numeric id = physical, UUID = digital.

### 3.6 Hold actions are state-dependent
Read the `actions` array per hold rather than assuming.

### 3.7 The mutation API endpoint (holds gateway)
`https://gateway.bibliocommons.com/v2/libraries/seattle/holds?locale=en-US`.
The brief instructed capturing the real request by placing a hold in a browser
with devtools open. **This proved unnecessary — see the addendum.**

### 3.8 Lesson from the spike: use the API, not the UI
A programmatic click on "Place hold" threw a client-side error and never sent
the POST. **Do not automate the UI.**

---

## 4. Guardrails

- A `READ_ONLY` flag (default **on**) that **hard-disables all four mutating
  tools** at the code level — they return a "read-only mode" error without
  hitting the network.
- Do **not** place, cancel, or suspend a real hold during dev without explicit
  confirmation each time.
- When testing mutations, use a popular, all-copies-out title so no physical
  copy is pulled from a shelf, and cancel immediately after.

**Note on digital scope:** BiblioCommons exposes `cancel` on digital holds, so
digital mutation is *technically* possible. **The product decision is to keep
digital strictly read-only.** It's a choice, not a technical limit.

---

## 5. Risks

- **Frontend drift.** Parse defensively, validate, and **fail loudly** on
  missing expected fields rather than returning empty/partial results.
- **Session expiry / future CAPTCHA.** Design the re-login path and surface auth
  failures clearly.
- **Concurrency.** Serialize authenticated calls behind one async lock.
- **Politeness.** Don't hammer the site.

---

# Addendum: what the build found

Everything in §3 above was re-verified live during the build and held, with the
exceptions and additions below. Where this addendum and the brief disagree, the
addendum is what the code does.

## A1. There is no `data` wrapper in the app state

The brief refers to `data.search.catalogSearch.results` and
`data.entities.bibs`. There is no `data` key — `search`, `entities`,
`borrowing`, `app`, and `auth` sit at the **top level** of the parsed JSON.

## A2. The mutation contract was recovered from source, not by capturing a request

§3.7 called for placing a real hold in a browser to learn the gateway contract.
That was avoided entirely: **BiblioCommons' shipped JavaScript bundle contains
the API map and action creators**, which give the complete contract for place,
cancel, suspend, and reactivate — including optional fields no single captured
request would have revealed.

This mattered for correctness, not just convenience. A capture would very
plausibly have gotten `suspend` wrong: the `PATCH` endpoint accepts both
`suspended: {status, endDate}` and a top-level `expiryDate`, and the latter
changes when the *hold itself lapses*, not when a pause ends. See
[GATEWAY.md](GATEWAY.md).

No test hold was placed on anyone's account.

## A3. `check_ebook_formats` is unauthenticated, and BiblioCommons cannot answer it

§7 of the original brief assumed this capability needed login and might be
resolvable by following `availability.eresourceUrl` into OverDrive. Both
assumptions were wrong:

- `eresourceUrl` is **null** on every digital bib checked, including on record
  pages. There is no link to follow.
- The record page's embedded JSON contains no per-title format or Kindle flag.
  The only "Kindle" strings in it belong to the UI's localisation dictionary.

The answer comes instead from OverDrive's public web API
(`thunder.api.overdrive.com`), joined to the catalog record by ISBN. It needs no
authentication at all. See [KINDLE.md](KINDLE.md).

## A4. Format codes drift faster than the brief suggested

`GRAPHIC_NOVEL_DOWNLOAD`, `MUSIC_CD`, `DVD`, and `GRAPHIC_NOVEL` all appear in
live results beyond the five codes §3.3 lists. The open-enum handling is load
bearing, and is pinned by a test.

## A5. Search pagination

`search.catalogSearch.pagination` is `{count, limit, page, pages, pageForSolr}`
with a fixed `limit` of 20. `limit` values above 20 in `search_titles` are
served by fetching successive pages.

## A6. Status of the mutating paths

The gateway contract is derived and implemented, but **has not been exercised
against a live account**. The read paths are confirmed working end to end. Until
one real mutation has been run, treat the four mutating tools as untested code.
This is the main reason `read_only` ships engaged.
