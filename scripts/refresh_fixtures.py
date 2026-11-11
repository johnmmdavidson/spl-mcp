#!/usr/bin/env python3
"""Regenerate the parser test fixtures from the live site.

Only **unauthenticated, public catalog** pages are captured, and each payload is
reduced to the fields the parsers actually read. Nothing personal can end up in
the repo through this path: no account page is fetched.

The script runs as an anonymous visitor: it uses a throwaway cookie jar, so it
never reads or writes your signed-in session, and the pages it captures are the
ones anyone sees. It needs no credentials and never signs in.

Record ids are found by a live search, never hardcoded: Seattle renumbered its
records once (``S30C…`` to ``S246C…``), and a hardcoded id would go stale again.

The holds and checkouts fixtures are *not* generated here -- they are
hand-written synthetic data (``tests/fixtures/holds_state.json``,
``tests/fixtures/checkouts_state.json``), because real holds are personal.

Usage:  python scripts/refresh_fixtures.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from spl_mcp.config import Config  # noqa: E402
from spl_mcp.session import SplSession  # noqa: E402
from spl_mcp.state import extract_state  # noqa: E402

FIXTURES = REPO / "tests" / "fixtures"

# Exactly the fields catalog.py / ebooks.py read. Anything else is dropped, so
# fixtures stay small and reviewable.
BRIEF_FIELDS = (
    "title",
    "subtitle",
    "authors",
    "format",
    "publicationDate",
    "edition",
    "groupKey",
    "isbns",
    "callNumber",
    "subjectHeadings",
    "description",
    "publisher",
)
AVAIL_FIELDS = (
    "status",
    "statusType",
    "bibType",
    "totalCopies",
    "availableCopies",
    "heldCopies",
    "circulationType",
    "eresourceUrl",
)
POLICY_FIELDS = ("materialType", "provider", "holdable")
MATERIAL_POLICY_FIELDS = (
    "supportsSuspendHold",
    "supportsSuspendHoldStartDateEdit",
    "requiresDuration",
)


def _pick(src: dict, fields: tuple[str, ...]) -> dict:
    return {k: src.get(k) for k in fields if k in src}


def reduce_bib(bib: dict) -> dict:
    policy = bib.get("policy") or {}
    out = {
        "id": bib.get("id"),
        "briefInfo": _pick(bib.get("briefInfo") or {}, BRIEF_FIELDS),
        "availability": _pick(bib.get("availability") or {}, AVAIL_FIELDS),
        "policy": _pick(policy, POLICY_FIELDS),
    }
    out["policy"]["materialPolicy"] = _pick(
        policy.get("materialPolicy") or {}, MATERIAL_POLICY_FIELDS
    )
    return out


def reduce_search(state: dict, keep_groups: int = 6) -> dict:
    """Keep the first few groups and only the bibs they reference.

    A handful of groups exercises every code path the parser has; carrying all
    20 just makes the fixture harder to eyeball in a diff.
    """
    cs = state["search"]["catalogSearch"]
    results = cs["results"][:keep_groups]
    wanted = {
        bid
        for entry in results
        for bid in [entry.get("representative"), *(entry.get("manifestations") or [])]
        if bid
    }
    return {
        "app": {"apiGatewayURL": (state.get("app") or {}).get("apiGatewayURL")},
        "search": {
            "catalogSearch": {
                "results": results,
                "pagination": cs.get("pagination", {}),
            }
        },
        "entities": {
            "bibs": {
                bid: reduce_bib(b)
                for bid, b in state["entities"]["bibs"].items()
                if bid in wanted
            }
        },
    }


def reduce_record(state: dict) -> dict:
    return {
        "app": {"apiGatewayURL": (state.get("app") or {}).get("apiGatewayURL")},
        "entities": {
            "bibs": {bid: reduce_bib(b) for bid, b in state["entities"]["bibs"].items()}
        },
    }


# A well-held title whose work has physical, ebook and eaudiobook editions.
SEARCH_QUERY = "dune"
EBOOK_TITLE = "Dune"


def find_ebook_bib_id(state: dict, title: str) -> str:
    """The EBOOK edition titled exactly ``title`` in the search results.

    Read from the live search, so the record id is always one the library uses
    today. Matched on title rather than result position, because the ranking
    moves. Fails loudly rather than capturing some other record.
    """
    bibs = state["entities"]["bibs"]
    for entry in state["search"]["catalogSearch"]["results"]:
        for bid in [entry.get("representative"), *(entry.get("manifestations") or [])]:
            brief = (bibs.get(bid) or {}).get("briefInfo") or {}
            if brief.get("format") == "EBOOK" and (brief.get("title") or "").casefold() == (
                title.casefold()
            ):
                return bid
    raise SystemExit(
        f"no EBOOK edition titled {title!r} in the search results for {SEARCH_QUERY!r}; "
        "pick another query in scripts/refresh_fixtures.py"
    )


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="spl-mcp-fixtures-") as tmp:
        # Anonymous: a fresh, empty cookie jar that is thrown away afterwards.
        config = Config(cookie_store=Path(tmp) / "cookies.json")
        await capture(config)


async def capture(config: Config) -> None:
    session = SplSession(config)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    try:
        # A search whose top group spans physical, ebook, and eaudiobook.
        url = config.search_url
        resp = await session.get(url, params={"searchType": "smart", "query": SEARCH_QUERY})
        search = extract_state(resp.text, url=url)
        ebook_id = find_ebook_bib_id(search, EBOOK_TITLE)
        data = reduce_search(search)
        (FIXTURES / "search_state.json").write_text(json.dumps(data, indent=2) + "\n")
        n_bibs = len(data["entities"]["bibs"])
        n_groups = len(data["search"]["catalogSearch"]["results"])
        print(f"wrote search_state.json ({n_groups} groups, {n_bibs} bibs)")

        # The ebook record, used by the Kindle-resolution tests.
        for bib_id, name in ((ebook_id, "record_ebook_state.json"),):
            url = config.record_url(bib_id)
            resp = await session.get(url)
            data = reduce_record(extract_state(resp.text, url=url))
            (FIXTURES / name).write_text(json.dumps(data, indent=2) + "\n")
            print(f"wrote {name} ({len(data['entities']['bibs'])} bibs)")
    finally:
        await session.aclose()

    print("\nFixtures refreshed. Review the diff before committing -- these files")
    print("are public catalog data only and must stay that way.")


if __name__ == "__main__":
    asyncio.run(main())
