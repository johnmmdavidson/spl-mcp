# spl-mcp

An [MCP](https://modelcontextprotocol.io) server that lets a personal AI agent
search a BiblioCommons library's catalog and manage library holds: the
**Seattle Public Library** by default, the **King County Library System** with
one setting, and both at once as two registrations. Every answer says which
library it came from.

It is a single-user, personal project. It runs over stdio, is spawned by your
MCP client, and talks to the library the same way your browser does — plain
HTTP. There is no headless browser, which is what makes it comfortable on a
Raspberry Pi.

> **Not affiliated with the Seattle Public Library, BiblioCommons, or OverDrive.**
> See [Responsible use](#responsible-use) before running it.

---

## What it can do

| Tool | Auth | Notes |
|---|---|---|
| `search_titles(query, limit=10)` | no | Catalog search, results grouped across formats |
| `get_title(bib_id)` | no | Full detail for one catalog record |
| `check_ebook_formats(bib_id)` | no | Is the ebook available for Kindle? |
| `list_holds()` | yes | All holds — physical and digital, plus a `ready` block |
| `list_checkouts()` | yes | What's borrowed now, with due dates |
| `list_ready_to_borrow()` | yes | Holds waiting on you right now |
| `place_hold(bib_id, pickup_branch=None)` | yes | Mutating. `pickup_branch` is physical-only |
| `cancel_hold(hold_id)` | yes | Mutating |
| `suspend_hold(hold_id, until)` | yes | Mutating. `until` is required, but see below |
| `reactivate_hold(hold_id)` | yes | Mutating |

Physical and digital (ebook / eaudiobook) holds are treated identically. There
is **one** switch governing whether the agent can change anything —
`read_only` — with no material-type carve-out.

The differences that remain are real ones, not policy: a digital hold has no
pickup branch (you're emailed when the loan is ready), and its notification
address is read from your library account rather than asked for.

What any individual hold permits depends on its state, which `list_holds`
reports per hold in `actions`. A hold that's ready for pickup can be cancelled
but not suspended, and the server checks that before sending anything.

### What's out and what's waiting

`list_checkouts()` returns everything currently borrowed, soonest due first,
with a summary carrying `next_due` and an `overdue` count.

Two fields the library simply does not publish, and which this server therefore
does not invent: **there is no checkout date** and **no remaining-renewals
count**. What you get instead is `times_renewed`, plus a `renewable` flag
derived from whether the library currently offers the `renew` action on that
loan — which is exactly how the library's own interface decides whether to show
you a Renew button.

Digital loans only carry a read/download link *after* a format is chosen. Until
then `download_url` and `read_online_url` are null and the loan offers
`updateFormat`.

`list_ready_to_borrow()` returns holds you can act on now — physical ones to
collect, digital ones to borrow. It is read-only: it tells you what is ready,
it does not borrow anything.

> **`pickup_by` is not `expires`.** On a hold that is ready for collection, the
> deadline is `pickup_by`, from the library's dedicated `pickupByDate` field.
> The separate `expires` field means "when this hold lapses in the queue" and is
> typically months away. Using `expires` as a pickup deadline would tell you
> that you have until next year to collect a book the branch will reshelve in a
> week. Both are returned, clearly named.

A hold that is `IN_TRANSIT` — on its way to your branch — is deliberately *not*
listed as ready, since it cannot be collected yet. It still appears in
`list_holds()`.

### Suspending: digital holds do not resume on their own

Suspending keeps your queue position for both material types. Whether the hold
*comes back by itself* differs, and the library gives no warning about it:

| | Keeps queue position | Resumes automatically on `until` |
|---|---|---|
| **Physical** | yes | **yes** — the date is stored and honoured |
| **Digital** (OverDrive) | yes | **no** — the date is discarded |

A suspended digital hold stays paused **until something explicitly reactivates
it**. `until` is still required by the API — omit it and the library accepts the
request, returns success, and does nothing at all — but on a digital title the
date is a formality that BiblioCommons itself fills in and never uses.

So `suspend_hold` reports the date the library actually **recorded**, read back
from the holds list, not the one you asked for. On a digital hold it says so
outright, and the result carries `resumes_automatically: false`:

```json
{
  "status": "SUSPENDED",
  "queue_position": 75,
  "until": null,
  "requested_until": "2026-08-25",
  "resumes_automatically": false
}
```

**If you want a paused ebook back on a given date, something has to call
`reactivate_hold` on that date** — a scheduled agent task, a reminder, anything.
Nothing server-side will do it. Check `resumes_automatically` rather than
assuming the pause expires.

Verified against a live account; details and the underlying policy flags are in
[docs/GATEWAY.md](docs/GATEWAY.md).

### The read-only guardrail

All four mutating tools are disabled by default. With `read_only = true` (the
shipped default) they refuse at the code level and **never send a request**. Turn
it off deliberately:

```toml
# config.toml
[safety]
read_only = false
```

---

## How it works

Every BiblioCommons v2 page ships its entire app state as JSON inside a
`<script data-iso-key="_0">` tag. This server reads that JSON rather than
scraping rendered HTML, so it survives cosmetic redesigns and gets fields the
page never displays (copy counts, hold queues, per-format holdability).

- **Search** is unauthenticated and already grouped by work — BiblioCommons
  hands back `{representative, manifestations[]}`, so no grouping logic here.
- **Login** is an ordinary Rails form POST. The CSRF token is scraped from the
  login page first. Cookies persist to disk, so a session survives restarts and
  logins are rare.
- **Holds** are one authenticated `GET /v2/holds`; physical and digital arrive
  together, discriminated by `materialType`. Cancelled and expired holds need a
  separate request — see [docs/GATEWAY.md](docs/GATEWAY.md).
- **Checkouts** are one authenticated `GET /v2/checkedout`. Note the path:
  `/v2/checkouts` and `/v2/borrowing/checkedout` also answer 200 with a valid
  app state, but their `entities.checkouts` is always empty — probing for a 200
  picks a URL that silently reports zero loans forever.
- **Mutations** go to the JSON holds gateway directly, never by driving the UI.
- **Kindle availability** is resolved through OverDrive's public web API
  (`thunder.api.overdrive.com`) — the same unauthenticated endpoint
  `spl.overdrive.com` calls from a browser — joined to the catalog record by
  ISBN. See [docs/KINDLE.md](docs/KINDLE.md). Set `use_overdrive_thunder = false`
  to disable it, in which case the tool honestly reports Kindle status as
  unknown instead of guessing.

Full reasoning and the field-level reconnaissance behind all of this is in
[docs/BUILD_BRIEF.md](docs/BUILD_BRIEF.md).

---

## Install

Requires Python 3.11+. All dependencies are pure Python and have ARM64 wheels,
so this installs cleanly on a Raspberry Pi.

```bash
git clone https://github.com/johnmmdavidson/spl-mcp.git
cd spl-mcp

python3 -m venv .venv
.venv/bin/pip install -e .
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv venv && uv pip install -e .
```

### As a command, with `uv tool` (macOS or Linux)

```bash
uv tool install "git+https://github.com/johnmmdavidson/spl-mcp@<commit-sha>"
```

This puts two commands on your PATH (`~/.local/bin` by default): `spl-mcp`,
the stdio server an MCP client spawns, and `spl-mcp-health`. Pin a commit so
an install is repeatable; every dependency is capped below its next major
version, because `mcp` 2.0 removed the API this server is built on.

### On a Raspberry Pi

```bash
sudo apt update && sudo apt install -y python3-venv python3-dev
git clone https://github.com/johnmmdavidson/spl-mcp.git ~/spl-mcp
cd ~/spl-mcp && python3 -m venv .venv && .venv/bin/pip install -e .
```

No systemd unit is needed. This is a **stdio** server: your MCP client spawns it
on demand and it exits with the client. Because cookies persist to disk, the
library session outlives any individual spawn.

---

## Configure

```bash
mkdir -p ~/.config/spl-mcp
cp config.example.toml  ~/.config/spl-mcp/config.toml
cp secrets.example.toml ~/.config/spl-mcp/secrets.toml
chmod 600 ~/.config/spl-mcp/secrets.toml
```

Credentials are read from, in order:

1. `SPL_MCP_CARD_NUMBER` and `SPL_MCP_PIN` in the environment;
2. on macOS, the Keychain: generic passwords under service `cornice-spl`,
   accounts `card_number` and `pin` (set `SPL_MCP_KEYCHAIN_SERVICE` to use
   another service name, or to an empty string to skip the Keychain):

   ```bash
   security add-generic-password -s cornice-spl -a card_number -w   # prompts
   security add-generic-password -s cornice-spl -a pin -w
   ```

3. `secrets.toml`.

A Keychain that exists but cannot be read (locked, access denied) is reported
as an error, never treated as "no credentials". Without credentials the
catalog tools still work; only the account tools need a login.

**Never commit either file.** Both are in `.gitignore`.

Every config field has an env override named `SPL_MCP_<FIELD>` — for example
`SPL_MCP_READ_ONLY=false`, `SPL_MCP_DEFAULT_PICKUP_BRANCH=CEN`.

### Verify your setup

```bash
.venv/bin/python -m spl_mcp.check
```

This runs an unauthenticated search, then — if credentials are present — logs
in and lists your holds. It prints what worked and what didn't, and never
mutates anything.

For a one-screen answer to "is it working", run `spl-mcp-health` (or call the
`health_check` tool). It reports three things: the server is up, the library
is reachable, and the login works. It exits non-zero unless all three pass. A
check it could not run is shown as `skip`, never as a pass:

```
library: Seattle Public Library
[  ok  ] server: spl-mcp 0.1.0 is up for Seattle Public Library; read_only=True
[  ok  ] library: https://seattle.bibliocommons.com answered
[ FAIL ] login: login was rejected -- still on the login page afterwards. ...
status: degraded
```

---

## Register with an MCP client

The server speaks stdio. Point your client at the venv's Python:

```json
{
  "mcpServers": {
    "spl": {
      "command": "/home/pi/spl-mcp/.venv/bin/python",
      "args": ["-m", "spl_mcp.server"],
      "env": {
        "SPL_MCP_CARD_NUMBER": "…",
        "SPL_MCP_PIN": "…",
        "SPL_MCP_READ_ONLY": "true"
      }
    }
  }
}
```

Credentials can live in `~/.config/spl-mcp/secrets.toml` instead of the client
config, which keeps them out of your agent's configuration file. The installed
console script `spl-mcp` works as a `command` too.

For Claude Code specifically:

```bash
claude mcp add spl -- /home/pi/spl-mcp/.venv/bin/python -m spl_mcp.server
```

With a `uv tool` install and credentials in the Keychain, the entry needs no
secrets at all: `{"command": "/Users/<you>/.local/bin/spl-mcp"}`.

---

## Two libraries: SPL and KCLS

Run the server twice, once per library. Each registration has its own login,
its own read-only switch and its own cookie jar. Setting the slug is enough
for a library the server knows (`seattle`, `kcls`): it then uses that
library's site, its OverDrive key, its own Keychain service and its own cookie
file, never Seattle's. Anything set explicitly wins.

| | `spl` | `kcls` |
|---|---|---|
| `SPL_MCP_SLUG` | `seattle` (default) | `kcls` |
| site | `https://seattle.bibliocommons.com` | `https://kcls.bibliocommons.com` |
| OverDrive key | `spl` | `kcls` |
| Keychain service | `cornice-spl` | `cornice-kcls` |
| cookie file | `~/.local/state/spl-mcp/cookies.json` | `…/cookies-kcls.json` |
| `library` in answers | `Seattle Public Library` | `King County Library System` |

```json
{
  "mcpServers": {
    "spl":  { "command": "/Users/<you>/.local/bin/spl-mcp",
              "env": { "SPL_MCP_READ_ONLY": "true" } },
    "kcls": { "command": "/Users/<you>/.local/bin/spl-mcp",
              "env": { "SPL_MCP_SLUG": "kcls", "SPL_MCP_READ_ONLY": "true" } }
  }
}
```

Every tool answer starts with a `library` field naming the library, and every
error starts with its name. A bib id belongs to the library that returned it:
the same work has different ids at each (`S246C…` at SPL, `S82C…` at KCLS).
Pickup branch codes are numeric at both; list them from
`https://gateway.bibliocommons.com/v2/libraries/<slug>/branches`.

If your cards at the two libraries are linked ("connected libraries" in
BiblioCommons), each library's pages carry both cards. Each server acts only
on the card whose `libId` is its own library's, and refuses rather than use
the other library's card. See [docs/GATEWAY.md](docs/GATEWAY.md).

## Another library

Nothing here is Seattle-specific except the defaults. Point it at any
BiblioCommons library:

```toml
[library]
slug = "vpl"
base_url = "https://vpl.bibliocommons.com"
name = "Vancouver Public Library"   # the `library` field in every answer
```

The OverDrive/Kindle lookup needs that library's OverDrive slug
(`[ebooks] overdrive_library`), which you can find in the hostname of its
OverDrive site. Set `use_overdrive_thunder = false` if it doesn't have one.

---

## Development

```bash
uv pip install -e ".[dev]"
pytest          # parser tests run against saved fixtures — no network
ruff check .
```

Parser tests run entirely offline against redacted fixtures in
`tests/fixtures/`, so a BiblioCommons markup change fails a test rather than
silently returning junk.

To refresh fixtures from the live site:

```bash
python scripts/refresh_fixtures.py
```

That script only captures **unauthenticated** pages and strips the payload down
to the fields the parsers actually read, so no personal data lands in the repo.

---

## Responsible use

This server depends on parsing BiblioCommons' web pages. That carries
obligations:

- **It can break.** BiblioCommons ships frontend changes and the embedded JSON
  shape can move under it. The parsers fail loudly rather than returning empty
  results, but expect occasional maintenance.
- **Be polite.** This is built for one person's use. It makes low-volume,
  sequential requests, sends an honest and contactable `User-Agent`, and does
  not crawl. Please keep it that way — don't point it at a job that hammers the
  catalog.
- **Respect the terms of service.** Using it means using your own library
  account, under your library's terms. You are responsible for that.
- **Mind your PIN.** Repeated failed logins can lock a library account. The
  server does not retry a rejected login.

---

## License

MIT — see [LICENSE](LICENSE).
