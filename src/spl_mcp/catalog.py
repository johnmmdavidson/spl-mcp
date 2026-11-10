"""Catalog search and record lookup.

Both are unauthenticated. BiblioCommons already groups a work's format variants
for us -- ``search.catalogSearch.results`` is a list of
``{representative, manifestations[]}`` -- so we do no grouping of our own; we
just resolve each bib id against ``entities.bibs`` and shape the result.
"""

from __future__ import annotations

from typing import Any

from .config import Config
from .errors import ParseError, SplMcpError, StaleIdError
from .models import FormatOption, SearchResults, WorkGroup, format_label
from .session import SplSession
from .state import dig, extract_state

# BiblioCommons serves 20 results per page and ignores requests for more.
PAGE_SIZE = 20


def _format_option(bib_id: str, bib: dict[str, Any]) -> FormatOption:
    """Shape one bib entity into a FormatOption.

    Only ``id`` is truly required; the rest degrade to None so that one odd
    record cannot fail an entire search.
    """
    brief = bib.get("briefInfo") or {}
    avail = bib.get("availability") or {}
    policy = bib.get("policy") or {}

    code = brief.get("format")
    material_type = policy.get("materialType")

    return FormatOption(
        bib_id=bib_id,
        format=code,
        format_label=format_label(code),
        material_type=material_type,
        is_digital=(material_type == "DIGITAL") or (avail.get("bibType") == "DIGITAL"),
        provider=policy.get("provider"),
        holdable=bool(policy.get("holdable", False)),
        status=avail.get("status"),
        total_copies=avail.get("totalCopies"),
        available_copies=avail.get("availableCopies"),
        # heldCopies is the number of copies currently spoken for, i.e. the
        # queue ahead of a new hold. It is the closest thing the search payload
        # gives to "how long is the wait".
        hold_queue=avail.get("heldCopies"),
        edition=brief.get("edition"),
        publication_date=brief.get("publicationDate"),
    )


def parse_search(state: dict[str, Any], query: str, *, url: str | None = None) -> SearchResults:
    """Turn a search page's app state into SearchResults."""
    results = dig(state, "search.catalogSearch.results", url=url)
    bibs = dig(state, "entities.bibs", url=url)
    pagination = dig(state, "search.catalogSearch.pagination", url=url, default={}) or {}

    if not isinstance(results, list):
        raise ParseError(
            f"expected search.catalogSearch.results to be a list, got {type(results).__name__}",
            url=url,
            path="search.catalogSearch.results",
        )

    groups: list[WorkGroup] = []
    for entry in results:
        rep_id = entry.get("representative")
        if not rep_id:
            continue
        rep = bibs.get(rep_id)
        if rep is None:
            # The representative should always be present. If it isn't, skip
            # the group rather than inventing a title for it.
            continue
        brief = rep.get("briefInfo") or {}

        # Keep the representative first; it's the edition BiblioCommons chose
        # to collapse the group under, and the one the agent should default to.
        ids = list(dict.fromkeys([rep_id, *(entry.get("manifestations") or [])]))
        formats = [_format_option(bid, bibs[bid]) for bid in ids if bid in bibs]

        authors = list(brief.get("authors") or [])
        groups.append(
            WorkGroup(
                title=brief.get("title") or "(untitled)",
                subtitle=(brief.get("subtitle") or None),
                author=authors[0] if authors else None,
                authors=authors,
                representative_bib_id=rep_id,
                formats=formats,
            )
        )

    return SearchResults(
        query=query,
        total_matches=pagination.get("count"),
        page=pagination.get("page", 1),
        returned=len(groups),
        results=groups,
    )


