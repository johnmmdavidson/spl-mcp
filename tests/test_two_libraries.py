"""One server, two libraries: each registration names its library and uses its own card.

A patron can hold cards at two BiblioCommons libraries whose online accounts
are linked ("connected libraries"). Then the page state at *either* library
carries both cards in ``entities.accounts``, told apart only by ``libId``, and
``library.id`` says which library the page is. All ids and numbers here are
synthetic.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from spl_mcp import catalog, checkouts, ebooks, health, holds, server
from spl_mcp import config as config_mod
from spl_mcp.config import Config, load_config
from spl_mcp.errors import ParseError
from spl_mcp.holds import GatewayContext

# Two linked cards on one profile: library 246 and library 82.
CARD_A = 4000000246
CARD_B = 4000000082
EMAIL_A, EMAIL_B = "a@x.invalid", "b@x.invalid"


def linked_state(library_id: Any) -> dict[str, Any]:
    return {
        "app": {"apiGatewayURL": "https://gw.invalid/v2"},
        "library": {"id": library_id},
        "auth": {"currentUserId": 7, "sessionId": "s", "authToken": "t"},
        "entities": {
            "users": {"7": {"accounts": [CARD_A, CARD_B]}},
            "accounts": {
                str(CARD_A): {"id": CARD_A, "libId": 246, "digitalNotificationEmail": EMAIL_A},
                str(CARD_B): {"id": CARD_B, "libId": 82, "digitalNotificationEmail": EMAIL_B},
            },
            "holds": {},
        },
    }


class TestAccountChoice:
    @pytest.mark.parametrize("library_id, card", [(246, CARD_A), (82, CARD_B)])
    def test_each_library_picks_its_own_card_from_a_linked_profile(self, library_id, card):
        ctx = GatewayContext(linked_state(library_id), Config())
        assert ctx.account_id == card
        assert ctx.notification_email == ("a@x.invalid" if card == CARD_A else "b@x.invalid")

    def test_a_lone_card_from_another_library_is_never_used(self):
        # The page is library 82's, and the only card on it is library 246's.
        # Using it would send this library's hold on another library's card.
        state = linked_state(82)
        del state["entities"]["accounts"][str(CARD_B)]
        state["entities"]["users"]["7"]["accounts"] = [CARD_A]
        ctx = GatewayContext(state, Config())
        assert ctx.account_id is None
        with pytest.raises(ParseError, match="which library account"):
            ctx.require_account_id()

    def test_library_id_and_lib_id_match_across_int_and_string(self):
        state = linked_state("82")
        assert GatewayContext(state, Config()).account_id == CARD_B

    def test_a_lone_card_that_names_no_library_is_still_used(self):
        # Unknown is not wrong: a single card with no libId is the only choice.
        state = linked_state(82)
        state["entities"]["accounts"] = {"1": {"id": 1}}
        state["entities"]["users"]["7"]["accounts"] = [1]
        assert GatewayContext(state, Config()).account_id == 1


class FakeSession:
    """Serves the holds page from a state dict and records gateway requests."""

    def __init__(self, state: dict[str, Any]):
        self.state = state
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    async def authenticated_get(self, url: str, **kwargs: Any):
        return _Resp(_page(self.state))

    async def authenticated_request(self, method: str, url: str, **kwargs: Any):
        body = kwargs.get("json") or {}
        self.requests.append((method, url, body))
        # The hold now exists on the account, as the re-read will show.
        self.state["entities"]["holds"]["70000001"] = {
            "bibTitle": "A Synthetic Title",
            "status": "NOT_YET_AVAILABLE",
            "materialType": "PHYSICAL",
            "holdsPosition": 21,
            "metadataId": body["metadataId"],
            "actions": ["cancel"],
            "pickupLocation": {"code": "1001", "name": "Synthetic Branch"},
        }
        return _Resp(json.dumps({"successCount": 1}))


class _Resp:
    def __init__(self, text: str):
        self.text = text
        self.content = text.encode()
        self.status_code = 200

    def json(self) -> Any:
        return json.loads(self.text)


def _page(state: dict[str, Any]) -> str:
    return f'<html><script data-iso-key="_0">{json.dumps(state)}</script></html>'


@pytest.mark.parametrize(
    "slug, library_id, card", [("seattle", 246, CARD_A), ("kcls", 82, CARD_B)]
)
async def test_place_hold_goes_out_on_this_librarys_card(monkeypatch, slug, library_id, card):
    """Mocked transport only: no request leaves the process."""

    async def fake_fetch(session, config, bib_id):
        return {
            "briefInfo": {"title": "A Synthetic Title", "format": "BK"},
            "policy": {"materialType": "PHYSICAL", "holdable": True},
        }

    monkeypatch.setattr(catalog, "fetch_record", fake_fetch)
    config = Config(slug=slug, read_only=False, default_pickup_branch="1001")
    session = FakeSession(linked_state(library_id))

    result = await holds.place_hold(session, config, "S0C1")

    assert result.ok and result.detail["queue_position"] == 21
    [(method, url, body)] = session.requests
    assert method == "POST"
    assert url == f"https://gw.invalid/v2/libraries/{slug}/holds"
    assert body["accountId"] == card


class TestLibraryName:
    def test_known_slugs_are_named(self):
        assert Config().library_name == "Seattle Public Library"
        assert Config(slug="kcls").library_name == "King County Library System"

    def test_unknown_slug_falls_back_to_the_slug(self):
        assert Config(slug="exampleville").library_name == "exampleville"

    def test_env_names_the_library(self, monkeypatch, clean_env):
        monkeypatch.setenv("SPL_MCP_SLUG", "exampleville")
        monkeypatch.setenv("SPL_MCP_LIBRARY_NAME", "Exampleville Library")
        monkeypatch.setenv("SPL_MCP_KEYCHAIN_SERVICE", "")
        assert load_config().library_name == "Exampleville Library"


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith("SPL_MCP_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # no stray ./config.toml or ./secrets.toml
    monkeypatch.setattr(config_mod, "CONFIG_SEARCH_PATHS", ())
    monkeypatch.setattr(config_mod, "SECRETS_SEARCH_PATHS", ())
    # A Mac whose Keychain answers "not found", recording which service was asked.
    monkeypatch.setattr(config_mod.sys, "platform", "darwin")
    monkeypatch.setattr(config_mod.shutil, "which", lambda name: "/usr/bin/security")
    asked: list[str] = []

    def run(service, account):
        asked.append(service)
        return subprocess.CompletedProcess(args=[], returncode=44, stdout="", stderr="")

    monkeypatch.setattr(config_mod, "_run_security", run)
    return asked


class TestDefaultsFollowTheSlug:
    """A registration that sets only the slug gets that library's values, never Seattle's."""

    def test_seattle_is_unchanged(self, clean_env):
        cfg = load_config()
        assert cfg.base_url == "https://seattle.bibliocommons.com"
        assert cfg.overdrive_library == "spl"
        assert cfg.cookie_store == Path("~/.local/state/spl-mcp/cookies.json").expanduser()
        assert set(clean_env) == {"cornice-spl"}

    def test_kcls_gets_its_own_site_overdrive_card_and_cookie_jar(self, monkeypatch, clean_env):
        monkeypatch.setenv("SPL_MCP_SLUG", "kcls")
        cfg = load_config()
        assert cfg.base_url == "https://kcls.bibliocommons.com"
        assert cfg.overdrive_library == "kcls"
        assert cfg.cookie_store == Path("~/.local/state/spl-mcp/cookies-kcls.json").expanduser()
        assert set(clean_env) == {"cornice-kcls"}
        assert cfg.library_name == "King County Library System"

    def test_explicit_settings_win(self, monkeypatch, clean_env, tmp_path):
        monkeypatch.setenv("SPL_MCP_SLUG", "kcls")
        monkeypatch.setenv("SPL_MCP_BASE_URL", "https://mirror.invalid/")
        monkeypatch.setenv("SPL_MCP_OVERDRIVE_LIBRARY", "other")
        monkeypatch.setenv("SPL_MCP_COOKIE_STORE", str(tmp_path / "jar.json"))
        monkeypatch.setenv("SPL_MCP_KEYCHAIN_SERVICE", "my-service")
        cfg = load_config()
        assert cfg.base_url == "https://mirror.invalid"
        assert cfg.overdrive_library == "other"
        assert cfg.cookie_store == tmp_path / "jar.json"
        assert set(clean_env) == {"my-service"}

    def test_an_unknown_library_never_shares_seattles_cookie_jar(self, monkeypatch, clean_env):
        monkeypatch.setenv("SPL_MCP_SLUG", "exampleville")
        assert load_config().cookie_store.name == "cookies-exampleville.json"


class _Model:
    def model_dump(self, mode: str = "json") -> dict[str, Any]:
        return {"answer": True}


async def _model(*args: Any, **kwargs: Any) -> _Model:
    return _Model()


async def _bib(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return {"briefInfo": {"title": "T", "format": "BK"}, "availability": {}, "policy": {}}


async def _report(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return {"status": "ok", "checks": []}


# Every tool, the module function behind it, a stub for it, and its arguments.
TOOL_STUBS = {
    "search_titles": (catalog, "search_titles", _model, {"query": "q"}),
    "get_title": (catalog, "fetch_record", _bib, {"bib_id": "S0C1"}),
    "check_ebook_formats": (ebooks, "check_ebook_formats", _model, {"bib_id": "S0C1"}),
    "list_holds": (holds, "list_holds", _model, {}),
    "list_checkouts": (checkouts, "list_checkouts", _model, {}),
    "list_ready_to_borrow": (holds, "list_ready_to_borrow", _model, {}),
    "health_check": (health, "run_health", _report, {}),
    "place_hold": (holds, "place_hold", _model, {"bib_id": "S0C1"}),
    "cancel_hold": (holds, "cancel_hold", _model, {"hold_id": "1"}),
    "suspend_hold": (holds, "suspend_hold", _model, {"hold_id": "1", "until": "2099-01-01"}),
    "reactivate_hold": (holds, "reactivate_hold", _model, {"hold_id": "1"}),
}


@pytest.fixture
def kcls_server(monkeypatch):
    monkeypatch.setattr(server, "_config", Config(slug="kcls"))
    monkeypatch.setattr(server, "_session", object())


async def test_every_tool_is_covered_here():
    assert {t.name for t in await server.mcp.list_tools()} == set(TOOL_STUBS)


@pytest.mark.parametrize("tool", sorted(TOOL_STUBS))
async def test_every_answer_names_its_library(monkeypatch, kcls_server, tool):
    module, attr, stub, args = TOOL_STUBS[tool]
    monkeypatch.setattr(module, attr, stub)
    answer = await getattr(server, tool)(**args)
    assert answer["library"] == "King County Library System"
    assert next(iter(answer)) == "library"
    # Additive: the tool's own fields are still there.
    assert ("answer" in answer) or ("status" in answer) or ("bib_id" in answer)


@pytest.mark.parametrize("tool", sorted(TOOL_STUBS))
async def test_every_error_names_its_library(monkeypatch, kcls_server, tool):
    module, attr, _stub, args = TOOL_STUBS[tool]

    async def boom(*a: Any, **k: Any) -> Any:
        raise ParseError("synthetic failure")

    monkeypatch.setattr(module, attr, boom)
    with pytest.raises(Exception) as excinfo:
        await getattr(server, tool)(**args)
    assert str(excinfo.value).startswith("King County Library System: ")


async def test_the_read_only_refusal_names_its_library(monkeypatch):
    # The real guard, not a stub: read_only is on by default.
    monkeypatch.setattr(server, "_config", Config(slug="kcls"))
    monkeypatch.setattr(server, "_session", object())
    with pytest.raises(Exception) as excinfo:
        await server.cancel_hold("1")
    assert str(excinfo.value).startswith("King County Library System: ")
    assert "READ_ONLY" in str(excinfo.value)


async def test_health_report_names_its_library():
    config = Config(slug="kcls", base_url="https://kcls.invalid")

    class Down:
        login_count = 0

        async def get(self, url: str, **kwargs: Any):
            from spl_mcp.errors import UnreachableError

            raise UnreachableError("the library could not be reached at kcls.invalid")

    report = await health.run_health(config, Down())
    assert report["library"] == "King County Library System"
    assert "King County Library System" in report["checks"][0]["detail"]
    assert health.format_report(report).splitlines()[0] == "library: King County Library System"
