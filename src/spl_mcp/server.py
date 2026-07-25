"""FastMCP entrypoint. Registers the tools and serves them over stdio.

This server is spawned by an MCP client, not run as a daemon. Because the
cookie jar is persisted to disk, the library session outlives any single spawn.
"""

from __future__ import annotations

import logging
import sys
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from . import catalog, ebooks, holds
from .config import Config, load_config
from .errors import SplMcpError
from .models import format_label
from .session import SplSession

# stderr only: stdout is the MCP transport and anything printed there corrupts it.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("spl_mcp")

INSTRUCTIONS = """
Search the Seattle Public Library catalog and manage library holds.

Search results are grouped by work: each result carries a `formats` list with
one entry per edition (physical book, large print, audio CD, ebook,
eaudiobook). Use the `bib_id` of the specific format you want -- `place_hold`
takes a format's bib_id, not the work's.

Holds on physical items can be placed, cancelled, suspended, and reactivated.
Digital (ebook and eaudiobook) holds are read-only here: `list_holds` shows
them, but they must be managed in Libby or on the library website.

Mutating tools are disabled unless the server is configured with
read_only = false, in which case they will say so plainly.
""".strip()

mcp = FastMCP("spl", instructions=INSTRUCTIONS)

_config: Config | None = None
_session: SplSession | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = load_config()
        log.info(
            "loaded config: library=%s read_only=%s cookie_store=%s",
            _config.slug,
            _config.read_only,
            _config.cookie_store,
        )
    return _config


def get_session() -> SplSession:
    global _session
    if _session is None:
        _session = SplSession(get_config())
    return _session


def _fail(exc: Exception) -> ToolError:
    """Surface our own errors as clean agent-readable messages.

    Anything unexpected keeps its type name so a real bug stays diagnosable.
    """
    if isinstance(exc, SplMcpError | ValueError):
        return ToolError(str(exc))
    log.exception("unexpected error in tool")
    return ToolError(f"{type(exc).__name__}: {exc}")


# ------------------------------------------------------------- read tools


@mcp.tool(
    description=(
        "Search the library catalog. Returns works grouped across their formats, "
        "each with availability, copy counts, and whether it can be placed on hold."
    ),
    annotations={"readOnlyHint": True, "openWorldHint": True},
)
async def search_titles(
    query: Annotated[str, Field(description="Title, author, subject, or keywords")],
    limit: Annotated[int, Field(description="Max works to return", ge=1, le=100)] = 10,
) -> dict[str, Any]:
    try:
        result = await catalog.search_titles(get_session(), get_config(), query, limit)
        return result.model_dump(mode="json")
    except Exception as exc:
        raise _fail(exc) from exc


@mcp.tool(
    description=(
        "Get full detail for one catalog record by its bib id, including "
        "availability, copy counts, and hold policy."
    ),
    annotations={"readOnlyHint": True, "openWorldHint": True},
)
async def get_title(
    bib_id: Annotated[str, Field(description="Catalog record id, e.g. 'S30C3420266'")],
) -> dict[str, Any]:
    try:
        bib = await catalog.fetch_record(get_session(), get_config(), bib_id)
        brief = bib.get("briefInfo") or {}
        avail = bib.get("availability") or {}
        policy = bib.get("policy") or {}
        return {
            "bib_id": bib_id,
            "title": brief.get("title"),
            "subtitle": brief.get("subtitle") or None,
            "authors": brief.get("authors") or [],
            "format": brief.get("format"),
            "format_label": format_label(brief.get("format")),
            "edition": brief.get("edition"),
            "publication_date": brief.get("publicationDate"),
            "publisher": brief.get("publisher"),
            "isbns": brief.get("isbns") or [],
            "call_number": brief.get("callNumber"),
            "description": brief.get("description"),
            "subjects": brief.get("subjectHeadings") or [],
            "material_type": policy.get("materialType"),
            "is_digital": policy.get("materialType") == "DIGITAL",
            "provider": policy.get("provider"),
            "holdable": bool(policy.get("holdable", False)),
            "status": avail.get("status"),
            "total_copies": avail.get("totalCopies"),
            "available_copies": avail.get("availableCopies"),
            "hold_queue": avail.get("heldCopies"),
        }
    except Exception as exc:
        raise _fail(exc) from exc