async def search_titles(
    session: SplSession, config: Config, query: str, limit: int = 10
) -> SearchResults:
    """Search the catalog. Unauthenticated.

    ``limit`` trims the returned groups. BiblioCommons pages at 20, so we fetch
    as many pages as the limit needs and no more.
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("query must not be empty")
    limit = max(1, min(int(limit), 100))

    collected: list[WorkGroup] = []
    total: int | None = None
    page = 1
    seen: set[str] = set()

    while len(collected) < limit:
        params = {"searchType": "smart", "query": query}
        if page > 1:
            params["page"] = str(page)
        url = config.search_url
        resp = await session.get(url, params=params)
        parsed = parse_search(extract_state(resp.text, url=url), query, url=url)
        total = parsed.total_matches if total is None else total

        if not parsed.results:
            break
        for group in parsed.results:
            if group.representative_bib_id in seen:
                continue
            seen.add(group.representative_bib_id)
            collected.append(group)

        pages_available = (total + PAGE_SIZE - 1) // PAGE_SIZE if total else page
        if page >= pages_available:
            break
        page += 1

    return SearchResults(
        query=query,
        total_matches=total,
        page=1,
        returned=len(collected[:limit]),
        results=collected[:limit],
    )


async def fetch_record(session: SplSession, config: Config, bib_id: str) -> dict[str, Any]:
    """Fetch one bib's entity from its record page. Unauthenticated."""
    bib_id = (bib_id or "").strip()
    if not bib_id:
        raise ValueError("bib_id must not be empty")

    url = config.record_url(bib_id)
    resp = await session.get(url)
    state = extract_state(resp.text, url=url)
    bibs = dig(state, "entities.bibs", url=url)

    bib = bibs.get(bib_id)
    if bib is None:
        raise missing_bib_error(state, bibs, bib_id, url=url)
    return bib


def missing_bib_error(
    state: dict[str, Any], bibs: dict[str, Any], bib_id: str, *, url: str
) -> SplMcpError:
    """The error for a record page that does not carry the bib asked for.

    When the page names a different current record for the work
    (``catalogBibs.catalogBib.id``), the id is one the library no longer uses:
    say so and give the current ids. The old id is not mapped to a new one
    here -- the page names the work, not which edition the old id was, and a
    hold on the wrong edition is worse than a plain error.
    """
    current = dig(state, "catalogBibs.catalogBib.id", url=url, default=None)
    if current and current != bib_id and current in bibs:
        editions = dig(state, "catalogBibs.manifestations.ids", url=url, default=None) or [current]
        listed = ", ".join(
            f"{eid} ({format_label((bibs.get(eid, {}).get('briefInfo') or {}).get('format'))})"
            for eid in editions
            if eid in bibs
        )
        title = (bibs[current].get("briefInfo") or {}).get("title")
        return StaleIdError(
            f"{bib_id!r} is not a current record id at the library: its page now serves "
            f"{title!r} under different ids ({listed}). Search again and use a current id."
        )
    return ParseError(
        f"record page for {bib_id!r} did not contain that bib in entities.bibs "
        f"(it held: {', '.join(sorted(bibs)[:8]) or 'nothing'}). "
        "The id may be wrong or the record withdrawn.",
        url=url,
        path=f"entities.bibs.{bib_id}",
    )


def queue_summary(availability: dict[str, Any] | None) -> dict[str, Any]:
    """What the library publishes about the wait for a title, before placing a hold.

    BiblioCommons publishes the number of holds and copies per edition
    (``heldCopies``, ``totalCopies``), the same "N holds on M copies" its own
    page shows. It does not publish where a new hold would land: the position
    exists only once a hold is placed (``holdsPosition``, shown by list_holds).
    A field the library did not send stays None -- never 0.
    """
    avail = availability or {}
    holds = avail.get("heldCopies")
    copies = avail.get("totalCopies")
    available = avail.get("availableCopies")
    summary = None
    if holds is not None and copies is not None:
        summary = f"{holds} hold{'s' if holds != 1 else ''} on {copies} cop" + (
            "y" if copies == 1 else "ies"
        )
        if available:
            summary += f"; {available} available now"
    return {
        "holds": holds,
        "copies": copies,
        "available_copies": available,
        "on_order_copies": avail.get("onOrderCopies"),
        "summary": summary,
        "position_if_placed": None,
        "position_note": (
            "The library does not publish where a new hold would land. A new hold "
            "joins behind the existing holds; its exact position appears in "
            "list_holds once it is placed."
        ),
    }
