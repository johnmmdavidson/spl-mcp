"""Credentials from the macOS Keychain, with every `security` call scripted.

The real Keychain is never touched (see conftest). Secrets used here are fake,
and each error test checks the value never appears in the message.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from spl_mcp import config as config_mod
from spl_mcp.config import load_config, load_credentials
from spl_mcp.errors import ConfigError

FAKE_CARD = "29999000011111"
FAKE_PIN = "8642"


@pytest.fixture(autouse=True)
def _mac_with_clean_env(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith("SPL_MCP_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config_mod.sys, "platform", "darwin")
    monkeypatch.setattr(config_mod.shutil, "which", lambda name: "/usr/bin/security")
    monkeypatch.chdir(tmp_path)  # no stray ./secrets.toml or ./config.toml


def script(monkeypatch, answers):
    """answers: {account: (returncode, stdout)}; records what was asked."""
    asked = []

    def run(service, account):
        asked.append((service, account))
        code, out = answers.get(account, (44, ""))
        return subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr="")

    monkeypatch.setattr(config_mod, "_run_security", run)
    return asked


def test_reads_both_items_from_the_default_service(monkeypatch):
    asked = script(monkeypatch, {"card_number": (0, FAKE_CARD + "\n"), "pin": (0, FAKE_PIN + "\n")})
    creds = load_credentials()
    assert (creds.card_number, creds.pin) == (FAKE_CARD, FAKE_PIN)
    assert asked == [("cornice-spl", "card_number"), ("cornice-spl", "pin")]
    assert FAKE_PIN not in repr(creds) and FAKE_CARD not in repr(creds)


def test_absent_items_fall_through_to_the_secrets_file(monkeypatch, tmp_path):
    script(monkeypatch, {})
    path = tmp_path / "s.toml"
    path.write_text('[credentials]\ncard_number = "filecard"\npin = "filepin"\n')
    assert load_credentials(path).card_number == "filecard"


def test_absent_items_and_no_file_is_none_not_an_error(monkeypatch):
    script(monkeypatch, {})
    assert load_credentials() is None


def test_env_override_wins_and_the_keychain_is_not_read(monkeypatch):
    asked = script(monkeypatch, {"card_number": (0, FAKE_CARD), "pin": (0, FAKE_PIN)})
    monkeypatch.setenv("SPL_MCP_CARD_NUMBER", "envcard")
    monkeypatch.setenv("SPL_MCP_PIN", "wrongpin")
    creds = load_credentials()
    assert (creds.card_number, creds.pin) == ("envcard", "wrongpin")
    assert asked == []


def test_empty_service_turns_the_keychain_off(monkeypatch):
    asked = script(monkeypatch, {"card_number": (0, FAKE_CARD), "pin": (0, FAKE_PIN)})
    monkeypatch.setenv("SPL_MCP_KEYCHAIN_SERVICE", "")
    assert load_credentials() is None
    assert asked == []


def test_half_a_login_is_a_plain_error_without_the_value(monkeypatch):
    script(monkeypatch, {"card_number": (0, FAKE_CARD)})
    with pytest.raises(ConfigError, match="no 'pin' item") as err:
        load_credentials()
    assert FAKE_CARD not in str(err.value)


def test_unreadable_keychain_is_not_reported_as_absent(monkeypatch):
    # exit 51 = access denied / user cancelled: unknown, not "no credentials".
    script(monkeypatch, {"card_number": (51, FAKE_CARD), "pin": (51, FAKE_PIN)})
    with pytest.raises(ConfigError, match="locked or access denied") as err:
        load_credentials()
    assert FAKE_CARD not in str(err.value) and FAKE_PIN not in str(err.value)


def test_load_config_keeps_the_error_so_public_tools_still_work(monkeypatch):
    script(monkeypatch, {"card_number": (51, ""), "pin": (51, "")})
    cfg = load_config()
    assert cfg.credentials is None
    with pytest.raises(ConfigError, match="locked or access denied"):
        cfg.require_credentials()
