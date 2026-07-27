"""Reading what is currently borrowed.

One authenticated ``GET /v2/checkedout``. Physical and digital loans arrive
together in ``entities.checkouts``, discriminated by ``materialType`` -- the
same shape and the same parse pattern as holds.

Endpoint discovery note: ``/v2/checkouts`` and ``/v2/borrowing/checkedout`` also
answer HTTP 200 with a valid app state, but their ``entities.checkouts`` is
empty. Only ``/v2/checkedout`` is populated. Probing for a 200 would have picked
the wrong URL; the collection has to actually contain something.

This module is strictly read-only. Renewing and returning are real actions
BiblioCommons offers (``renew``, ``checkIn``), but they are out of scope here.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import Config
from .errors import ParseError
from .models import Checkout, CheckoutsList, CheckoutsSummary, format_label
from .session import SplSession
from .state import dig, extract_state

log = logging.getLogger("spl_mcp.checkouts")

# Statuses that mean the item is late. Open enum -- anything unrecognised is
# passed through and simply not counted as overdue.
OVERDUE_STATUSES = {"OVERDUE", "LATE"}


def _parse_checkout(checkout_id: str, raw: dict[str, Any], bibs: dict[str, Any]) -> Checkout:
    """Shape one checkout entity, joining the catalog record for title metadata.

    The checkout entity carries ``bibTitle`` but no author and no format code,
    so both are read from the linked bib in ``entities.bibs`` -- which the same
    page already includes. A missing bib degrades the extra fields to None
    rather than failing the whole list.
    """
    material_type = raw.get("materialType")
    is_digital = (material_type or "").upper() == "DIGITAL"
    actions = list(raw.get("actions") or [])

    bib = bibs.get(raw.get("metadataId") or "") or {}
    brief = bib.get("briefInfo") or {}
    authors = brief.get("authors") or []
    code = brief.get("format")

    return Checkout(
        checkout_id=checkout_id,
        title=raw.get("bibTitle") or brief.get("title"),
        author=authors[0] if authors else None,
        bib_id=raw.get("metadataId"),
        format=code,
        format_label=format_label(code),
        material_type=material_type,
        is_digital=is_digital,
        status=raw.get("status"),
        due_date=raw.get("dueDate"),
        times_renewed=raw.get("timesRenewed"),
        fines=raw.get("fines"),
        actions=actions,
        # The library publishes no `renewable` flag and no remaining-renewals
        # count. Its own UI decides both from the actions list, so we do too.
        renewable="renew" in actions,
        returnable="checkIn" in actions,
        barcode=raw.get("barcode"),
        call_number=raw.get("callNumber"),
        branch=raw.get("branch"),
        copy_label=raw.get("copy"),
        volume=raw.get("volume"),
        digital_id=raw.get("digitalId"),
        digital_format=raw.get("format"),
        download_url=raw.get("downloadUrl"),
        downloadable=bool(raw.get("downloadable", False)),
        read_online_url=raw.get("thirdPartyReaderUrl"),
    )


def parse_checkouts(state: dict[str, Any], *, url: str | None = None) -> CheckoutsList:
    checkouts = dig(state, "entities.checkouts", url=url, default={})
    if checkouts is None:
        checkouts = {}
    # Not `or {}`: coercing a wrong-typed payload into an empty result would
    # read as "you have nothing checked out", which is the dangerous answer.
    if not isinstance(checkouts, dict):
        raise ParseError(
            f"expected entities.checkouts to be an object, got {type(checkouts).__name__}",
            url=url,
            path="entities.checkouts",
        )
    bibs = dig(state, "entities.bibs", url=url, default={}) or {}

    physical: list[Checkout] = []
    digital: list[Checkout] = []
    by_status: dict[str, int] = {}
    overdue = 0

    for checkout_id, raw in checkouts.items():
        if not isinstance(raw, dict):
            continue
        item = _parse_checkout(checkout_id, raw, bibs)
        status = item.status or "UNKNOWN"
        by_status[status] = by_status.get(status, 0) + 1
        if status.upper() in OVERDUE_STATUSES:
            overdue += 1
        (digital if item.is_digital else physical).append(item)

    # Soonest due first -- the useful order for "what needs attention".
    for bucket in (physical, digital):
        bucket.sort(key=lambda c: (c.due_date is None, c.due_date or ""))

    due_dates = sorted(c.due_date for c in [*physical, *digital] if c.due_date)
    summary_block = dig(state, "borrowing.checkedout.summary", url=url, default={}) or {}
    next_due = (summary_block.get("nextDue") or {}).get("date") or (
        due_dates[0] if due_dates else None
    )

    return CheckoutsList(
        summary=CheckoutsSummary(
            total=len(physical) + len(digital),
            physical=len(physical),
            digital=len(digital),
            by_status=dict(sorted(by_status.items())),
            next_due=next_due,
            overdue=overdue,
        ),
        physical=physical,
        digital=digital,
    )


async def list_checkouts(session: SplSession, config: Config) -> CheckoutsList:
    """Everything currently borrowed. One authenticated request."""
    url = config.checkouts_url
    resp = await session.authenticated_get(url)
    return parse_checkouts(extract_state(resp.text, url=url), url=url)
