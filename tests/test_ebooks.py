"""Tests for the BiblioCommons -> OverDrive join used to answer the Kindle question.

The risk here is a *wrong* join passing silently: reporting Kindle availability
for a different edition, or for the audiobook of the same title. These pin the
disambiguation ladder and the honest-uncertainty behaviour.
"""

from __future__ import annotations

from spl_mcp.ebooks import (
    KINDLE_FORMAT_ID,
    _author_surname,
    _confident_match,
    _formats_from_item,
    _normalise_isbn,
    _select_ebook_bib,
)

EBOOK = {
    "id": 1,
    "title": "Dune",
    "firstCreatorName": "Frank Herbert",
    "type": {"id": "ebook"},
    "ownedCopies": 15,
}
AUDIOBOOK = {
    "id": 2,
    "title": "Dune",
    "firstCreatorName": "Frank Herbert",
    "type": {"id": "audiobook"},
    "ownedCopies": 227,
}
OTHER_EBOOK_EDITION = {
    "id": 3,
    "title": "Dune",
    "firstCreatorName": "Frank Herbert",
    "type": {"id": "ebook"},
    "ownedCopies": 1,
}
DIFFERENT_WORK = {
    "id": 4,
    "title": "Dune Messiah",
    "firstCreatorName": "Frank Herbert",
    "type": {"id": "ebook"},
    "ownedCopies": 9,
}


class TestNormalisation:
    def test_strips_isbn_qualifiers_and_hyphens(self):
        assert _normalise_isbn("978-0441013593 (pbk.)") == "9780441013593"
        assert _normalise_isbn("9781101658055") == "9781101658055"
        assert _normalise_isbn("") == ""

    def test_author_surname(self):
        assert _author_surname("Herbert, Frank") == "herbert"
        assert _author_surname("Le Guin, Ursula K.") == "le guin"
        assert _author_surname(None) == ""


class TestConfidentMatch:
    def test_exact_single_match(self):
        item, how = _confident_match(
            [EBOOK, DIFFERENT_WORK],
            title="Dune",
            author="Herbert, Frank",
            media_type="ebook",
            total_copies=15,
        )
        assert item is EBOOK
        assert how

    def test_media_type_separates_ebook_from_audiobook(self):
        # The audiobook of the same title by the same author is the trap.
        item, _ = _confident_match(
            [EBOOK, AUDIOBOOK],
            title="Dune",
            author="Herbert, Frank",
            media_type="ebook",
            total_copies=None,
        )
        assert item is EBOOK

        item, _ = _confident_match(
            [EBOOK, AUDIOBOOK],
            title="Dune",
            author="Herbert, Frank",
            media_type="audiobook",
            total_copies=None,
        )
        assert item is AUDIOBOOK

    def test_copy_count_breaks_a_tie_between_editions(self):
        item, how = _confident_match(
            [EBOOK, OTHER_EBOOK_EDITION],
            title="Dune",
            author="Herbert, Frank",
            media_type="ebook",
            total_copies=15,
        )
        assert item is EBOOK
        assert "copies" in how

    def test_refuses_to_guess_when_still_ambiguous(self):
        item, how = _confident_match(
            [EBOOK, OTHER_EBOOK_EDITION],
            title="Dune",
            author="Herbert, Frank",
            media_type="ebook",
            total_copies=None,
        )
        assert item is None
        assert "ambiguous" in how

    def test_no_title_match_returns_nothing(self):
        item, how = _confident_match(
            [DIFFERENT_WORK],
            title="Dune",
            author="Herbert, Frank",
            media_type="ebook",
            total_copies=None,
        )
        assert item is None
        assert "no OverDrive title matched" in how

    def test_wrong_author_is_not_matched(self):
        impostor = {**EBOOK, "firstCreatorName": "Someone Else", "id": 99}
        item, _ = _confident_match(
            [impostor, EBOOK],
            title="Dune",
            author="Herbert, Frank",
            media_type="ebook",
            total_copies=15,
        )
        assert item is EBOOK