@mcp.tool(
    description=(
        "Check which digital formats a title is offered in, and specifically "
        "whether it can be read on a Kindle. Accepts any edition's bib id -- if "
        "you pass a physical or audiobook edition it resolves the ebook edition "
        "of the same work. Returns kindle_available = null (not false) when it "
        "genuinely cannot be determined."
    ),
    annotations={"readOnlyHint": True, "openWorldHint": True},
)
async def check_ebook_formats(
    bib_id: Annotated[str, Field(description="Catalog record id of any edition")],
) -> dict[str, Any]:
    try:
        report = await ebooks.check_ebook_formats(get_session(), get_config(), bib_id)
        return report.model_dump(mode="json")
    except Exception as exc:
        raise _fail(exc) from exc


@mcp.tool(
    description=(
        "List all current holds on the library account: physical holds with full "
        "detail (queue position, pickup branch, dates) and digital holds with "
        "status. Requires credentials."
    ),
    annotations={"readOnlyHint": True, "openWorldHint": True},
)
async def list_holds() -> dict[str, Any]:
    try:
        result = await holds.list_holds(get_session(), get_config())
        return result.model_dump(mode="json")
    except Exception as exc:
        raise _fail(exc) from exc


# --------------------------------------------------------- mutating tools


@mcp.tool(
    description=(
        "Place a hold on a PHYSICAL item, given that format's bib id. Digital "
        "items are not supported. Disabled while the server is in read-only mode."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
)
async def place_hold(
    bib_id: Annotated[str, Field(description="Bib id of the physical format to hold")],
    pickup_branch: Annotated[
        str | None, Field(description="Branch code; defaults to the configured branch")
    ] = None,
) -> dict[str, Any]:
    try:
        result = await holds.place_hold(get_session(), get_config(), bib_id, pickup_branch)
        return result.model_dump(mode="json")
    except Exception as exc:
        raise _fail(exc) from exc


@mcp.tool(
    description=(
        "Cancel a physical hold by its hold id (from list_holds). Digital holds "
        "are not supported. Disabled while the server is in read-only mode."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
)
async def cancel_hold(
    hold_id: Annotated[str, Field(description="Hold id from list_holds")],
) -> dict[str, Any]:
    try:
        result = await holds.cancel_hold(get_session(), get_config(), hold_id)
        return result.model_dump(mode="json")
    except Exception as exc:
        raise _fail(exc) from exc


@mcp.tool(
    description=(
        "Pause a physical hold until a given date, keeping its queue position. "
        "'until' is required -- the library silently ignores a pause with no end "
        "date, leaving the hold active. Disabled while the server is in "
        "read-only mode."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
)
async def suspend_hold(
    hold_id: Annotated[str, Field(description="Hold id from list_holds")],
    until: Annotated[
        str, Field(description="ISO date (YYYY-MM-DD) the hold should resume. Required.")
    ],
) -> dict[str, Any]:
    try:
        result = await holds.suspend_hold(get_session(), get_config(), hold_id, until)
        return result.model_dump(mode="json")
    except Exception as exc:
        raise _fail(exc) from exc


@mcp.tool(
    description=(
        "Resume a suspended physical hold. Disabled while the server is in "
        "read-only mode."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
)
async def reactivate_hold(
    hold_id: Annotated[str, Field(description="Hold id from list_holds")],
) -> dict[str, Any]:
    try:
        result = await holds.reactivate_hold(get_session(), get_config(), hold_id)
        return result.model_dump(mode="json")
    except Exception as exc:
        raise _fail(exc) from exc


def main() -> None:
    cfg = get_config()
    log.info(
        "spl-mcp starting (library=%s, read_only=%s, credentials=%s)",
        cfg.slug,
        cfg.read_only,
        "yes" if cfg.credentials else "no",
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
