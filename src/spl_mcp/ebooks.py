"""Ebook format lookup -- chiefly: can I read this on a Kindle?

BiblioCommons does not carry this. Its record pages know a title is an ``EBOOK``
from ``OverDriveAPI``, but they expose no per-format list and (as of the last
check) leave ``availability.eresourceUrl`` null, so there is no link to follow
into OverDrive either.

The answer lives on OverDrive's own public web API, ``thunder.api.overdrive.com``
-- the unauthenticated endpoint the library's OverDrive site calls from a
browser. It returns a ``formats[]`` list per title, and ``ebook-kindle`` in that
list is exactly the "Read now with Kindle" affordance.

Joining the two catalogs: BiblioCommons' ``briefInfo.isbns`` holds the ISBN that
OverDrive records against its EPUB format, and a Thunder search for that ISBN
returns the single matching title. When no ISBN matches we fall back to a
title/author search narrowed by media type and, if needed, by copy counts --
then give up honestly rather than guess. Both catalogs' copy and hold counts are
returned as corroboration, so a bad join is visible instead of silent.
"""

from __future__ import annotations

import logging
from typing import Any

from .catalog import missing_bib_error
from .config import Config
from .errors import UnreachableError
from .models import EbookFormat, EbookFormatReport
from .session import SplSession
from .state import dig, extract_state

log = logging.getLogger("spl_mcp.ebooks")

THUNDER_BASE = "https://thunder.api.overdrive.com/v2/libraries"

OVERDRIVE_FORMAT_LABELS: dict[str, str] = {
    "ebook-kindle": "Kindle",
    "ebook-overdrive": "OverDrive Read (in browser)",
    "ebook-epub-adobe": "Adobe EPUB",
    "ebook-epub-open": "Open EPUB",
    "ebook-pdf-adobe": "Adobe PDF",
    "ebook-pdf-open": "Open PDF",
    "ebook-kobo": "Kobo",
    "audiobook-overdrive": "OverDrive Listen (in browser)",
    "audiobook-mp3": "MP3 audiobook",
    "magazine-overdrive": "Magazine (in browser)",
}

KINDLE_FORMAT_ID = "ebook-kindle"

# BiblioCommons format code -> OverDrive media type, used to tell an ebook
# edition apart from the audiobook of the same title.
MEDIA_TYPE_BY_FORMAT: dict[str, str] = {
    "EBOOK": "ebook",
    "EAUDIOBOOK": "audiobook",
    "EVIDEO": "video",
    "EMAGAZINE": "magazine",
}


def _label(format_id: str) -> str:
    return OVERDRIVE_FORMAT_LABELS.get(format_id, format_id)


def _normalise_isbn(raw: str) -> str:
    """BiblioCommons ISBNs sometimes carry a qualifier, e.g. '9780441... (pbk.)'."""
    return raw.split()[0].replace("-", "").strip() if raw else ""


def _author_surname(author: str | None) -> str:
    """'Herbert, Frank' -> 'herbert'. Used only to sanity-check a fuzzy match."""
    if not author:
        return ""
    return author.split(",")[0].strip().lower()


async def _thunder_get(
    session: SplSession, config: Config, path: str, params: dict | None = None
) -> Any:
    url = f"{THUNDER_BASE}/{config.overdrive_library}{path}"
    client = await session.client()
    try:
        resp = await client.get(url, params=params, headers={"Accept": "application/json"})
    except UnreachableError as exc:
        # Best-effort lookup: unreachable reads as "could not determine" (null),
        # never as "not offered".
        log.warning("thunder %s unreachable: %s", path, exc)
        return None
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        log.warning("thunder %s returned HTTP %s", path, resp.status_code)
        return None
    try:
        return resp.json()
    except ValueError:
        log.warning("thunder %s returned non-JSON", path)
        return None


