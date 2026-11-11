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


@pytest.fixture(autouse=True)
def _no_real_keychain(monkeypatch):
    """No test may read John's real Keychain. Every Keychain lookup sees
    'item not found' unless a test scripts its own answer."""
    import subprocess

    from spl_mcp import config as config_mod

    def not_found(service, account):
        return subprocess.CompletedProcess(args=[], returncode=44, stdout="", stderr="")

    monkeypatch.setattr(config_mod, "_run_security", not_found)


@pytest.fixture(autouse=True)
def _no_real_state_dir(monkeypatch, tmp_path_factory):
    """No test may touch ~/.local/state/spl-mcp: the session lock, the cookie
    files. `~` resolves to a throwaway home for every test."""
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
