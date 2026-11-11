"""One library account, one live session: two servers must never clash.

Observed live on 2026-11-10 (COR-375): Seattle and KCLS are BiblioCommons
"connected libraries", and a patron with linked cards has ONE live session.
A sign-in at KCLS ends the session Seattle's server is holding, and the other
way round. When the Seattle server's sign-in was accepted (302, access cookie
set) and the KCLS sign-in landed before Seattle followed its redirect, the
redirect ended on the login page and the server said the PIN was wrong.

The fake library below models exactly that: one live access token per
account, across both hosts. All credentials are synthetic.
"""

from __future__ import annotations

import asyncio
import fcntl
import itertools
import os
from urllib.parse import parse_qs

import httpx
import pytest

from spl_mcp.config import Config, Credentials
from spl_mcp.errors import AuthError, SessionReplacedError, UpstreamError
from spl_mcp.session import SplSession

from .test_session import HOLDS_PAGE, LOGIN_PAGE

SPL = "https://seattle.bibliocommons.com"
KCLS = "https://kcls.bibliocommons.com"
GOOD_PIN = "4321"


class OneSessionLibrary:
    """Two library hosts sharing one account, which has one live session."""

    def __init__(self) -> None:
        self.live: str | None = None
        self.tokens = itertools.count(1)
        self.posts: list[tuple[str, bool]] = []  # (host, accepted)
        # Raised when a POST is accepted, so a test can hold the other host's
        # redirect open until another sign-in has had its chance to land.
        self.accepted = {SPL: asyncio.Event(), KCLS: asyncio.Event()}

    async def handler(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)  # let other tasks run, as a network would
        host = f"https://{request.url.host}"
        path = request.url.path
        if path == "/user/login" and request.method == "GET":
            return httpx.Response(200, html=LOGIN_PAGE)
        if path == "/user/login" and request.method == "POST":
            form = parse_qs(request.content.decode())
            ok = form.get("user_pin") == [GOOD_PIN]
            self.posts.append((host, ok))
            if not ok:
                return httpx.Response(200, html=LOGIN_PAGE)  # re-rendered form
            token = f"t{next(self.tokens)}"
            self.live = token  # every earlier session, at either host, ends
            self.accepted[host].set()
            return httpx.Response(
                302,
                headers=[
                    ("Location", "/user_dashboard"),
                    ("Set-Cookie", f"bc_access_token={token}; Path=/"),
                ],
            )
        # A member page: served only to the live session.
        if path == "/user_dashboard" and host == SPL:
            # Hold Seattle's redirect open briefly: long enough for an
            # unserialised KCLS sign-in to land, which is the live race.
            other = self.accepted[KCLS]
            with __import__("contextlib").suppress(TimeoutError):
                await asyncio.wait_for(other.wait(), timeout=0.3)
        if request.headers.get("cookie", "").find(f"bc_access_token={self.live}") >= 0:
            if path == "/v2/holds":
                return httpx.Response(200, html=HOLDS_PAGE)
            return httpx.Response(200, html="<html>dashboard</html>")
        return httpx.Response(302, headers={"Location": "/user/login"})


def server(tmp_path, library: OneSessionLibrary, base: str, pin: str = GOOD_PIN) -> SplSession:
    slug = "seattle" if base == SPL else "kcls"
    config = Config(
        slug=slug,
        base_url=base,
        credentials=Credentials(card_number="CARD-SYNTH", pin=pin),
        cookie_store=tmp_path / f"cookies-{slug}.json",
        session_lock=tmp_path / "session.lock",  # one lock, two cookie files
        timeout_seconds=5,
    )
    return SplSession(config, transport=httpx.MockTransport(library.handler))


async def test_two_servers_signing_in_at_once_both_get_their_holds(tmp_path):
    """Fails on 40eeea4: Seattle reports a wrong PIN although the PIN is right."""
    library = OneSessionLibrary()
    spl, kcls = server(tmp_path, library, SPL), server(tmp_path, library, KCLS)

    async def kcls_after_spl_posts():
        # KCLS signs in the moment Seattle's sign-in has been accepted.
        await library.accepted[SPL].wait()
        return await kcls.authenticated_get(f"{KCLS}/v2/holds")

    spl_resp, kcls_resp = await asyncio.gather(
        spl.authenticated_get(f"{SPL}/v2/holds"), kcls_after_spl_posts()
    )
    assert 'data-iso-key="_0"' in spl_resp.text
    assert 'data-iso-key="_0"' in kcls_resp.text
    # Every sign-in used the right PIN and was accepted.
    assert library.posts and all(ok for _, ok in library.posts)


async def test_many_parallel_calls_never_report_a_bad_pin(tmp_path):
    library = OneSessionLibrary()
    library.accepted[KCLS].set()  # no held redirect: plain interleaving
    sessions = [server(tmp_path / str(i), library, b) for i in range(5) for b in (SPL, KCLS)]
    for s in sessions:
        s.config.session_lock = tmp_path / "session.lock"
    results = await asyncio.gather(
        *(s.authenticated_get(f"{s.config.base_url}/v2/holds") for s in sessions),
        return_exceptions=True,
    )
    assert [r for r in results if isinstance(r, Exception)] == []


async def test_an_accepted_sign_in_whose_session_ends_at_once_is_not_a_bad_pin(tmp_path):
    """Another device signs in between the accepted POST and its redirect."""
    library = OneSessionLibrary()
    spl = server(tmp_path, library, SPL)
    real = library.handler

    async def interloper(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user_dashboard":
            library.live = "someone-else"  # a browser signed in meanwhile
        return await real(request)

    library.handler = interloper  # type: ignore[method-assign]
    spl._transport = httpx.MockTransport(interloper)
    with pytest.raises(SessionReplacedError) as excinfo:
        await spl.login()
    message = str(excinfo.value)
    assert "PIN is fine" in message
    assert "rejected" not in message
    assert library.posts == [(SPL, True)]  # one sign-in; not retried


@pytest.mark.parametrize("call", ["login", "authenticated_get"])
async def test_a_true_bad_pin_is_reported_once_and_never_retried(tmp_path, call):
    library = OneSessionLibrary()
    spl = server(tmp_path, library, SPL, pin="0000")
    with pytest.raises(AuthError) as excinfo:
        if call == "login":
            await spl.login()
        else:
            await spl.authenticated_get(f"{SPL}/v2/holds")
    assert not isinstance(excinfo.value, SessionReplacedError)
    assert "rejected" in str(excinfo.value)
    assert library.posts == [(SPL, False)]


async def test_a_held_lock_times_out_plainly_and_sends_nothing(tmp_path):
    library = OneSessionLibrary()
    spl = server(tmp_path, library, SPL)
    spl.config.timeout_seconds = 0.05
    fd = os.open(tmp_path / "session.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # another server, mid sign-in
        with pytest.raises(UpstreamError, match="another library server"):
            await spl.authenticated_get(f"{SPL}/v2/holds")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert library.posts == []
