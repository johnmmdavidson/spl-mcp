"""Tests for list_ready_to_borrow.

The important distinction pinned here is between two date fields that are easy
to conflate, and where conflating them gives an answer that is wrong by months.
"""

from __future__ import annotations

import pytest

from spl_mcp.config import Config
from spl_mcp.errors import ParseError
from spl_mcp.holds import parse_holds, parse_ready_to_borrow

CONFIG = Config()


class TestFiltering:
    def test_only_ready_holds_are_included(self, holds_state):
        ready = parse_ready_to_borrow(holds_state, CONFIG)
        assert ready.total == 2
        assert len(ready.physical) == 1
        assert len(ready.digital) == 1
        # The fixture also has NOT_YET_AVAILABLE and SUSPENDED holds.
        assert parse_holds(holds_state).summary.total == 5

    def test_in_transit_is_not_ready(self):
        # Observed live: a hold on its way to the branch is IN_TRANSIT, not
        # READY_FOR_PICKUP. It cannot be collected yet, so it must not appear.
        state = {
            "entities": {
                "holds": {
                    "1": {
                        "holdsId": "1",
                        "materialType": "PHYSICAL",
                        "status": "IN_TRANSIT",
                        "bibTitle": "On its way",
                        "actions": ["cancel"],
                    }
                }
            }
        }
        assert parse_ready_to_borrow(state, CONFIG).total == 0

    def test_empty_is_a_valid_empty_result(self):
        result = parse_ready_to_borrow({"entities": {"holds": {}}}, CONFIG)
        assert result.total == 0

    def test_wrong_shape_fails_loudly(self):
        with pytest.raises(ParseError, match="entities.holds"):
            parse_ready_to_borrow({"entities": {"holds": []}}, CONFIG)


class TestPickupDeadline:
    """`pickupByDate` is the collect-by deadline. `expiryDate` is NOT.

    The library's own UI renders pickupByDate only when a hold is
    READY_FOR_PICKUP, and deliberately hides expiryDate in that state --
    expiryDate keeps its meaning of "when the hold lapses in the queue", which
    is typically months out. Reporting expiryDate as a pickup deadline would
    tell the user they have until next year to collect a book the branch will
    reshelve in a week.
    """

    def test_pickup_by_comes_from_pickup_by_date(self, holds_state):
        item = parse_ready_to_borrow(holds_state, CONFIG).physical[0]
        assert item.pickup_by == "2026-08-01"

    def test_expires_is_reported_separately_and_differs(self, holds_state):
        item = parse_ready_to_borrow(holds_state, CONFIG).physical[0]
        assert item.expires == "2027-08-01"
        assert item.pickup_by != item.expires

    def test_missing_pickup_by_date_is_none_not_expiry(self, holds_state):
        del holds_state["entities"]["holds"]["70000002"]["pickupByDate"]
        item = parse_ready_to_borrow(holds_state, CONFIG).physical[0]
        # Must NOT fall back to expiryDate -- a wrong date beats no date only
        # if you enjoy missing pickups.
        assert item.pickup_by is None
        assert item.expires == "2027-08-01"


class TestAnnotation:
    def test_physical_item_says_pick_up_and_names_the_branch(self, holds_state):
        item = parse_ready_to_borrow(holds_state, CONFIG).physical[0]
        assert item.type == "physical"
        assert item.action_needed == "pick up"
        assert item.pickup_branch == "Ballard Branch"
        assert item.pickup_branch_code == "BAL"
        assert item.title == "A Book Waiting On The Shelf"
        assert item.hold_id == "70000002"

    def test_digital_item_says_borrow_and_reports_checkout_availability(self, holds_state):
        item = parse_ready_to_borrow(holds_state, CONFIG).digital[0]
        assert item.type == "digital"
        assert item.action_needed == "borrow"
        # The frontend's own test for a borrowable digital hold.
        assert "checkout" in item.actions
        assert item.can_checkout is True
        assert item.pickup_branch is None and item.pickup_by is None

    def test_can_checkout_false_when_the_action_is_absent(self, holds_state):
        holds_state["entities"]["holds"]["9D52F3B0-0000-4000-8000-000000000001"]["actions"] = [
            "cancel"
        ]
        item = parse_ready_to_borrow(holds_state, CONFIG).digital[0]
        assert item.can_checkout is False

    def test_record_url_is_always_actionable(self, holds_state):
        for item in parse_ready_to_borrow(holds_state, CONFIG).physical:
            assert item.record_url == f"https://seattle.bibliocommons.com/v2/record/{item.bib_id}"

    def test_eresource_url_is_reported_not_fabricated(self, holds_state):
        # SPL leaves eresourceUrl null on its OverDrive titles, and the fixture
        # has no bib entities, so this must be None rather than a guess.
        item = parse_ready_to_borrow(holds_state, CONFIG).digital[0]
        assert item.eresource_url is None

    def test_eresource_url_is_picked_up_when_present(self, holds_state):
        holds_state["entities"]["bibs"] = {
            "S246C2000001": {"availability": {"eresourceUrl": "https://example.invalid/borrow"}}
        }
        item = parse_ready_to_borrow(holds_state, CONFIG).digital[0]
        assert item.eresource_url == "https://example.invalid/borrow"


class TestListHoldsBackwardCompatibility:
    def test_existing_keys_are_unchanged(self, holds_state):
        # `ready` is additive; callers written against the old shape keep working.
        result = parse_holds(holds_state)
        dumped = result.model_dump()
        assert set(dumped) == {"summary", "physical", "digital", "ready"}
        assert dumped["summary"]["total"] == 5
        assert len(dumped["physical"]) == 3
        assert len(dumped["digital"]) == 2

    def test_parse_holds_alone_leaves_ready_unset(self, holds_state):
        # Only list_holds populates it, so the pure parser stays cheap.
        assert parse_holds(holds_state).ready is None
