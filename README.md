# spl-mcp

An [MCP](https://modelcontextprotocol.io) server that lets a personal AI agent
search the **Seattle Public Library** catalog and manage library holds.

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
| `list_holds()` | yes | All holds — physical and digital |
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

Put your library card number and PIN in `secrets.toml`, or set
`SPL_MCP_CARD_NUMBER` and `SPL_MCP_PIN` in the environment. Environment
variables always win over files.

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

---

## Another library

Nothing here is Seattle-specific except the defaults. Point it at any
BiblioCommons library:

```toml
[library]
slug = "vpl"
base_url = "https://vpl.bibliocommons.com"
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
