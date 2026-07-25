"""Login, session persistence, and re-authentication.

Exercised against a mock transport rather than the live site, so these run
offline and need no library account. The login page markup below mirrors the
real one: a randomised form id, the Rails CSRF token, and the hidden fields
that must be carried through untouched.
"""

from __future__ import annotations

import json

import httpx
import pytest

from spl_mcp.config import Config, Credentials
from spl_mcp.errors import AuthError
from spl_mcp.session import SplSession

BASE = "https://seattle.bibliocommons.com"

LOGIN_PAGE = """
<html><body>
  <form action="/search" method="get" id="desktop_search_form">
    <input type="search" name="q">
  </form>
  <form action="/user/login?destination=%2Fuser_dashboard" method="post"
        id="login_58jndlpq2suuusai9cn0jsh5hl">
    <input type="hidden" name="utf8" value="&#x2713;">
    <input type="hidden" name="authenticity_token" value="TOKEN-FROM-PAGE">
    <input type="text" name="name">
    <input type="password" name="user_pin" value="">
    <input type="checkbox" name="remember_me" value="true">
    <input type="hidden" name="local" value="false">
    <input type="submit" name="commit" value="Log In">
  </form>
</body></html>
"""

HOLDS_PAGE = """
<html><body>
  <div data-iso-key="_0">rendered</div>
  <script data-iso-key="_0">{"entities": {"holds": {}}}</script>
</body></html>
"""


def make_config(tmp_path, **kwargs) -> Config:
    return Config(
        credentials=Credentials(card_number="CARD123", pin="4321"),
        cookie_store=tmp_path / "cookies.json",
        **kwargs,
    )


class Recorder:
    """A mock transport that scripts responses and records what was sent."""

    def __init__(self, *, authed_after_login=True, login_page=LOGIN_PAGE):
        self.requests: list[httpx.Request] = []
        self.posts: list[dict] = []
        self.authed = False
        self.authed_after_login = authed_after_login
        self.login_page = login_page
        self.session_expired_once = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if path == "/user/login" and request.method == "GET":
            return httpx.Response(200, html=self.login_page)

        if path == "/user/login" and request.method == "POST":
            body = dict(
                pair.split("=", 1)
                for pair in request.content.decode().split("&")
                if "=" in pair
            )
            self.posts.append(body)
            if not self.authed_after_login:
                return httpx.Response(200, html=self.login_page)
            self.authed = True
            # The real site answers a successful login with a redirect to the
            # destination, setting the session cookie on the way.
            return httpx.Response(
                302,
                headers={
                    "location": "/user_dashboard",
                    "set-cookie": "bc_session=abc123; Path=/; Secure",
                },
            )

        if path == "/user_dashboard":
            return httpx.Response(200, html="<html>dashboard</html>")

        if path == "/v2/holds":
            if not self.authed:
                return httpx.Response(
                    200, html=self.login_page, request=request
                )
            return httpx.Response(200, html=HOLDS_PAGE)

        return httpx.Response(404, text="not found")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class TestLogin:
    async def test_scrapes_csrf_token_and_posts_credentials(self, tmp_path):
        rec = Recorder()
        session = SplSession(make_config(tmp_path), transport=rec.transport())
        try:
            await session.authenticated_get(f"{BASE}/v2/holds")
        finally:
            await session.aclose()

        assert len(rec.posts) == 1
        posted = rec.posts[0]
        # Rails rejects the POST without the token lifted from the GET.
        assert posted["authenticity_token"] == "TOKEN-FROM-PAGE"
        assert posted["name"] == "CARD123"
        assert posted["user_pin"] == "4321"

    async def test_requests_a_durable_session(self, tmp_path):
        rec = Recorder()
        session = SplSession(make_config(tmp_path), transport=rec.transport())
        try:
            await session.authenticated_get(f"{BASE}/v2/holds")
        finally:
            await session.aclose()
        assert rec.posts[0]["remember_me"] == "true"

    async def test_carries_hidden_fields_through_untouched(self, tmp_path):
        rec = Recorder()
        session = SplSession(make_config(tmp_path), transport=rec.transport())
        try:
            await session.authenticated_get(f"{BASE}/v2/holds")
        finally:
            await session.aclose()
        posted = rec.posts[0]
        assert posted["local"] == "false"
        assert "utf8" in posted

    async def test_rejected_login_fails_loudly(self, tmp_path):
        rec = Recorder(authed_after_login=False)
        session = SplSession(make_config(tmp_path), transport=rec.transport())
        try:
            with pytest.raises(AuthError, match="rejected"):
                await session.authenticated_get(f"{BASE}/v2/holds")
        finally:
            await session.aclose()

    async def test_rejected_login_is_not_retried(self, tmp_path):
        # Repeated bad PINs lock a library account, so one failure must stop.
        rec = Recorder(authed_after_login=False)
        session = SplSession(make_config(tmp_path), transport=rec.transport())
        try:
            with pytest.raises(AuthError):
                await session.authenticated_get(f"{BASE}/v2/holds")
        finally:
            await session.aclose()
        assert len(rec.posts) == 1

    async def test_bot_wall_is_detected_and_named(self, tmp_path):
        wall = LOGIN_PAGE + '<script src="https://www.recaptcha.net/x.js"></script>'
        rec = Recorder(login_page=wall)
        session = SplSession(make_config(tmp_path), transport=rec.transport())
        try:
            with pytest.raises(AuthError, match="bot challenge"):
                await session.authenticated_get(f"{BASE}/v2/holds")
        finally:
            await session.aclose()

    async def test_missing_csrf_token_fails_loudly(self, tmp_path):
        page = LOGIN_PAGE.replace('value="TOKEN-FROM-PAGE"', 'value=""')
        rec = Recorder(login_page=page)
        session = SplSession(make_config(tmp_path), transport=rec.transport())
        try:
            with pytest.raises(AuthError, match="authenticity_token"):
                await session.authenticated_get(f"{BASE}/v2/holds")
        finally:
            await session.aclose()

    async def test_missing_credentials_is_a_clear_error(self, tmp_path):
        config = Config(cookie_store=tmp_path / "cookies.json")  # no credentials
        session = SplSession(config, transport=Recorder().transport())
        try:
            with pytest.raises(Exception, match="SPL_MCP_CARD_NUMBER"):
                await session.authenticated_get(f"{BASE}/v2/holds")
        finally:
            await session.aclose()