def _select_ebook_bib(
    state: dict[str, Any], bib_id: str, *, url: str
) -> tuple[str, dict[str, Any], str | None]:
    """Return the bib to look up, preferring the work's *ebook* edition.

    The question this tool answers is "can I read this on a Kindle", so given a
    physical or audiobook edition we resolve to the ebook edition of the same
    work and say so. Siblings are matched on ``groupKey``, so unrelated records
    on the page (recommendations and the like) can't be picked up.
    """
    bibs = dig(state, "entities.bibs", url=url)
    target = bibs.get(bib_id)
    if target is None:
        raise missing_bib_error(state, bibs, bib_id, url=url)

    target_brief = target.get("briefInfo") or {}
    if target_brief.get("format") == "EBOOK":
        return bib_id, target, None

    group_key = target_brief.get("groupKey")
    if group_key:
        for other_id, other in bibs.items():
            brief = other.get("briefInfo") or {}
            policy = other.get("policy") or {}
            if (
                other_id != bib_id
                and brief.get("groupKey") == group_key
                and brief.get("format") == "EBOOK"
                and policy.get("materialType") == "DIGITAL"
            ):
                kind = target_brief.get("format") or "non-ebook"
                return (
                    other_id,
                    other,
                    f"{bib_id} is the {kind} edition; "
                    f"reporting the ebook edition of the same work ({other_id}) instead.",
                )

    return bib_id, target, None


def _confident_match(
    items: list[dict],
    *,
    title: str,
    author: str | None,
    media_type: str | None,
    total_copies: int | None,
) -> tuple[dict | None, str | None]:
    """Narrow candidates to exactly one, or explain why we couldn't.

    Applied as a ladder: exact title, then author, then media type, then copy
    count. Each rung only runs if the previous left more than one candidate.
    """
    wanted_title = title.strip().lower()
    candidates = [i for i in items if (i.get("title") or "").strip().lower() == wanted_title]
    if not candidates:
        return None, "no OverDrive title matched on exact title"

    surname = _author_surname(author)
    if surname and len(candidates) > 1:
        by_author = [
            i for i in candidates if surname in (i.get("firstCreatorName") or "").lower()
        ]
        candidates = by_author or candidates

    if media_type and len(candidates) > 1:
        by_type = [i for i in candidates if (i.get("type") or {}).get("id") == media_type]
        candidates = by_type or candidates

    # Last resort: OverDrive and BiblioCommons report the same holdings for the
    # same record, so an exact copy-count agreement identifies the edition.
    if total_copies is not None and len(candidates) > 1:
        by_copies = [i for i in candidates if i.get("ownedCopies") == total_copies]
        if len(by_copies) == 1:
            return by_copies[0], "title+author+copies"

    if len(candidates) == 1:
        return candidates[0], "title+author" + (f"+{media_type}" if media_type else "")
    return None, f"ambiguous: {len(candidates)} OverDrive titles matched"


async def _find_overdrive_title(
    session: SplSession,
    config: Config,
    brief: dict[str, Any],
    availability: dict[str, Any],
) -> tuple[dict | None, str | None]:
    """Locate the OverDrive title for a BiblioCommons bib. Returns (item, how)."""
    title = brief.get("title") or ""
    authors = brief.get("authors") or []
    author = authors[0] if authors else None
    media_type = MEDIA_TYPE_BY_FORMAT.get(brief.get("format") or "")
    total_copies = availability.get("totalCopies")

    # 1. ISBN is exact. Prefer it.
    for raw in brief.get("isbns") or []:
        isbn = _normalise_isbn(raw)
        if not isbn:
            continue
        data = await _thunder_get(session, config, "/media", {"query": isbn, "perPage": 5})
        items = (data or {}).get("items") or []
        if len(items) == 1:
            return items[0], f"isbn:{isbn}"
        if len(items) > 1:
            match, _ = _confident_match(
                items,
                title=title,
                author=author,
                media_type=media_type,
                total_copies=total_copies,
            )
            if match is not None:
                return match, f"isbn:{isbn}+title"

    # 2. Fall back to title + author. Ask for a wide page: OverDrive interleaves
    #    ebooks and audiobooks, and the edition we want may be well down the list.
    if title:
        query = f"{title} {author}" if author else title
        data = await _thunder_get(session, config, "/media", {"query": query, "perPage": 50})
        items = (data or {}).get("items") or []
        return _confident_match(
            items,
            title=title,
            author=author,
            media_type=media_type,
            total_copies=total_copies,
        )

    return None, None


