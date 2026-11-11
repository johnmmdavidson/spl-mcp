"""The fixture refresh script: it finds its record by search and runs anonymously."""

from __future__ import annotations

import asyncio
import importlib.util
import re
from pathlib import Path

import pytest

from spl_mcp.config import Config

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "refresh_fixtures.py"


@pytest.fixture(scope="module")
def refresh():
    spec = importlib.util.spec_from_file_location("refresh_fixtures", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _state(results: list[dict], bibs: dict[str, tuple[str, str]]) -> dict:
    return {
        "search": {"catalogSearch": {"results": results}},
        "entities": {
            "bibs": {
                bid: {"briefInfo": {"title": title, "format": fmt}}
                for bid, (title, fmt) in bibs.items()
            }
        },
    }


def test_no_record_id_is_hardcoded(refresh):
    # A record id is S<library>C<digits>; the docstring may name the shapes (S246C...).
    assert re.findall(r"\bS\d+C\d+", SCRIPT.read_text()) == []


def test_finds_the_ebook_by_title_not_by_rank(refresh):
    # A sequel's ebook ranks first; the script must still pick the named title.
    state = _state(
        [
            {"representative": "A1", "manifestations": ["A1", "A2"]},
            {"representative": "B1", "manifestations": ["B1", "B2"]},
        ],
        {
            "A1": ("Dune Messiah", "EAUDIOBOOK"),
            "A2": ("Dune Messiah", "EBOOK"),
            "B1": ("Dune", "BK"),
            "B2": ("Dune", "EBOOK"),
        },
    )
    assert refresh.find_ebook_bib_id(state, "Dune") == "B2"


def test_fails_loudly_when_the_title_has_no_ebook(refresh):
    state = _state(
        [{"representative": "B1", "manifestations": ["B1"]}],
        {"B1": ("Dune", "BK")},
    )
    with pytest.raises(SystemExit, match="no EBOOK edition titled 'Dune'"):
        refresh.find_ebook_bib_id(state, "Dune")


def test_finds_the_ebook_in_the_saved_search_fixture(refresh, search_state):
    # The saved search keeps only the bibs its groups reference; the Dune ebook is one.
    bib_id = refresh.find_ebook_bib_id(search_state, "Dune")
    assert search_state["entities"]["bibs"][bib_id]["briefInfo"]["format"] == "EBOOK"


def test_runs_with_a_throwaway_cookie_jar(refresh, monkeypatch):
    # The capture must never load or save the signed-in session's cookie jar.
    seen: list[Config] = []

    async def fake_capture(config: Config) -> None:
        seen.append(config)
        assert not config.cookie_store.exists()

    monkeypatch.setattr(refresh, "capture", fake_capture)
    asyncio.run(refresh.main())

    (config,) = seen
    assert config.cookie_store != Config().cookie_store
    assert config.credentials is None
    assert not config.cookie_store.parent.exists()  # thrown away afterwards
