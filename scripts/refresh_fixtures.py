#!/usr/bin/env python3
"""Regenerate the parser test fixtures from the live site.

Only **unauthenticated, public catalog** pages are captured, and each payload is
reduced to the fields the parsers actually read. Nothing personal can end up in
the repo through this path: no account page is fetched.

The holds fixture is *not* generated here -- it is hand-written synthetic data
in ``tests/fixtures/holds_state.json``, because real holds are personal.

Usage:  python scripts/refresh_fixtures.py
"""

from __future__ import annotations

import asyncio
import json
import sys
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


async def main() -> None:
    config = Config()
    session = SplSession(config)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    try:
        # A search whose top group spans physical, ebook, and eaudiobook.
        url = config.search_url
        resp = await session.get(url, params={"searchType": "smart", "query": "dune"})
        data = reduce_search(extract_state(resp.text, url=url))
        (FIXTURES / "search_state.json").write_text(json.dumps(data, indent=2) + "\n")
        n_bibs = len(data["entities"]["bibs"])
        n_groups = len(data["search"]["catalogSearch"]["results"])
        print(f"wrote search_state.json ({n_groups} groups, {n_bibs} bibs)")

        # The ebook record, used by the Kindle-resolution tests.
        for bib_id, name in (("S30C2949104", "record_ebook_state.json"),):
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
