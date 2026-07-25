"""Reading and changing holds.

Reading is one authenticated ``GET /v2/holds``: physical and digital holds come
back together in ``entities.holds``, discriminated by ``materialType``.

Changing them goes to the JSON gateway, never by driving the UI -- a synthetic
click on the record page's "Place hold" button throws a client-side error and
never sends the request, because the SPA expects state the click bypasses. The
gateway contract is documented in ``docs/GATEWAY.md``.

Scope note: **digital holds are deliberately read-only here.** BiblioCommons
does expose ``cancel`` on them, so this is a product decision rather than a
technical limit.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from .config import Config
from .errors import NotSupportedError, ParseError, ReadOnlyError, UpstreamError
from .models import Hold, HoldActionResult, HoldsList, HoldsSummary
from .session import SplSession
from .state import dig, extract_state

log = logging.getLogger("spl_mcp.holds")

LOCALE = "en-US"


# ------------------------------------------------------------------ read


def _parse_hold(hold_id: str, raw: dict[str, Any]) -> Hold:
    pickup = raw.get("pickupLocation") or {}
    return Hold(
        hold_id=hold_id,
        title=raw.get("bibTitle"),
        status=raw.get("status"),
        material_type=raw.get("materialType"),
        queue_position=raw.get("holdsPosition"),
        placed=raw.get("holdPlacedDate"),
        expires=raw.get("expiryDate"),
        suspend_end_date=raw.get("suspendEndDate"),
        bib_id=raw.get("metadataId"),
        # Actions are state-dependent -- a SUSPENDED hold offers different ones
        # than a NOT_YET_AVAILABLE one -- so we report what the account actually
        # offers rather than assuming.
        actions=list(raw.get("actions") or []),
        pickup_branch=pickup.get("name"),
        pickup_branch_code=pickup.get("code"),
        item_id=raw.get("itemId"),
    )


def parse_holds(state: dict[str, Any], *, url: str | None = None) -> HoldsList:
    holds = dig(state, "entities.holds", url=url, default={})
    if holds is None:
        holds = {}
    # Deliberately not `or {}`: that would coerce a wrong-typed payload into an
    # empty result, which is exactly the silent failure we must avoid -- an
    # empty holds list reads as "you have no holds".
    if not isinstance(holds, dict):
        raise ParseError(
            f"expected entities.holds to be an object, got {type(holds).__name__}",
            url=url,
            path="entities.holds",
        )

    physical: list[Hold] = []
    digital: list[Hold] = []
    by_status: dict[str, int] = {}

    for hold_id, raw in holds.items():
        if not isinstance(raw, dict):
            continue
        hold = _parse_hold(hold_id, raw)
        status = hold.status or "UNKNOWN"
        by_status[status] = by_status.get(status, 0) + 1
        # materialType is the discriminator; hold id shape (numeric vs UUID)
        # agrees with it but is not the contract.
        if (hold.material_type or "").upper() == "DIGITAL":
            digital.append(hold)
        else:
            physical.append(hold)

    physical.sort(key=lambda h: (h.queue_position is None, h.queue_position or 0))
    digital.sort(key=lambda h: (h.queue_position is None, h.queue_position or 0))

    return HoldsList(
        summary=HoldsSummary(
            total=len(physical) + len(digital),
            physical=len(physical),
            digital=len(digital),
            by_status=dict(sorted(by_status.items())),
        ),
        physical=physical,
        digital=digital,
    )


async def _fetch_holds_state(session: SplSession, config: Config) -> dict[str, Any]:
    url = config.holds_url
    resp = await session.authenticated_get(url)
    return extract_state(resp.text, url=url)


async def list_holds(session: SplSession, config: Config) -> HoldsList:
    """All current holds. One authenticated request."""
    state = await _fetch_holds_state(session, config)
    return parse_holds(state, url=config.holds_url)


# --------------------------------------------------------- gateway calls


class GatewayContext:
    """Everything a gateway request needs, read from live authenticated state.

    Reading the base URL and tokens out of the page rather than hardcoding them
    means a change on BiblioCommons' side is picked up automatically.
    """

    def __init__(self, state: dict[str, Any], config: Config):
        self.config = config
        self.state = state
        self.api_base = (
            dig(state, "app.apiGatewayURL", default=None) or f"{config.gateway_url}/v2"
        ).rstrip("/")
        self.session_id = dig(state, "auth.sessionId", default=None)
        self.auth_token = dig(state, "auth.authToken", default=None)
        self.account_id = self._resolve_account_id(state)

    @staticmethod
    def _resolve_account_id(state: dict[str, Any]) -> Any | None:
        """Mirror the frontend's selector: user -> their accounts -> this library.

        A patron can hold cards at several BiblioCommons libraries, and the
        page state carries all of them. Picking the wrong one gets the request
        rejected outright, so this never falls back to "the first account" --
        if the right one cannot be identified it returns None and the caller
        refuses to send anything.
        """
        user_id = dig(state, "auth.currentUserId", default=None)
        users = dig(state, "entities.users", default={}) or {}
        accounts = dig(state, "entities.accounts", default={}) or {}
        library_id = dig(state, "library.id", default=None)

        user = users.get(str(user_id)) if user_id is not None else None
        if user is None and len(users) == 1:
            user = next(iter(users.values()))

        # users[].accounts holds ints while entities.accounts is keyed by
        # string; compare as strings or every lookup misses.
        raw_ids = list((user or {}).get("accounts") or []) or list(accounts)
        candidates = [accounts[str(a)] for a in raw_ids if str(a) in accounts]

        def account_library(acct: dict[str, Any]) -> Any:
            # 'libId' is the live field name; the others are defensive.
            for field in ("libId", "libraryId", "library"):
                if field in acct:
                    return acct[field]
            return None

        if library_id is not None:
            matched = [a for a in candidates if account_library(a) == library_id]
            if len(matched) == 1:
                return matched[0].get("id")
            if len(matched) > 1:
                return None  # ambiguous: refuse rather than guess

        # Only safe to assume when there is genuinely nothing to choose between.
        if len(candidates) == 1:
            return candidates[0].get("id")
        return None

    @property
    def holds_url(self) -> str:
        return f"{self.api_base}/libraries/{self.config.slug}/holds"

    def headers(self) -> dict[str, str]:
        h = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": self.config.base_url,
            "Referer": f"{self.config.base_url}/v2/holds",
        }
        # Sent only when the page actually carried them, matching the frontend.
        if self.session_id:
            h["X-Session-Id"] = str(self.session_id)
        if self.auth_token:
            h["X-Access-Token"] = str(self.auth_token)
        return h

    def require_account_id(self) -> Any:
        if self.account_id is None:
            raise ParseError(
                "could not identify which library account to act on. Walked "
                "auth.currentUserId -> entities.users -> entities.accounts and found no "
                "single account matching library.id "
                f"({dig(self.state, 'library.id', default='unknown')}). If you hold cards at "
                "several BiblioCommons libraries, this server will not guess between them. "
                "Refusing to send a hold request."
            )
        return self.account_id


async def _gateway(
    session: SplSession, config: Config
) -> tuple[GatewayContext, HoldsList]:
    """Fetch authenticated state once and derive both the context and current holds."""
    state = await _fetch_holds_state(session, config)
    return GatewayContext(state, config), parse_holds(state, url=config.holds_url)


async def _call_gateway(
    session: SplSession, ctx: GatewayContext, method: str, body: dict[str, Any]
) -> dict[str, Any]:
    resp = await session.authenticated_request(
        method,
        ctx.holds_url,
        params={"locale": LOCALE},
        json=body,
        headers=ctx.headers(),
    )
    try:
        payload = resp.json() if resp.content else {}
    except ValueError:
        payload = {"raw": resp.text[:500]}

    if resp.status_code >= 400:
        raise UpstreamError(
            f"the holds gateway rejected {method} with HTTP {resp.status_code}: "
            f"{payload.get('error') or payload or resp.text[:300]}"
        )
    return payload if isinstance(payload, dict) else {"response": payload}


def _guard(config: Config, tool: str) -> None:
    """The hard read-only guardrail. Checked before any network call."""
    if config.read_only:
        raise ReadOnlyError(tool)


def _require_physical(hold: Hold, action: str) -> None:
    if (hold.material_type or "").upper() == "DIGITAL":
        raise NotSupportedError(
            f"{action} is not supported for digital holds. '{hold.title or hold.hold_id}' is a "
            "digital (ebook/audiobook) hold, and spl-mcp keeps digital holds read-only by "
            "design. Manage it in Libby or on the library website."
        )


def _find_hold(holds: HoldsList, hold_id: str) -> Hold:
    for hold in [*holds.physical, *holds.digital]:
        if hold.hold_id == hold_id:
            return hold
    known = ", ".join(h.hold_id for h in holds.physical[:8]) or "none"
    raise ValueError(
        f"no hold with id {hold_id!r} is on the account. Current physical hold ids: {known}. "
        "Call list_holds to see what is actually there."
    )


# ------------------------------------------------------------- mutations


async def place_hold(
    session: SplSession,
    config: Config,
    bib_id: str,
    pickup_branch: str | None = None,
) -> HoldActionResult:
    """Place a hold on a physical item."""
    _guard(config, "place_hold")
    bib_id = (bib_id or "").strip()
    if not bib_id:
        raise ValueError("bib_id must not be empty")

    # Check what we're about to request before requesting it.
    from .catalog import fetch_record  # local import avoids a module cycle

    bib = await fetch_record(session, config, bib_id)
    policy = bib.get("policy") or {}
    brief = bib.get("briefInfo") or {}
    material_type = policy.get("materialType")

    if material_type != "PHYSICAL":
        raise NotSupportedError(
            f"{bib_id} is a {material_type or 'non-physical'} item "
            f"({brief.get('format')}). spl-mcp only places holds on physical items; "
            "digital holds are read-only by design."
        )
    if not policy.get("holdable", False):
        raise NotSupportedError(
            f"{bib_id} ({brief.get('title')!r}) is not holdable according to the catalog."
        )

    branch = pickup_branch or config.default_pickup_branch
    if not branch:
        raise ValueError(
            "no pickup branch given and no default configured. Pass pickup_branch, or set "
            "holds.default_pickup_branch in config.toml."
        )

    ctx, before = await _gateway(session, config)
    already = next((h for h in before.physical if h.bib_id == bib_id), None)
    if already is not None:
        return HoldActionResult(
            ok=True,
            action="place_hold",
            hold_id=already.hold_id,
            bib_id=bib_id,
            message=(
                f"A hold on {already.title!r} already exists (position "
                f"{already.queue_position}); nothing was placed."
            ),
            detail={"already_held": True, "status": already.status},
        )

    payload = await _call_gateway(
        session,
        ctx,
        "POST",
        {
            "metadataId": bib_id,
            "materialType": "PHYSICAL",
            "accountId": ctx.require_account_id(),
            "enableSingleClickHolds": False,
            "materialParams": {"branchId": branch, "errorMessageLocale": LOCALE},
        },
    )

    after = await list_holds(session, config)
    placed = next((h for h in after.physical if h.bib_id == bib_id), None)
    if placed is None:
        raise UpstreamError(
            f"the gateway accepted the hold on {bib_id} but it does not appear in the holds "
            f"list afterwards. Gateway said: {payload}. Check the account before retrying."
        )

    return HoldActionResult(
        ok=True,
        action="place_hold",
        hold_id=placed.hold_id,
        bib_id=bib_id,
        message=(
            f"Placed a hold on {placed.title!r} for pickup at "
            f"{placed.pickup_branch or branch}. Queue position: {placed.queue_position}."
        ),
        detail={"queue_position": placed.queue_position, "status": placed.status},
    )


async def cancel_hold(session: SplSession, config: Config, hold_id: str) -> HoldActionResult:
    """Cancel a physical hold."""
    _guard(config, "cancel_hold")
    hold_id = str(hold_id or "").strip()
    if not hold_id:
        raise ValueError("hold_id must not be empty")

    ctx, before = await _gateway(session, config)
    hold = _find_hold(before, hold_id)
    _require_physical(hold, "cancel_hold")

    payload = await _call_gateway(
        session,
        ctx,
        "DELETE",
        {
            "accountId": ctx.require_account_id(),
            "holdIds": [hold_id],
            "metadataIds": [hold.bib_id] if hold.bib_id else [],
        },
    )

    after = await list_holds(session, config)
    if any(h.hold_id == hold_id for h in after.physical):
        raise UpstreamError(
            f"the gateway accepted the cancellation of hold {hold_id} but it is still on the "
            f"account. Gateway said: {payload}."
        )

    return HoldActionResult(
        ok=True,
        action="cancel_hold",
        hold_id=hold_id,
        bib_id=hold.bib_id,
        message=f"Cancelled the hold on {hold.title!r}.",
        detail={"failures": payload.get("failures") or []},
    )


async def suspend_hold(
    session: SplSession,
    config: Config,
    hold_id: str,
    until: date | str,
) -> HoldActionResult:
    """Pause a physical hold until a given date. Keeps queue position.

    ``until`` is **required**. Verified against the live gateway: a suspend sent
    without an ``endDate`` is answered with HTTP 200 and an empty ``failures``
    list, but the hold's status never changes. Accepting an open-ended pause
    would mean reporting a success that did not happen.
    """
    _guard(config, "suspend_hold")
    hold_id = str(hold_id or "").strip()
    if not hold_id:
        raise ValueError("hold_id must not be empty")

    if until is None or (isinstance(until, str) and not until.strip()):
        raise ValueError(
            "'until' is required: BiblioCommons accepts a suspend with no end date but "
            "silently ignores it, leaving the hold active. Give an ISO date (YYYY-MM-DD) "
            "for when the hold should resume."
        )

    end_date = until.isoformat() if isinstance(until, date) else str(until).strip()
    try:
        parsed = date.fromisoformat(end_date)
    except ValueError as exc:
        raise ValueError(f"'until' must be an ISO date (YYYY-MM-DD), got {end_date!r}") from exc
    if parsed <= date.today():
        raise ValueError(f"'until' must be in the future, got {end_date}")

    ctx, before = await _gateway(session, config)
    hold = _find_hold(before, hold_id)
    _require_physical(hold, "suspend_hold")

    # suspended is an object, not a boolean; endDate is the resume date and is
    # a different field from the hold's own expiryDate. See docs/GATEWAY.md.
    suspended: dict[str, Any] = {"status": True, "endDate": end_date}

    payload = await _call_gateway(
        session,
        ctx,
        "PATCH",
        {
            "accountId": ctx.require_account_id(),
            "holdIds": [hold_id],
            "suspended": suspended,
        },
    )

    after = await list_holds(session, config)
    updated = _find_hold(after, hold_id)
    if (updated.status or "").upper() != "SUSPENDED":
        raise UpstreamError(
            f"the gateway accepted the suspension of hold {hold_id} but its status is "
            f"{updated.status!r}, not SUSPENDED. Gateway said: {payload}."
        )

    tail = f" until {end_date}" if end_date else ""
    return HoldActionResult(
        ok=True,
        action="suspend_hold",
        hold_id=hold_id,
        bib_id=hold.bib_id,
        message=f"Suspended the hold on {hold.title!r}{tail}. Queue position is kept.",
        detail={"status": updated.status, "until": end_date},
    )


async def reactivate_hold(
    session: SplSession, config: Config, hold_id: str
) -> HoldActionResult:
    """Resume a suspended physical hold."""
    _guard(config, "reactivate_hold")
    hold_id = str(hold_id or "").strip()
    if not hold_id:
        raise ValueError("hold_id must not be empty")

    ctx, before = await _gateway(session, config)
    hold = _find_hold(before, hold_id)
    _require_physical(hold, "reactivate_hold")

    payload = await _call_gateway(
        session,
        ctx,
        "PATCH",
        {
            "accountId": ctx.require_account_id(),
            "holdIds": [hold_id],
            "suspended": {"status": False},
        },
    )

    after = await list_holds(session, config)
    updated = _find_hold(after, hold_id)
    if (updated.status or "").upper() == "SUSPENDED":
        raise UpstreamError(
            f"the gateway accepted the reactivation of hold {hold_id} but it is still "
            f"SUSPENDED. Gateway said: {payload}."
        )

    return HoldActionResult(
        ok=True,
        action="reactivate_hold",
        hold_id=hold_id,
        bib_id=hold.bib_id,
        message=(
            f"Reactivated the hold on {hold.title!r}. Status is now {updated.status}, "
            f"queue position {updated.queue_position}."
        ),
        detail={"status": updated.status, "queue_position": updated.queue_position},
    )
