"""Authenticated HTTP session against BiblioCommons.

There is no browser here and no headless Chromium -- the whole server is one
``httpx`` client. Login is an ordinary Rails form POST guarded by a CSRF token
we scrape from the login page; the resulting cookies are persisted to disk so a
session outlives individual stdio spawns of this server.

Concurrency: a single ``asyncio.Lock`` serialises everything that touches the
authenticated session. This is a single-user server, but the agent may well
fire several tool calls at once and a shared cookie jar plus a re-login race is
not safe under concurrency.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.cookiejar
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .config import Config
from .errors import AuthError, SessionReplacedError, UnreachableError, UpstreamError

try:
    import fcntl
except ImportError:  # pragma: no cover - not macOS or Linux
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger("spl_mcp.session")

# Terms that, if they appear on the login page, mean a bot wall went up and the
# form-POST approach is finished. None of these were present during the spike.
_BOT_WALL_MARKERS = ("recaptcha", "hcaptcha", "datadome", "perimeterx", "cf-turnstile")

# Marks a non-GET request that cannot change the account (the login form POST).
CHANGES_NOTHING = "spl_mcp.changes_nothing"

# The cookie a sign-in sets when the library accepts the card and PIN. A
# rejected sign-in re-renders the form with HTTP 200 and sets no such cookie.
_ACCEPTED_LOGIN_COOKIE = "bc_access_token"

# How long to wait for another server's sign-in or account request to finish.
_LOCK_POLL_SECONDS = 0.05

# Transport failures where the request provably never reached the server.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.UnsupportedProtocol)


class PlainErrorClient(httpx.AsyncClient):
    """An httpx client whose every network failure reads as one plain line.

    Every request this server makes goes through ``send`` -- the library, the
    holds gateway, OverDrive -- including redirects and the response body, so
    the wording lives in one place. It overrides ``send`` rather than wrapping
    the transport, which would switch off httpx's environment proxy routing.
    A request that may change something (anything but GET) and failed after
    it was sent is reported as possibly applied: it must not be retried blindly.
    """

    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        try:
            return await super().send(request, **kwargs)
        except httpx.TransportError as exc:
            raise UnreachableError(self._describe(request, exc)) from exc

    def _describe(self, request: httpx.Request, exc: httpx.TransportError) -> str:
        if isinstance(exc, httpx.TimeoutException) and not isinstance(exc, httpx.ConnectTimeout):
            why = "timed out"
            if isinstance(self.timeout.read, int | float):
                why = f"no answer within {self.timeout.read:g}s"
        else:
            why = type(exc).__name__
        msg = f"the library could not be reached at {request.url.host} ({why})"
        changes_nothing = request.method == "GET" or request.extensions.get(CHANGES_NOTHING)
        if not changes_nothing and not isinstance(exc, _NOT_SENT):
            msg += (
                f"; the {request.method} may or may not have taken effect, "
                "so check list_holds before trying again"
            )
        return msg


class SplSession:
    """Owns the HTTP client, the cookie jar, and the login lifecycle."""

    def __init__(self, config: Config, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        # Only the tests pass a transport; it lets the login and re-login paths
        # be exercised without a network or a real library account.
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._logged_in_hint = False
        # Successful credential logins this session has made. Observed, so the
        # health check can say whether a login just happened.
        self.login_count = 0

    # ------------------------------------------------------------ client

    def _new_client(self) -> httpx.AsyncClient:
        client = PlainErrorClient(
            headers={
                "User-Agent": self.config.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=self.config.timeout_seconds,
            follow_redirects=True,
            transport=self._transport,
        )
        self._load_cookies(client)
        return client

    async def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._new_client()
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            self._save_cookies(self._client)
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------ account lock

    @contextlib.asynccontextmanager
    async def _account_lock(self) -> AsyncIterator[None]:
        """Hold the machine-wide session lock (``config.session_lock``).

        One library account has one live session, and connected libraries
        (Seattle and KCLS) share it: a sign-in at either ends the session the
        other server holds. Every server on this machine takes this lock for
        each sign-in and each authenticated request, so a sign-in never lands
        between another server's sign-in and its use of that session. The
        in-process asyncio lock is taken first, so one process waits here at
        most once.
        """
        if fcntl is None:
            yield
            return
        path = self.config.session_lock
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            # Generous: a holder makes at most a few requests, each bounded by
            # the HTTP timeout.
            deadline = time.monotonic() + 6 * self.config.timeout_seconds
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise UpstreamError(
                            "another library server on this machine has been signed in "
                            f"and busy for over {6 * self.config.timeout_seconds:g}s "
                            f"(lock {path}); nothing was sent. Try again shortly."
                        ) from None
                    await asyncio.sleep(_LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # ------------------------------------------------------ cookie store

    def _load_cookies(self, client: httpx.AsyncClient) -> None:
        path = self.config.cookie_store
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("ignoring unreadable cookie store %s: %s", path, exc)
            return

        now = time.time()
        loaded = 0
        for c in raw.get("cookies", []):
            expires = c.get("expires")
            if expires is not None and expires < now:
                continue  # already dead, don't bother
            client.cookies.jar.set_cookie(
                http.cookiejar.Cookie(
                    version=0,
                    name=c["name"],
                    value=c["value"],
                    port=None,
                    port_specified=False,
                    domain=c.get("domain", ""),
                    domain_specified=bool(c.get("domain")),
                    domain_initial_dot=str(c.get("domain", "")).startswith("."),
                    path=c.get("path", "/"),
                    path_specified=True,
                    secure=bool(c.get("secure", True)),
                    expires=expires,
                    discard=expires is None,
                    comment=None,
                    comment_url=None,
                    rest={},
                    rfc2109=False,
                )
            )
            loaded += 1
        if loaded:
            self._logged_in_hint = True
            log.debug("loaded %d cookies from %s", loaded, path)

    def _save_cookies(self, client: httpx.AsyncClient) -> None:
        path = self.config.cookie_store
        payload = {
            "saved_at": time.time(),
            "cookies": [
                {
                    "name": c.name,
                    "value": c.value,
                    "domain": c.domain,
                    "path": c.path,
                    "secure": c.secure,
                    "expires": c.expires,
                }
                for c in client.cookies.jar
            ],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.chmod(0o600)  # session cookies are credentials
            tmp.replace(path)
        except OSError as exc:
            # Not fatal: we just lose session reuse across spawns.
            log.warning("could not persist cookies to %s: %s", path, exc)

    def forget_session(self) -> None:
        """Drop cookies in memory and on disk. Used when a session goes bad."""
        if self._client is not None:
            self._client.cookies.clear()
        self._logged_in_hint = False
        try:
            self.config.cookie_store.unlink(missing_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------- login

    @staticmethod
    def _looks_like_login(response: httpx.Response) -> bool:
        """True when we got the login page instead of the content we asked for.

        The presence of the login form is the reliable signal, not the URL. A
        successful login POST can legitimately settle on a ``/user/login`` URL
        (the form posts to it with a ``destination`` query), so treating the
        path alone as failure would misread a success. The URL is used only
        when there is no body to inspect.
        """
        body = response.text[:200_000]
        if 'name="user_pin"' in body:
            return True
        if not body.strip():
            return "/user/login" in urlparse(str(response.url)).path
        return False

    async def _login(self, client: httpx.AsyncClient) -> None:
        """Scrape the CSRF token, POST the credentials, verify it took."""
        creds = self.config.require_credentials()

        page = await client.get(self.config.login_url)
        if page.status_code >= 400:
            raise AuthError(
                f"could not load the login page ({page.status_code} from {page.url})"
            )

        lowered = page.text.lower()
        for marker in _BOT_WALL_MARKERS:
            if marker in lowered:
                raise AuthError(
                    f"the login page now contains a bot challenge ({marker!r}). "
                    "spl-mcp logs in with a plain form POST and cannot solve one. "
                    "Log in manually in a browser, or the server will stay unauthenticated."
                )

        soup = BeautifulSoup(page.text, "lxml")
        # The login form's id is randomised per request, so find it by the one
        # field only it has.
        form = next(
            (f for f in soup.find_all("form") if f.find("input", attrs={"name": "user_pin"})),
            None,
        )
        if form is None:
            raise AuthError(
                "no login form found on the login page -- the field 'user_pin' was absent. "
                "BiblioCommons may have changed its login flow."
            )

        # Carry every hidden field through untouched (utf8, local, and the
        # authenticity_token Rails requires), then set our own values on top.
        payload: dict[str, str] = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            if not name:
                continue
            if inp.get("type") == "hidden":
                payload[name] = inp.get("value", "")

        if not payload.get("authenticity_token"):
            raise AuthError(
                "login form had no authenticity_token; Rails will reject the POST. "
                "The login page shape has changed."
            )

        payload["name"] = creds.card_number
        payload["user_pin"] = creds.pin
        payload["remember_me"] = "true"  # durable cookie, so we log in rarely
        submit = form.find("input", attrs={"type": "submit"}) or form.find("button")
        if submit is not None and submit.get("name"):
            payload[submit["name"]] = submit.get("value", "Log In")
        else:
            payload.setdefault("commit", "Log In")

        action = urljoin(str(page.url), form.get("action") or self.config.login_url)
        resp = await client.post(
            action,
            data=payload,
            headers={"Referer": str(page.url), "Origin": self.config.base_url},
            extensions={CHANGES_NOTHING: True},
        )

        if resp.status_code >= 400:
            raise AuthError(f"the library refused the login with HTTP {resp.status_code}")
        # The POST's own answer says whether the card and PIN were accepted:
        # a redirect that sets the access cookie. Where the redirects then
        # land is a separate fact -- the session can end before they finish.
        accepted = bool(resp.history) and _ACCEPTED_LOGIN_COOKIE in {
            c.split("=", 1)[0].strip() for c in resp.history[0].headers.get_list("set-cookie")
        }
        if accepted and self._looks_like_login(resp):
            raise SessionReplacedError(
                "the library accepted the card and PIN, but the session ended at once: "
                "another sign-in to the same library account (another library server, "
                "a browser or a phone; connected libraries such as Seattle and KCLS share "
                "one account) replaced it. The PIN is fine. Try again in a moment."
            )
        if self._looks_like_login(resp):
            raise AuthError(
                "login was rejected -- still on the login page afterwards. "
                "Check the card number and PIN (Keychain, environment, or secrets file). "
                "(BiblioCommons locks the account after repeated bad PINs, "
                "so do not retry blindly.)"
            )

        self._logged_in_hint = True
        self.login_count += 1
        self._save_cookies(client)
        log.info("logged in to %s", self.config.base_url)

    async def login(self, *, force: bool = False) -> None:
        """Public login entry point, serialised against other session use."""
        async with self._lock, self._account_lock():
            client = await self.client()
            if force:
                client.cookies.clear()
            await self._login(client)

    # ------------------------------------------------- authenticated I/O

    async def authenticated_get(self, url: str, **kwargs: Any) -> httpx.Response:
        """GET a member page, logging in first or again exactly once if needed."""
        async with self._lock, self._account_lock():
            client = await self.client()

            if not self._logged_in_hint:
                await self._login(client)

            resp = await client.get(url, **kwargs)
            if self._looks_like_login(resp):
                # Session expired. One transparent re-login, then one retry.
                log.info("session expired, re-authenticating")
                client.cookies.clear()
                await self._login(client)
                resp = await client.get(url, **kwargs)
                if self._looks_like_login(resp):
                    raise AuthError(
                        f"still redirected to the login page after re-authenticating "
                        f"(requested {url}). The session could not be established."
                    )

            if resp.status_code >= 400:
                raise UpstreamError(f"the library refused GET {url} with HTTP {resp.status_code}")
            self._save_cookies(client)
            return resp

    async def authenticated_request(
        self, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        """Issue an arbitrary authenticated request (used by the holds gateway).

        Unlike :meth:`authenticated_get` this does not retry on its own beyond a
        single re-login, and it never retries a request that may have already
        taken effect -- a mutation must not be replayed blindly.
        """
        async with self._lock, self._account_lock():
            client = await self.client()
            if not self._logged_in_hint:
                await self._login(client)

            resp = await client.request(method, url, **kwargs)
            if resp.status_code in (401, 403) or self._looks_like_login(resp):
                # An auth failure means the request was rejected, not applied,
                # so retrying it once is safe.
                log.info("gateway rejected the session, re-authenticating")
                client.cookies.clear()
                await self._login(client)
                resp = await client.request(method, url, **kwargs)

            self._save_cookies(client)
            return resp

    # ------------------------------------------------- unauthenticated I/O

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        """Plain GET for public pages. No lock, no login, no cookies required."""
        client = await self.client()
        resp = await client.get(url, **kwargs)
        if resp.status_code >= 400:
            raise UpstreamError(f"the library refused GET {url} with HTTP {resp.status_code}")
        return resp

    # ------------------------------------------------------------ helpers

    def csrf_token_from(self, html: str) -> str | None:
        """Pull the Rails CSRF token out of a rendered page's <meta> tag.

        The holds gateway may want this echoed as ``X-CSRF-Token``.
        """
        soup = BeautifulSoup(html, "lxml")
        meta = soup.find("meta", attrs={"name": "csrf-token"})
        if meta and meta.get("content"):
            return meta["content"]
        return None


def default_cookie_path() -> Path:
    return Path("~/.local/state/spl-mcp/cookies.json").expanduser()
