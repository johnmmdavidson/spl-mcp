from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def search_state() -> dict:
    return load_fixture("search_state.json")


@pytest.fixture
def record_ebook_state() -> dict:
    return load_fixture("record_ebook_state.json")


@pytest.fixture
def holds_state() -> dict:
    return load_fixture("holds_state.json")


@pytest.fixture
def checkouts_state() -> dict:
    return load_fixture("checkouts_state.json")
