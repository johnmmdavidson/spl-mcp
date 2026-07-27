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
    # Deliberately NOT exposing the catalog's supportsSuspendHold /
    # supportsSuspendHoldStartDateEdit policy flags. Verified against SPL: they
    # read false on every physical item, while every real hold offers "suspend"
    # and suspending demonstrably works. Publishing them would tell the agent a
    # hold cannot be paused when it can. Whether a hold can be paused is
    # answered by that hold's own `actions` list, which list_holds returns.


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
    # Additive: a convenience view of the holds already present above, so the
    # existing shape stays backward-compatible.
    ready: ReadyToBorrow | None = Field(
        None, description="Holds waiting on you right now; same items as above, filtered"
    )


# ------------------------------------------------------------- checkouts


class Checkout(BaseModel):
    """One borrowed item, physical or digital."""

    checkout_id: str
    title: str | None = None
    author: str | None = None
    bib_id: str | None = Field(None, description="Join key back into the catalog")
    format: str | None = Field(None, description="Catalog format code, e.g. 'BK', 'EBOOK'")
    format_label: str = "Unknown"
    material_type: str | None = Field(None, description="'PHYSICAL' or 'DIGITAL'")
    is_digital: bool = False
    status: str | None = Field(None, description="'OUT', 'OVERDUE', ...")
    due_date: str | None = None
    times_renewed: int | None = None
    fines: float | None = None
    actions: list[str] = Field(
        default_factory=list, description="What the library currently offers for this loan"
    )
    # Derived from `actions`, which is the library's own capability signal.
    # There is no `renewable` boolean and no `renewals_remaining` count in the
    # payload -- the frontend itself computes renewability this same way.
    renewable: bool = False
    returnable: bool = False
    # physical
    barcode: str | None = None
    call_number: str | None = None
    branch: str | None = None
    copy_label: str | None = Field(None, description="Copy designation, from the payload's `copy`")
    volume: str | None = None
    # digital
    digital_id: str | None = None
    digital_format: str | None = Field(
        None, description="Chosen ebook format (e.g. Kindle/EPUB); null until one is picked"
    )
    download_url: str | None = None
    downloadable: bool = False
    read_online_url: str | None = Field(None, description="thirdPartyReaderUrl, when present")


class CheckoutsSummary(BaseModel):
    total: int = 0
    physical: int = 0
    digital: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    next_due: str | None = Field(None, description="Earliest due date across all loans")
    overdue: int = 0


class CheckoutsList(BaseModel):
    summary: CheckoutsSummary
    physical: list[Checkout] = Field(default_factory=list)
    digital: list[Checkout] = Field(default_factory=list)


# ------------------------------------------------------- ready to borrow


class ReadyItem(BaseModel):
    """A hold that is waiting on you right now."""

    type: str = Field(..., description="'physical' or 'digital'")
    hold_id: str
    title: str | None = None
    bib_id: str | None = None
    action_needed: str = Field(..., description="What to do: 'pick up' or 'borrow'")
    # physical
    pickup_branch: str | None = None
    pickup_branch_code: str | None = None
    pickup_by: str | None = Field(
        None,
        description=(
            "Deadline to collect it, from the hold's own pickupByDate field. "
            "Distinct from expires."
        ),
    )
    # digital
    can_checkout: bool = Field(
        False, description="True when the hold offers the 'checkout' action"
    )
    eresource_url: str | None = Field(
        None, description="Direct borrow link if the catalog publishes one; often null"
    )
    expires: str | None = Field(
        None, description="The hold's own expiryDate -- when the hold lapses, not a pickup deadline"
    )
    record_url: str | None = Field(None, description="Catalog page where the item can be acted on")
    actions: list[str] = Field(default_factory=list)


class ReadyToBorrow(BaseModel):
    total: int = 0
    physical: list[ReadyItem] = Field(default_factory=list)
    digital: list[ReadyItem] = Field(default_factory=list)


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
