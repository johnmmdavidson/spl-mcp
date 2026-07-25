"""Config and credential loading, including the safe defaults."""

from __future__ import annotations

import pytest

from spl_mcp.config import Config, load_config, load_credentials
from spl_mcp.errors import ConfigError

CONFIG_TOML = """
[library]
slug = "vpl"
base_url = "https://vpl.bibliocommons.com/"

[holds]
default_pickup_branch = "MAIN"

[safety]
read_only = false

[ebooks]
use_overdrive_thunder = false
overdrive_library = "vpl"

[http]
timeout_seconds = 12.5
"""

SECRETS_TOML = """
[credentials]
card_number = "1234567890"
pin = "9876"
"""


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("SPL_MCP_"):
            monkeypatch.delenv(key, raising=False)


class TestDefaults:
    def test_read_only_is_engaged_by_default(self):
        # The guardrail must be on unless someone deliberately turns it off.
        assert Config().read_only is True

    def test_defaults_target_seattle(self):
        cfg = Config()
        assert cfg.slug == "seattle"
        assert cfg.search_url == "https://seattle.bibliocommons.com/v2/search"
        assert cfg.holds_url == "https://seattle.bibliocommons.com/v2/holds"
        assert cfg.record_url("S1") == "https://seattle.bibliocommons.com/v2/record/S1"

    def test_gateway_url_is_library_scoped(self):
        assert (
            Config(slug="vpl").holds_gateway_url
            == "https://gateway.bibliocommons.com/v2/libraries/vpl/holds"
        )

    def test_user_agent_is_honest_and_contactable(self):
        ua = Config().user_agent
        assert "spl-mcp" in ua
        assert "http" in ua  # carries a contact URL


class TestFileLoading:
    def test_reads_every_section(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(CONFIG_TOML)
        cfg = load_config(path)
        assert cfg.slug == "vpl"
        assert cfg.base_url == "https://vpl.bibliocommons.com"  # trailing slash stripped
        assert cfg.default_pickup_branch == "MAIN"
        assert cfg.read_only is False
        assert cfg.use_overdrive_thunder is False
        assert cfg.timeout_seconds == 12.5

    def test_malformed_toml_is_a_clear_error(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text("[library\nslug = ")
        with pytest.raises(ConfigError, match="not valid TOML"):
            load_config(path)


class TestEnvOverrides:
    def test_env_beats_file(self, tmp_path, monkeypatch):
        path = tmp_path / "config.toml"
        path.write_text(CONFIG_TOML)
        monkeypatch.setenv("SPL_MCP_SLUG", "envslug")
        monkeypatch.setenv("SPL_MCP_READ_ONLY", "true")
        cfg = load_config(path)
        assert cfg.slug == "envslug"
        assert cfg.read_only is True

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_read_only_values(self, raw, monkeypatch):
        monkeypatch.setenv("SPL_MCP_READ_ONLY", raw)
        assert load_config().read_only is True

    @pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off"])
    def test_falsy_read_only_values(self, raw, monkeypatch):
        monkeypatch.setenv("SPL_MCP_READ_ONLY", raw)
        assert load_config().read_only is False

    def test_ambiguous_read_only_value_is_rejected(self, monkeypatch):
        # Never silently treat a typo as "false" -- that would disarm the guardrail.
        monkeypatch.setenv("SPL_MCP_READ_ONLY", "maybe")
        with pytest.raises(ConfigError, match="boolean"):
            load_config()


class TestCredentials:
    def test_loads_from_file(self, tmp_path):
        path = tmp_path / "secrets.toml"
        path.write_text(SECRETS_TOML)
        creds = load_credentials(path)
        assert creds.card_number == "1234567890"
        assert creds.pin == "9876"

    def test_env_beats_file(self, tmp_path, monkeypatch):
        path = tmp_path / "secrets.toml"
        path.write_text(SECRETS_TOML)
        monkeypatch.setenv("SPL_MCP_CARD_NUMBER", "envcard")
        monkeypatch.setenv("SPL_MCP_PIN", "envpin")
        creds = load_credentials(path)
        assert creds.card_number == "envcard"

    def test_incomplete_file_is_a_clear_error(self, tmp_path):
        path = tmp_path / "secrets.toml"
        path.write_text('[credentials]\ncard_number = "123"\n')
        with pytest.raises(ConfigError, match="pin"):
            load_credentials(path)

    def test_missing_credentials_gives_actionable_message(self):
        with pytest.raises(ConfigError, match="SPL_MCP_CARD_NUMBER"):
            Config().require_credentials()

    def test_credentials_are_not_exposed_in_repr(self, tmp_path):
        # A traceback must never leak a PIN.
        path = tmp_path / "secrets.toml"
        path.write_text(SECRETS_TOML)
        creds = load_credentials(path)
        assert "9876" not in repr(creds)
        assert "1234567890" not in repr(creds)
