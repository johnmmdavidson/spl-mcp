"""Catalog search and record lookup.

Both are unauthenticated. BiblioCommons already groups a work's format variants
for us -- ``search.catalogSearch.results`` is a list of
``{representative, manifestations[]}`` -- so we do no grouping of our own; we
just resolve each bib id against ``entities.bibs`` and shape the result.
"""

from __future__ import annotations

from typing import Any

from .config import Config
from .errors import ParseError
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
    material_policy = policy.get("materialPolicy") or {}

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
        supports_suspend_hold=bool(material_policy.get("supportsSuspendHold", False)),
        supports_suspend_start_date_edit=bool(
            material_policy.get("supportsSuspendHoldStartDateEdit", False)
        ),
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
        raise ParseError(
            f"record page for {bib_id!r} did not contain that bib in entities.bibs "
            f"(it held: {', '.join(sorted(bibs)[:8]) or 'nothing'}). "
            "The id may be wrong or the record withdrawn.",
            url=url,
            path=f"entities.bibs.{bib_id}",
        )
    return bib
