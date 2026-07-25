"""Pydantic models for the MCP tool contracts.

JSON is the interface. Every tool returns one of these, dumped with
``model_dump(mode="json")``, so the shape stays stable and self-documenting.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# BiblioCommons format codes are an OPEN enum -- new ones appear without notice.
# We map the ones we've seen to friendly labels and pass anything else through
# unchanged rather than dropping or guessing at it.
FORMAT_LABELS: dict[str, str] = {
    "BK": "Book",
    "LPRINT": "Large Print",
    "GRAPHIC_NOVEL": "Graphic Novel",
    "BOOK_CD": "Audiobook on CD",
    "AB": "Audiobook",
    "MUSIC_CD": "Music CD",
    "DVD": "DVD",
    "BLURAY": "Blu-ray",
    "EBOOK": "eBook",
    "EAUDIOBOOK": "eAudiobook",
    "EVIDEO": "eVideo",
    "MAG": "Magazine",
    "NEWSPAPER": "Newspaper",
    "JOURNAL_MF": "Journal (microform)",
    "OT": "Other",
}


def format_label(code: str | None) -> str:
    """Friendly label for a format code, falling back to the raw code."""
    if not code:
        return "Unknown"
    return FORMAT_LABELS.get(code, code)


# ---------------------------------------------------------------- search


class FormatOption(BaseModel):
    """One format variant (manifestation) of a work."""

    bib_id: str
    format: str | None = Field(None, description="Raw BiblioCommons format code, e.g. 'BK'")
    format_label: str = Field(..., description="Human-readable format name")
    material_type: str | None = Field(None, description="'PHYSICAL' or 'DIGITAL'")
    is_digital: bool
    provider: str | None = Field(None, description="'ILS' for physical, e.g. 'OverDriveAPI'")
    holdable: bool = False
    status: str | None = Field(None, description="'AVAILABLE', 'UNAVAILABLE', ...")
    total_copies: int | None = None
    available_copies: int | None = None
    hold_queue: int | None = Field(None, description="Copies currently on hold ahead of you")
    edition: str | None = None
    publication_date: str | None = None
    supports_suspend_hold: bool = False
    supports_suspend_start_date_edit: bool = False


class WorkGroup(BaseModel):
    """A work, with its format variants grouped as BiblioCommons groups them."""

    title: str
    subtitle: str | None = None
    author: str | None = None
    authors: list[str] = Field(default_factory=list)
    representative_bib_id: str
    formats: list[FormatOption] = Field(default_factory=list)


class SearchResults(BaseModel):
    query: str
    total_matches: int | None = None
    page: int = 1
    returned: int = 0
    results: list[WorkGroup] = Field(default_factory=list)


# ----------------------------------------------------------------- holds


class Hold(BaseModel):
    hold_id: str
    title: str | None = None
    status: str | None = Field(None, description="'NOT_YET_AVAILABLE', 'READY_FOR_PICKUP', ...")
    material_type: str | None = None
    queue_position: int | None = None
    placed: str | None = None
    expires: str | None = None
    suspend_end_date: str | None = Field(
        None, description="Date a suspended hold resumes; only present while SUSPENDED"
    )
    bib_id: str | None = Field(None, description="Join key back into the catalog")
    actions: list[str] = Field(
        default_factory=list, description="Actions BiblioCommons currently offers for this hold"
    )
    # physical only
    pickup_branch: str | None = None
    pickup_branch_code: str | None = None
    item_id: str | None = None


class HoldsSummary(BaseModel):
    total: int = 0
    physical: int = 0
    digital: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)


class HoldsList(BaseModel):
    summary: HoldsSummary
    physical: list[Hold] = Field(default_factory=list)
    digital: list[Hold] = Field(default_factory=list)


# ---------------------------------------------------------------- ebooks


class EbookFormat(BaseModel):
    id: str = Field(..., description="OverDrive format id, e.g. 'ebook-kindle'")
    label: str
    identifier_type: str | None = None
    identifier: str | None = Field(None, description="ISBN, or ASIN for the Kindle format")


class EbookFormatReport(BaseModel):
    bib_id: str
    title: str | None = None
    author: str | None = None
    kindle_available: bool | None = Field(
        None, description="True/False when resolved; null when it could not be determined"
    )
    kindle_asin: str | None = None
    formats: list[EbookFormat] = Field(default_factory=list)
    # provenance -- the agent should be able to see how we concluded this
    source: str = Field(..., description="Where the format list came from")
    matched_by: str | None = Field(None, description="How the OverDrive title was matched")
    overdrive_id: str | None = None
    overdrive_url: str | None = None
    availability: dict | None = Field(
        None, description="Corroborating copy/hold counts from OverDrive"
    )
    note: str | None = Field(None, description="Set when the answer is partial or unresolved")


# -------------------------------------------------------------- mutation


class HoldActionResult(BaseModel):
    ok: bool
    action: str
    hold_id: str | None = None
    bib_id: str | None = None
    message: str
    detail: dict | None = None
