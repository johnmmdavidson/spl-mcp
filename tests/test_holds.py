"""Tests for holds parsing, the gateway context, and the read-only guardrail."""

from __future__ import annotations

import pytest

from spl_mcp.config import Config
from spl_mcp.errors import NotSupportedError, ParseError, ReadOnlyError
from spl_mcp.holds import (
    GatewayContext,
    _find_hold,
    _guard,
    _require_physical,
    parse_holds,
)
from spl_mcp.models import Hold


class TestParsing:
    def test_splits_physical_from_digital(self, holds_state):
        result = parse_holds(holds_state)
        assert result.summary.total == 5
        assert result.summary.physical == 3
        assert result.summary.digital == 2
        assert {h.hold_id for h in result.digital} == {
            "9D52F3B0-0000-4000-8000-000000000001",
            "9D52F3B0-0000-4000-8000-000000000002",
        }

    def test_material_type_is_the_discriminator_not_the_id_shape(self):
        # A numeric id that says DIGITAL must be classed digital.
        state = {
            "entities": {
                "holds": {
                    "12345": {
                        "holdsId": "12345",
                        "materialType": "DIGITAL",
                        "status": "NOT_YET_AVAILABLE",
                    }
                }
            }
        }
        result = parse_holds(state)
        assert result.summary.digital == 1
        assert result.summary.physical == 0

    def test_physical_holds_carry_full_detail(self, holds_state):
        hold = next(
            h for h in parse_holds(holds_state).physical if h.hold_id == "70000001"
        )
        assert hold.title == "An Example Physical Book"
        assert hold.status == "NOT_YET_AVAILABLE"
        assert hold.queue_position == 12
        assert hold.pickup_branch == "Central Library"
        assert hold.pickup_branch_code == "CEN"
        assert hold.placed == "2026-05-03"
        assert hold.expires == "2027-02-07"
        assert hold.bib_id == "S30C1000001"
        assert hold.item_id == "1000001"

    def test_actions_are_read_per_hold_not_assumed(self, holds_state):
        by_id = {h.hold_id: h for h in parse_holds(holds_state).physical}
        # Actions are state-dependent: a suspended hold offers reactivate,
        # a not-yet-available one offers suspend.
        assert by_id["70000001"].actions == ["cancel", "suspend"]
        assert by_id["70000003"].actions == ["cancel", "reactivate"]

    def test_digital_holds_have_no_pickup_branch(self, holds_state):
        for hold in parse_holds(holds_state).digital:
            assert hold.pickup_branch is None
            assert hold.item_id is None
            assert hold.status is not None

    def test_summary_counts_by_status(self, holds_state):
        summary = parse_holds(holds_state).summary
        assert summary.by_status["NOT_YET_AVAILABLE"] == 2
        assert summary.by_status["READY_FOR_PICKUP"] == 2
        assert summary.by_status["SUSPENDED"] == 1

    def test_holds_are_sorted_by_queue_position(self, holds_state):
        positions = [h.queue_position for h in parse_holds(holds_state).physical]
        assert positions == sorted(positions)

    def test_empty_holds_is_a_valid_empty_result(self):
        result = parse_holds({"entities": {"holds": {}}})
        assert result.summary.total == 0
        assert result.physical == [] and result.digital == []

    def test_wrong_shape_fails_loudly(self):
        with pytest.raises(ParseError, match="entities.holds"):
            parse_holds({"entities": {"holds": []}})


class TestGatewayContext:
    def test_reads_base_url_and_tokens_from_live_state(self, holds_state):
        ctx = GatewayContext(holds_state, Config())
        assert ctx.api_base == "https://gateway.bibliocommons.com/v2"
        assert ctx.holds_url == "https://gateway.bibliocommons.com/v2/libraries/seattle/holds"
        assert ctx.session_id == "test-session-id"
        assert ctx.auth_token == "test-auth-token"

    def test_resolves_account_id_via_user_then_library(self, holds_state):
        assert GatewayContext(holds_state, Config()).account_id == "acct-1"

    def test_picks_the_account_matching_this_library(self):
        state = {
            "app": {"apiGatewayURL": "https://gw/v2"},
            "library": {"id": 30},
            "auth": {"currentUserId": "1"},
            "entities": {
                "users": {"1": {"id": "1", "accounts": ["other", "mine"]}},
                "accounts": {
                    "other": {"id": "other", "libraryId": 99},
                    "mine": {"id": "mine", "libraryId": 30},
                },
            },
        }
        assert GatewayContext(state, Config()).account_id == "mine"

    def test_auth_headers_only_sent_when_present(self, holds_state):
        headers = GatewayContext(holds_state, Config()).headers()
        assert headers["X-Session-Id"] == "test-session-id"
        assert headers["Accept"] == "application/json"

        holds_state["auth"] = {"sessionId": None, "authToken": None, "currentUserId": "9001"}
        headers = GatewayContext(holds_state, Config()).headers()
        assert "X-Session-Id" not in headers
        assert "X-Access-Token" not in headers

    def test_refuses_to_send_without_an_account_id(self):
        ctx = GatewayContext({"app": {}, "auth": {}, "entities": {}}, Config())
        with pytest.raises(ParseError, match="account id"):
            ctx.require_account_id()


class TestGuardrails:
    @pytest.mark.parametrize(
        "tool", ["place_hold", "cancel_hold", "suspend_hold", "reactivate_hold"]
    )
    def test_read_only_blocks_every_mutating_tool(self, tool):
        with pytest.raises(ReadOnlyError) as exc:
            _guard(Config(read_only=True), tool)
        assert tool in str(exc.value)
        # The message must make clear nothing was sent.
        assert "no request was sent" in str(exc.value)

    def test_read_only_defaults_to_engaged(self):
        assert Config().read_only is True

    def test_guard_passes_when_read_only_is_off(self):
        _guard(Config(read_only=False), "place_hold")  # must not raise

    def test_digital_holds_are_refused_for_mutation(self, holds_state):
        digital = parse_holds(holds_state).digital[0]
        with pytest.raises(NotSupportedError, match="digital"):
            _require_physical(digital, "cancel_hold")

    def test_physical_holds_pass_the_material_check(self, holds_state):
        _require_physical(parse_holds(holds_state).physical[0], "cancel_hold")

    def test_unknown_hold_id_is_rejected_before_any_request(self, holds_state):
        with pytest.raises(ValueError, match="no hold with id"):
            _find_hold(parse_holds(holds_state), "does-not-exist")

    def test_find_hold_returns_the_match(self, holds_state):
        hold = _find_hold(parse_holds(holds_state), "70000002")
        assert isinstance(hold, Hold)
        assert hold.status == "READY_FOR_PICKUP"