class TestReauthentication:
    async def test_expired_session_triggers_one_transparent_relogin(self, tmp_path):
        rec = Recorder()
        session = SplSession(make_config(tmp_path), transport=rec.transport())
        try:
            await session.authenticated_get(f"{BASE}/v2/holds")
            assert len(rec.posts) == 1

            # Session dies server-side; the next call must recover on its own.
            rec.authed = False
            resp = await session.authenticated_get(f"{BASE}/v2/holds")
            assert "data-iso-key" in resp.text
            assert len(rec.posts) == 2
        finally:
            await session.aclose()

    async def test_gives_up_rather_than_looping_when_relogin_does_not_help(self, tmp_path):
        rec = Recorder()
        session = SplSession(make_config(tmp_path), transport=rec.transport())

        # Login "succeeds" but the member page still bounces to the login form.
        original = rec.handler

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v2/holds":
                rec.requests.append(request)
                return httpx.Response(200, html=LOGIN_PAGE)
            return original(request)

        session._transport = httpx.MockTransport(handler)
        try:
            with pytest.raises(AuthError, match="still redirected"):
                await session.authenticated_get(f"{BASE}/v2/holds")
        finally:
            await session.aclose()


class TestCookiePersistence:
    async def test_cookies_survive_a_restart(self, tmp_path):
        config = make_config(tmp_path)
        rec = Recorder()

        session = SplSession(config, transport=rec.transport())
        try:
            await session.authenticated_get(f"{BASE}/v2/holds")
        finally:
            await session.aclose()

        assert config.cookie_store.is_file()
        stored = json.loads(config.cookie_store.read_text())
        assert any(c["name"] == "bc_session" for c in stored["cookies"])

        # A fresh session (a new stdio spawn) must not log in again.
        rec2 = Recorder()
        rec2.authed = True
        session2 = SplSession(config, transport=rec2.transport())
        try:
            await session2.authenticated_get(f"{BASE}/v2/holds")
        finally:
            await session2.aclose()
        assert rec2.posts == []

    async def test_cookie_file_is_not_world_readable(self, tmp_path):
        # Session cookies are credentials.
        config = make_config(tmp_path)
        rec = Recorder()
        session = SplSession(config, transport=rec.transport())
        try:
            await session.authenticated_get(f"{BASE}/v2/holds")
        finally:
            await session.aclose()
        assert config.cookie_store.stat().st_mode & 0o077 == 0

    async def test_expired_cookies_are_discarded_on_load(self, tmp_path):
        config = make_config(tmp_path)
        config.cookie_store.parent.mkdir(parents=True, exist_ok=True)
        config.cookie_store.write_text(
            json.dumps(
                {
                    "cookies": [
                        {
                            "name": "stale",
                            "value": "x",
                            "domain": "seattle.bibliocommons.com",
                            "path": "/",
                            "secure": True,
                            "expires": 1_000_000,  # long past
                        }
                    ]
                }
            )
        )
        session = SplSession(config, transport=Recorder().transport())
        client = await session.client()
        try:
            assert "stale" not in {c.name for c in client.cookies.jar}
        finally:
            await session.aclose()

    async def test_corrupt_cookie_store_is_survivable(self, tmp_path):
        config = make_config(tmp_path)
        config.cookie_store.parent.mkdir(parents=True, exist_ok=True)
        config.cookie_store.write_text("{not json")

        rec = Recorder()
        session = SplSession(config, transport=rec.transport())
        try:
            # Falls back to a fresh login rather than crashing.
            await session.authenticated_get(f"{BASE}/v2/holds")
        finally:
            await session.aclose()
        assert len(rec.posts) == 1
