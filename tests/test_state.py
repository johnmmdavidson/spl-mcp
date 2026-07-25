"""Tests for the embedded-JSON extraction.

The trap these guard: BiblioCommons puts ``data-iso-key="_0"`` on *two* nodes,
a <div> of rendered HTML and the <script> holding the JSON. Picking the div
yields garbage, so that selection is pinned here.
"""

from __future__ import annotations

import pytest

from spl_mcp.errors import ParseError
from spl_mcp.state import dig, extract_state

PAGE = """
<html><body>
  <div data-iso-key="_0"><h1>Rendered markup, not JSON</h1></div>
  <script data-iso-key="_0">{"entities": {"bibs": {"S1": {"id": "S1"}}}, "n": 1}</script>
</body></html>
"""


def test_prefers_the_script_node_over_the_div():
    state = extract_state(PAGE)
    assert state["n"] == 1
    assert state["entities"]["bibs"]["S1"]["id"] == "S1"


def test_state_has_no_data_wrapper():
    # entities/search/borrowing sit at the top level; there is no "data" key.
    state = extract_state(PAGE)
    assert "data" not in state
    assert "entities" in state


def test_missing_node_raises_parse_error():
    with pytest.raises(ParseError, match="data-iso-key"):
        extract_state("<html><body>nothing here</body></html>")


def test_div_only_raises_rather_than_returning_markup():
    html = '<html><div data-iso-key="_0"><p>hi</p></div></html>'
    with pytest.raises(ParseError, match="not on a <script>"):
        extract_state(html)


def test_invalid_json_raises_parse_error():
    html = '<script data-iso-key="_0">{not json}</script>'
    with pytest.raises(ParseError, match="not valid JSON"):
        extract_state(html)


def test_empty_script_raises_parse_error():
    with pytest.raises(ParseError, match="empty"):
        extract_state('<script data-iso-key="_0"></script>')


class TestDig:
    def test_walks_a_dotted_path(self):
        assert dig({"a": {"b": {"c": 42}}}, "a.b.c") == 42

    def test_missing_key_fails_loudly_and_names_the_path(self):
        with pytest.raises(ParseError) as exc:
            dig({"a": {"b": {}}}, "a.b.c")
        message = str(exc.value)
        assert "'c'" in message
        assert "a.b" in message

    def test_default_opts_into_tolerance(self):
        assert dig({"a": {}}, "a.b.c", default=None) is None

    def test_distinguishes_absent_key_from_null_value(self):
        # A present-but-null value is returned as None, not treated as missing.
        assert dig({"a": {"b": None}}, "a.b") is None
        with pytest.raises(ParseError):
            dig({"a": {}}, "a.b")

    def test_reports_available_keys_to_make_drift_diagnosable(self):
        with pytest.raises(ParseError) as exc:
            dig({"entities": {"bibs": {}}}, "entities.holds")
        assert "bibs" in str(exc.value)