class TestFormats:
    def test_extracts_kindle_asin_and_isbns(self):
        formats = _formats_from_item(
            {
                "formats": [
                    {"id": "ebook-kindle", "identifiers": [{"type": "ASIN", "value": "B00X"}]},
                    {
                        "id": "ebook-epub-adobe",
                        "identifiers": [{"type": "ISBN", "value": "9781101658055"}],
                    },
                ]
            }
        )
        by_id = {f.id: f for f in formats}
        assert by_id[KINDLE_FORMAT_ID].identifier == "B00X"
        assert by_id[KINDLE_FORMAT_ID].identifier_type == "ASIN"
        assert by_id[KINDLE_FORMAT_ID].label == "Kindle"
        assert by_id["ebook-epub-adobe"].identifier == "9781101658055"

    def test_unknown_format_ids_pass_through(self):
        formats = _formats_from_item({"formats": [{"id": "ebook-somethingnew"}]})
        assert formats[0].id == "ebook-somethingnew"
        assert formats[0].label == "ebook-somethingnew"

    def test_empty_format_list(self):
        assert _formats_from_item({}) == []


def _bib_id_of_format(state: dict, fmt: str) -> str:
    """The fixture's record id for one format, so a fixture refresh needs no test edit."""
    (bib_id,) = [
        bid for bid, b in state["entities"]["bibs"].items() if b["briefInfo"]["format"] == fmt
    ]
    return bib_id


class TestEditionSelection:
    def test_uses_the_ebook_edition_directly(self, record_ebook_state):
        ebook = _bib_id_of_format(record_ebook_state, "EBOOK")
        bib_id, bib, note = _select_ebook_bib(record_ebook_state, ebook, url="http://test")
        assert bib_id == ebook
        assert note is None
        assert bib["briefInfo"]["format"] == "EBOOK"

    def test_resolves_the_ebook_sibling_of_a_physical_edition(self, record_ebook_state):
        # Asking "can I Kindle this?" about the paperback should answer about
        # the ebook edition of the same work, and say that it did.
        ebook = _bib_id_of_format(record_ebook_state, "EBOOK")
        paperback = _bib_id_of_format(record_ebook_state, "BK")
        bib_id, bib, note = _select_ebook_bib(record_ebook_state, paperback, url="http://test")
        assert bib_id == ebook
        assert bib["briefInfo"]["format"] == "EBOOK"
        assert paperback in note and "ebook edition" in note

    def test_resolves_the_ebook_sibling_of_an_audiobook(self, record_ebook_state):
        ebook = _bib_id_of_format(record_ebook_state, "EBOOK")
        audiobook = _bib_id_of_format(record_ebook_state, "EAUDIOBOOK")
        bib_id, _, note = _select_ebook_bib(record_ebook_state, audiobook, url="http://test")
        assert bib_id == ebook
        assert "EAUDIOBOOK" in note

    def test_fixture_carries_current_record_ids(self, record_ebook_state):
        # Seattle renumbered S30C... to S246C...; a refresh must capture current ids.
        assert all(bid.startswith("S246C") for bid in record_ebook_state["entities"]["bibs"])

    def test_only_matches_siblings_of_the_same_work(self):
        # An unrelated ebook on the page must never be picked up.
        state = {
            "entities": {
                "bibs": {
                    "TARGET": {
                        "briefInfo": {"format": "BK", "groupKey": "work-a"},
                        "policy": {"materialType": "PHYSICAL"},
                    },
                    "UNRELATED": {
                        "briefInfo": {"format": "EBOOK", "groupKey": "work-b"},
                        "policy": {"materialType": "DIGITAL"},
                    },
                }
            }
        }
        bib_id, _, note = _select_ebook_bib(state, "TARGET", url="http://test")
        assert bib_id == "TARGET"
        assert note is None