def _formats_from_item(item: dict[str, Any]) -> list[EbookFormat]:
    out: list[EbookFormat] = []
    for fmt in item.get("formats") or []:
        fid = fmt.get("id")
        if not fid:
            continue
        identifiers = fmt.get("identifiers") or []
        ident = identifiers[0] if identifiers else {}
        out.append(
            EbookFormat(
                id=fid,
                label=_label(fid),
                identifier_type=ident.get("type"),
                identifier=ident.get("value") or fmt.get("isbn"),
            )
        )
    return out


async def check_ebook_formats(
    session: SplSession, config: Config, bib_id: str
) -> EbookFormatReport:
    """Report the digital formats a title is offered in, and whether Kindle is one."""
    bib_id = (bib_id or "").strip()
    if not bib_id:
        raise ValueError("bib_id must not be empty")

    url = config.record_url(bib_id)
    resp = await session.get(url)
    state = extract_state(resp.text, url=url)
    resolved_id, bib, redirect_note = _select_ebook_bib(state, bib_id, url=url)

    brief = bib.get("briefInfo") or {}
    policy = bib.get("policy") or {}
    avail = bib.get("availability") or {}
    authors = brief.get("authors") or []
    base = {
        "bib_id": resolved_id,
        "title": brief.get("title"),
        "author": authors[0] if authors else None,
    }

    # Not a digital title at all -- say so plainly rather than reporting "no Kindle".
    if policy.get("materialType") != "DIGITAL":
        return EbookFormatReport(
            **base,
            kindle_available=False,
            formats=[],
            source="bibliocommons",
            note=(
                f"{resolved_id} is a {policy.get('materialType') or 'non-digital'} edition "
                f"({brief.get('format')}), and this work has no ebook edition in the "
                "catalog, so there is no Kindle version to borrow from the library."
            ),
        )

    if not config.use_overdrive_thunder:
        return EbookFormatReport(
            **base,
            kindle_available=None,
            formats=[],
            source="bibliocommons",
            note=(
                "Kindle availability could not be determined: the OverDrive lookup is "
                "disabled (ebooks.use_overdrive_thunder = false). BiblioCommons alone does "
                "not publish per-format information."
            ),
        )

    item, how = await _find_overdrive_title(session, config, brief, avail)

    if item is None:
        return EbookFormatReport(
            **base,
            kindle_available=None,
            formats=[],
            source="overdrive-thunder",
            matched_by=how,
            note=(
                "Could not match this title to an OverDrive record"
                + (f" ({how})" if how else "")
                + ", so Kindle availability is unknown rather than absent. "
                "The title may come from a provider other than OverDrive."
            ),
        )

    overdrive_id = str(item.get("id")) if item.get("id") is not None else None

    # The search payload can omit per-format identifiers; the detail record
    # always carries them, and the ASIN is what makes a Kindle answer concrete.
    detail = await _thunder_get(session, config, f"/media/{overdrive_id}") if overdrive_id else None
    source_item = detail or item

    formats = _formats_from_item(source_item)
    kindle = next((f for f in formats if f.id == KINDLE_FORMAT_ID), None)

    notes = [n for n in (redirect_note,) if n]
    if not formats:
        notes.append("OverDrive returned no format list for this title.")

    return EbookFormatReport(
        **base,
        kindle_available=(kindle is not None) if formats else None,
        kindle_asin=kindle.identifier if kindle and kindle.identifier_type == "ASIN" else None,
        formats=formats,
        source="overdrive-thunder",
        matched_by=how,
        overdrive_id=overdrive_id,
        overdrive_url=(
            f"https://{config.overdrive_library}.overdrive.com/media/{overdrive_id}"
            if overdrive_id
            else None
        ),
        # Both sides' counts, so a bad join shows up instead of passing silently.
        availability={
            "overdrive": {
                "is_available": source_item.get("isAvailable"),
                "owned_copies": source_item.get("ownedCopies"),
                "available_copies": source_item.get("availableCopies"),
                "holds_count": source_item.get("holdsCount"),
                "estimated_wait_days": source_item.get("estimatedWaitDays"),
            },
            "bibliocommons": {
                "status": avail.get("status"),
                "total_copies": avail.get("totalCopies"),
                "available_copies": avail.get("availableCopies"),
                "held_copies": avail.get("heldCopies"),
            },
        },
        note=" ".join(notes) or None,
    )
