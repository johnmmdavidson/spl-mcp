"""Parser tests for checkouts, offline against a synthetic fixture."""

from __future__ import annotations

import pytest

from spl_mcp.checkouts import parse_checkouts
from spl_mcp.config import Config
from spl_mcp.errors import ParseError


class TestParsing:
    def test_splits_physical_from_digital(self, checkouts_state):
        result = parse_checkouts(checkouts_state)
        assert result.summary.total == 4
        assert result.summary.physical == 2
        assert result.summary.digital == 2

    def test_negative_numeric_id_is_still_digital(self, checkouts_state):
        """The id shape does NOT discriminate material type here.

        A digital checkout has a *negative number* as its checkoutId and carries
        the UUID separately in `digitalId` -- the opposite of the holds payload,
        where digital ids are UUIDs. materialType is the only safe signal.
        """
        result = parse_checkouts(checkouts_state)
        digital_ids = {c.checkout_id for c in result.digital}
        assert digital_ids == {"-55000001", "-55000002"}
        for item in result.digital:
            assert item.checkout_id.lstrip("-").isdigit()
            assert item.digital_id and "-" in item.digital_id  # the UUID lives here

    def test_author_and_format_are_joined_from_the_bib(self, checkouts_state):
        # The checkout entity has bibTitle but no author and no format code.
        item = next(
            c for c in parse_checkouts(checkouts_state).physical if c.checkout_id == "90000001"
        )
        assert item.author == "Example, Ann"
        assert item.format == "BK"
        assert item.format_label == "Book"

    def test_missing_bib_degrades_without_failing(self, checkouts_state):
        del checkouts_state["entities"]["bibs"]["S246C1000001"]
        item = next(
            c for c in parse_checkouts(checkouts_state).physical if c.checkout_id == "90000001"
        )
        assert item.title == "An Example Borrowed Book"  # from bibTitle
        assert item.author is None and item.format is None

    def test_due_dates_and_next_due(self, checkouts_state):
        result = parse_checkouts(checkouts_state)
        assert result.summary.next_due == "2026-07-01"
        assert result.summary.overdue == 1
        assert result.summary.by_status == {"OUT": 3, "OVERDUE": 1}

    def test_sorted_soonest_due_first(self, checkouts_state):
        result = parse_checkouts(checkouts_state)
        for bucket in (result.physical, result.digital):
            dates = [c.due_date for c in bucket]
            assert dates == sorted(dates)

    def test_physical_fields(self, checkouts_state):
        item = next(
            c for c in parse_checkouts(checkouts_state).physical if c.checkout_id == "90000001"
        )
        assert item.barcode == "0010000000001"
        assert item.call_number == "FIC EXAMPLE"
        assert item.branch == "Montlake Branch"
        assert item.times_renewed == 1

    def test_digital_read_links_when_a_format_is_chosen(self, checkouts_state):
        result = parse_checkouts(checkouts_state)
        unchosen = next(c for c in result.digital if c.checkout_id == "-55000001")
        chosen = next(c for c in result.digital if c.checkout_id == "-55000002")

        # No format picked yet -> no links, and updateFormat is offered.
        assert unchosen.digital_format is None
        assert unchosen.download_url is None and unchosen.read_online_url is None
        assert "updateFormat" in unchosen.actions

        assert chosen.digital_format == "EPUB"
        assert chosen.download_url and chosen.read_online_url
        assert chosen.downloadable is True

    def test_empty_checkouts_is_a_valid_empty_result(self):
        result = parse_checkouts({"entities": {"checkouts": {}}})
        assert result.summary.total == 0
        assert result.physical == [] and result.digital == []

    def test_wrong_shape_fails_loudly(self):
        with pytest.raises(ParseError, match="entities.checkouts"):
            parse_checkouts({"entities": {"checkouts": []}})


class TestRenewability:
    """There is no `renewable` flag or renewals-remaining count in the payload.

    The library's own UI computes both as `actions.contains("renew")` /
    `actions.contains("checkIn")`, so we do the same rather than inventing a
    field. Same lesson as the holds `supportsSuspendHold` policy flag.
    """

    def test_renewable_is_derived_from_actions(self, checkouts_state):
        result = parse_checkouts(checkouts_state)
        by_id = {c.checkout_id: c for c in [*result.physical, *result.digital]}
        assert by_id["90000001"].renewable is True  # actions: [renew]
        assert by_id["90000002"].renewable is False  # actions: []
        assert by_id["-55000002"].renewable is True  # actions: [checkIn, renew]

    def test_returnable_is_derived_from_actions(self, checkouts_state):
        result = parse_checkouts(checkouts_state)
        by_id = {c.checkout_id: c for c in [*result.physical, *result.digital]}
        assert by_id["-55000001"].returnable is True  # actions: [checkIn, ...]
        assert by_id["90000001"].returnable is False

    def test_no_invented_renewals_remaining_field(self, checkouts_state):
        item = parse_checkouts(checkouts_state).physical[0]
        dumped = item.model_dump()
        assert "renewals_remaining" not in dumped
        assert "checkout_date" not in dumped  # not published by the library either
        assert "times_renewed" in dumped


class TestConfig:
    def test_checkouts_url_is_checkedout_not_checkouts(self):
        # /v2/checkouts and /v2/borrowing/checkedout both return HTTP 200 with a
        # valid app state, but an EMPTY entities.checkouts. Probing for a 200
        # would have silently picked a URL that always reports zero loans.
        assert Config().checkouts_url == "https://seattle.bibliocommons.com/v2/checkedout"
