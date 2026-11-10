"""Plain errors and the health check, against scripted transports.

Each failure the health check must name is produced here on purpose: the
library unreachable, the library refusing, a rejected login, no credentials.
"""

from __future__ import annotations

import httpx
import pytest

from spl_mcp import ebooks
from spl_mcp.config import Config, Credentials
from spl_mcp.errors import UnreachableError, UpstreamError
from spl_mcp.health import format_report, run_health
from spl_mcp.session import SplSession

from .test_session import BASE, Recorder


def cfg(tmp_path, *, creds=True) -> Config:
    return Config(
        credentials=Credentials(card_number="CARD123", pin="4321") if creds else None,
        cookie_store=tmp_path / "cookies.json",
    )


def raising(exc_type):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc_type("boom", request=request)

    return httpx.MockTransport(handler)


# ------------------------------------------------------------ plain errors


async def test_unreachable_is_one_plain_line(tmp_path):
    session = SplSession(cfg(tmp_path), transport=raising(httpx.ConnectError))
    try:
        with pytest.raises(UnreachableError) as err:
            await session.get(f"{BASE}/v2/search")
    finally:
        await session.aclose()
    msg = str(err.value)
    assert msg == "the library could not be reached at seattle.bibliocommons.com (ConnectError)"


async def test_unreachable_during_login_is_plain_too(tmp_path):
    session = SplSession(cfg(tmp_path), transport=raising(httpx.ConnectTimeout))
    try:
        with pytest.raises(UnreachableError, match="could not be reached"):
            await session.authenticated_get(f"{BASE}/v2/holds")
    finally:
        await session.aclose()


async def test_a_write_that_timed_out_after_sending_says_it_may_have_applied(tmp_path):
    session = SplSession(cfg(tmp_path), transport=raising(httpx.ReadTimeout))
    session._logged_in_hint = True  # skip the login so the POST itself fails
    try:
        with pytest.raises(UnreachableError, match="may or may not have taken effect"):
            await session.authenticated_request("POST", f"{BASE}/v2/x")
    finally:
        await session.aclose()


async def test_a_write_that_never_connected_does_not_say_it_may_have_applied(tmp_path):
    session = SplSession(cfg(tmp_path), transport=raising(httpx.ConnectError))
    session._logged_in_hint = True
    try:
        with pytest.raises(UnreachableError) as err:
            await session.authenticated_request("POST", f"{BASE}/v2/x")
    finally:
        await session.aclose()
    assert "taken effect" not in str(err.value)


async def test_refusal_is_named_as_a_refusal(tmp_path):
    transport = httpx.MockTransport(lambda r: httpx.Response(503, text="down"))
    session = SplSession(cfg(tmp_path), transport=transport)
    try:
        with pytest.raises(UpstreamError, match="the library refused GET .* with HTTP 503"):
            await session.get(f"{BASE}/v2/search")
    finally:
        await session.aclose()


async def test_overdrive_unreachable_reads_as_unknown_not_absent(tmp_path):
    session = SplSession(cfg(tmp_path), transport=raising(httpx.ConnectError))
    try:
        assert await ebooks._thunder_get(session, cfg(tmp_path), "/media/1") is None
    finally:
        await session.aclose()


# ------------------------------------------------------------ health check


async def _health(tmp_path, transport, *, creds=True):
    config = cfg(tmp_path, creds=creds)
    session = SplSession(config, transport=transport)
    try:
        return await run_health(config, session)
    finally:
        await session.aclose()


def by_name(report):
    return {c["check"]: c for c in report["checks"]}


async def test_health_all_green(tmp_path):
    report = await _health(tmp_path, Recorder().transport())
    assert report["status"] == "ok"
    assert by_name(report)["login"]["ok"] is True
    assert "logged in with the card and PIN" in by_name(report)["login"]["detail"]


async def test_health_bad_login(tmp_path):
    report = await _health(tmp_path, Recorder(authed_after_login=False).transport())
    login = by_name(report)["login"]
    assert report["status"] == "degraded"
    assert login["ok"] is False and "login was rejected" in login["detail"]
    assert "4321" not in format_report(report)


async def test_health_library_unreachable(tmp_path):
    report = await _health(tmp_path, raising(httpx.ConnectError))
    checks = by_name(report)
    assert report["status"] == "down"
    assert checks["server"]["ok"] is True
    assert checks["library"]["ok"] is False
    assert checks["login"]["ok"] is None  # not checked is not a pass


async def test_health_without_credentials_is_not_green(tmp_path):
    report = await _health(tmp_path, Recorder().transport(), creds=False)
    assert report["status"] == "degraded"
    assert by_name(report)["login"]["ok"] is None
    assert "[ skip ] login" in format_report(report)


async def test_health_says_logged_in_when_a_stale_cookie_forced_a_fresh_login(tmp_path):
    # Anonymous cookies from public pages must not read as "saved session valid".
    (tmp_path / "cookies.json").write_text(
        '{"cookies": [{"name": "anon", "value": "x", "domain": "seattle.bibliocommons.com"}]}'
    )
    report = await _health(tmp_path, Recorder().transport())
    assert "logged in with the card and PIN" in by_name(report)["login"]["detail"]


async def test_health_says_saved_session_when_no_login_happened(tmp_path):
    rec = Recorder()
    rec.authed = True  # the saved cookie is still good
    (tmp_path / "cookies.json").write_text(
        '{"cookies": [{"name": "bc_session", "value": "x", "domain": "seattle.bibliocommons.com"}]}'
    )
    report = await _health(tmp_path, rec.transport())
    assert by_name(report)["login"]["detail"].startswith("saved session still valid")
    assert rec.posts == []
