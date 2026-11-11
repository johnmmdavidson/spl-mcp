"""Setup check: ``python -m spl_mcp.check``.

Exercises each layer in order and reports what works. It is strictly read-only
-- it never places, cancels, or suspends anything, whatever READ_ONLY is set to.
"""

from __future__ import annotations

import asyncio
import sys

from . import catalog, ebooks, holds
from .config import keychain_service, load_config
from .errors import SplMcpError
from .session import SplSession

OK = "  ok  "
FAIL = " FAIL "
SKIP = " skip "


def line(mark: str, text: str) -> None:
    print(f"[{mark}] {text}")


async def run() -> int:
    failures = 0
    config = load_config()
    session = SplSession(config)

    print("spl-mcp setup check")
    print("-" * 60)
    line("info", f"library         : {config.slug} ({config.base_url})")
    line("info", f"read_only       : {config.read_only}")
    line("info", f"credentials     : {'present' if config.credentials else 'MISSING'}")
    line("info", f"cookie store    : {config.cookie_store}")
    line("info", f"pickup branch   : {config.default_pickup_branch or '(none configured)'}")
    print("-" * 60)

    try:
        # 1. Unauthenticated search -- validates the whole parse pipeline. The
        #    ids for steps 2 and 3 come from this search, never hardcoded: the
        #    library renumbered its records once (S30C... to S246C...).
        sample_id: str | None = None
        try:
            results = await catalog.search_titles(session, config, "dune frank herbert", limit=3)
            if not results.results:
                line(FAIL, "search returned no results (expected matches for 'dune')")
                failures += 1
            else:
                top = results.results[0]
                sample_id = top.representative_bib_id
                line(
                    OK,
                    f"search: {results.total_matches} matches; "
                    f"top = {top.title!r} with {len(top.formats)} format(s)",
                )
        except Exception as exc:
            line(FAIL, f"search: {type(exc).__name__}: {exc}")
            failures += 1

        # 2. Record fetch.
        if sample_id is None:
            line(SKIP, "record fetch: no id, the search step failed")
            line(SKIP, "ebook formats: no id, the search step failed")
        else:
            try:
                bib = await catalog.fetch_record(session, config, sample_id)
                line(OK, f"record fetch: {(bib.get('briefInfo') or {}).get('title')!r}")
            except Exception as exc:
                line(FAIL, f"record fetch: {type(exc).__name__}: {exc}")
                failures += 1

        # 3. Ebook/Kindle resolution (resolves the work's ebook edition itself).
        if sample_id is None:
            pass
        elif config.use_overdrive_thunder:
            try:
                report = await ebooks.check_ebook_formats(session, config, sample_id)
                line(
                    OK,
                    f"ebook formats: kindle={report.kindle_available} "
                    f"matched_by={report.matched_by} formats={len(report.formats)}",
                )
            except Exception as exc:
                line(FAIL, f"ebook formats: {type(exc).__name__}: {exc}")
                failures += 1
        else:
            line(SKIP, "ebook formats: OverDrive lookup disabled in config")

        # 4. Authenticated holds read.
        if not config.credentials:
            if config.credentials_error:
                # Credentials exist but cannot be read: a failure, not a skip.
                line(FAIL, f"login + holds: {config.credentials_error}")
                failures += 1
            else:
                line(SKIP, "login + holds: no credentials configured")
            print()
            print("To enable the authenticated tools, add the card number and PIN to the")
            print(f"macOS Keychain (service {keychain_service()!r}, accounts 'card_number' and")
            print("'pin'), set SPL_MCP_CARD_NUMBER and SPL_MCP_PIN, or fill in")
            print("~/.config/spl-mcp/secrets.toml.")
        else:
            try:
                current = await holds.list_holds(session, config)
                line(
                    OK,
                    f"login + holds: {current.summary.total} hold(s) "
                    f"({current.summary.physical} physical, {current.summary.digital} digital)",
                )
                for hold in current.physical[:5]:
                    line(
                        "info",
                        f"  #{hold.hold_id} {str(hold.title)[:40]!r} "
                        f"{hold.status} pos={hold.queue_position} at {hold.pickup_branch}",
                    )
                branches = {
                    h.pickup_branch_code for h in current.physical if h.pickup_branch_code
                }
                if branches and not config.default_pickup_branch:
                    line(
                        "info",
                        f"  branch codes seen on your holds: {', '.join(sorted(branches))} "
                        "-- one of these is a good default_pickup_branch",
                    )
            except SplMcpError as exc:
                line(FAIL, f"login + holds: {exc}")
                failures += 1
            except Exception as exc:
                line(FAIL, f"login + holds: {type(exc).__name__}: {exc}")
                failures += 1
    finally:
        await session.aclose()

    print("-" * 60)
    if failures:
        print(f"{failures} check(s) failed.")
    else:
        print("All checks passed.")
    if config.read_only:
        print("Mutating tools are DISABLED (read_only = true).")
    else:
        print("WARNING: read_only = false -- mutating tools are live.")
    return 1 if failures else 0


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
