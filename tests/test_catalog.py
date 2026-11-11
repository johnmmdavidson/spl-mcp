"""Parser tests for catalog search, run offline against a saved fixture.

These exist so a BiblioCommons shape change fails a test rather than silently
returning junk.
"""

from __future__ import annotations

import pytest

from spl_mcp.catalog import parse_search
from spl_mcp.errors import ParseError
from spl_mcp.models import format_label


def test_parses_groups_and_pagination(search_state):
    results = parse_search(search_state, "dune")
    assert results.query == "dune"
    # Read from the fixture, so a fixture refresh (the count moves) needs no test edit.
    assert results.total_matches == search_state["search"]["catalogSearch"]["pagination"]["count"]
    assert isinstance(results.total_matches, int) and results.total_matches > 6
    assert results.returned == len(results.results) == 6


def test_representative_is_listed_first_among_formats(search_state):
    # The representative is the edition BiblioCommons collapsed the group
    # under, so it should be the agent's default choice.
    for group in parse_search(search_state, "dune").results:
        assert group.formats[0].bib_id == group.representative_bib_id


def test_format_variants_are_kept_not_collapsed(search_state):
    results = parse_search(search_state, "dune")
    multi = [g for g in results.results if len(g.formats) > 1]
    assert multi, "expected at least one work offered in several formats"

    group = multi[0]
    # Grouping is native to the payload; every format keeps its own bib id.
    assert len({f.bib_id for f in group.formats}) == len(group.formats)


def test_physical_and_digital_are_distinguished(search_state):
    formats = [f for g in parse_search(search_state, "dune").results for f in g.formats]
    digital = [f for f in formats if f.is_digital]
    physical = [f for f in formats if not f.is_digital]
    assert digital and physical

    assert all(f.material_type == "DIGITAL" for f in digital)
    assert all(f.provider != "ILS" for f in digital)
    assert all(f.material_type == "PHYSICAL" for f in physical)


def test_availability_signal_survives_parsing(search_state):
    # place_hold depends on these, so losing them must break a test.
    formats = [f for g in parse_search(search_state, "dune").results for f in g.formats]
    holdable = [f for f in formats if f.holdable]
    assert holdable
    assert any(f.total_copies for f in formats)
    assert all(f.status is not None for f in formats)


def test_titles_and_authors_are_populated(search_state):
    for group in parse_search(search_state, "dune").results:
        assert group.title and group.title != "(untitled)"
        assert group.representative_bib_id.startswith("S")
        if group.authors:
            assert group.author == group.authors[0]


def test_unknown_format_codes_pass_through(search_state):
    # The format code list is an open enum; unknown codes must survive rather
    # than being dropped or coerced.
    assert format_label("BK") == "Book"
    assert format_label("SOME_NEW_CODE_2027") == "SOME_NEW_CODE_2027"
    assert format_label(None) == "Unknown"

    for group in parse_search(search_state, "dune").results:
        for fmt in group.formats:
            assert fmt.format_label


def test_suspend_policy_flags_are_not_published(search_state):
    """The catalog's suspend flags are wrong for SPL and must not be exposed.

    Live-verified: every SPL physical bib reports supportsSuspendHold=false,
    while every real hold offers "suspend" and suspending works. Publishing the
    flag would tell the agent a hold cannot be paused when it can. The truth
    lives on the hold's own `actions` list, which list_holds returns.
    """
    results = parse_search(search_state, "dune")
    for group in results.results:
        for fmt in group.formats:
            dumped = fmt.model_dump()
            assert "supports_suspend_hold" not in dumped
            assert "supports_suspend_start_date_edit" not in dumped


def test_missing_results_key_fails_loudly():
    with pytest.raises(ParseError, match="results"):
        parse_search({"search": {"catalogSearch": {}}, "entities": {"bibs": {}}}, "q")


def test_missing_bibs_entity_fails_loudly():
    state = {"search": {"catalogSearch": {"results": []}}}
    with pytest.raises(ParseError, match="entities"):
        parse_search(state, "q")


def test_group_with_unresolvable_representative_is_skipped():
    # Better to drop a group than invent a title for it.
    state = {
        "search": {
            "catalogSearch": {
                "results": [{"representative": "MISSING", "manifestations": []}],
                "pagination": {"count": 1, "page": 1},
            }
        },
        "entities": {"bibs": {}},
    }
    assert parse_search(state, "q").results == []


def test_partial_bib_does_not_fail_the_whole_search():
    # One odd record must not take out an entire result set.
    state = {
        "search": {
            "catalogSearch": {
                "results": [{"representative": "S1", "manifestations": ["S1"]}],
                "pagination": {"count": 1, "page": 1},
            }
        },
        "entities": {"bibs": {"S1": {"id": "S1", "briefInfo": {"title": "Sparse"}}}},
    }
    results = parse_search(state, "q")
    assert results.results[0].title == "Sparse"
    fmt = results.results[0].formats[0]
    assert fmt.status is None and fmt.holdable is False


# --------------------------------------------------- renumbered record ids

from spl_mcp.catalog import missing_bib_error, queue_summary  # noqa: E402
from spl_mcp.errors import StaleIdError  # noqa: E402


def _renumbered_page():
    bibs = {
        "S246C1": {"briefInfo": {"title": "Dune", "format": "BK"}},
        "S246C2": {"briefInfo": {"title": "Dune", "format": "EBOOK"}},
    }
    state = {
        "entities": {"bibs": bibs},
        "catalogBibs": {
            "catalogBib": {"id": "S246C1"},
            "manifestations": {"ids": ["S246C1", "S246C2"]},
        },
    }
    return state, bibs


def test_old_id_is_named_stale_with_the_current_ids():
    state, bibs = _renumbered_page()
    err = missing_bib_error(state, bibs, "S30C3420266", url="http://test")
    assert isinstance(err, StaleIdError)
    assert "S30C3420266" in str(err) and "S246C1" in str(err) and "S246C2" in str(err)


def test_unknown_id_without_a_current_record_stays_a_parse_error():
    state, bibs = _renumbered_page()
    del state["catalogBibs"]
    err = missing_bib_error(state, bibs, "S30C3420266", url="http://test")
    assert isinstance(err, ParseError)


def test_queue_summary_uses_the_librarys_counts():
    q = queue_summary({"heldCopies": 47, "totalCopies": 9, "availableCopies": 0})
    assert q["summary"] == "47 holds on 9 copies"
    assert q["position_if_placed"] is None


def test_queue_summary_names_copies_available_now():
    q = queue_summary({"heldCopies": 0, "totalCopies": 1, "availableCopies": 1})
    assert q["summary"] == "0 holds on 1 copy; 1 available now"


def test_queue_summary_unknown_is_none_not_zero():
    q = queue_summary({})
    assert q["holds"] is None and q["copies"] is None and q["summary"] is None
