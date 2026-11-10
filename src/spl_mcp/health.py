"""Health check: is the server up, is the library reachable, does the login work.

Strictly read-only: it loads the public login page and, when credentials are
configured, reads the holds page. Usable without MCP, either as the
``spl-mcp-health`` command or as ``await run_health(config, session)``.

Each check is ``ok`` True, False, or None. None means "not checked", which is
never reported as a pass.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from . import __version__
from .config import Config, keychain_service, load_config
from .errors import SplMcpError
from .session import SplSession


def _check(name: str, ok: bool | None, detail: str) -> dict[str, Any]:
    return {"check": name, "ok": ok, "detail": detail}


async def run_health(config: Config, session: SplSession) -> dict[str, Any]:
    checks = [
        _check("server", True, f"spl-mcp {__version__} is up; read_only={config.read_only}")
    ]

    try:
        await session.get(config.login_url)
        library_ok = True
        checks.append(_check("library", True, f"{config.base_url} answered"))
    except SplMcpError as exc:
        library_ok = False
        checks.append(_check("library", False, str(exc)))

    if config.credentials is None:
        if config.credentials_error:
            checks.append(_check("login", False, config.credentials_error))
        else:
            checks.append(
                _check(
                    "login",
                    None,
                    "not checked: no library credentials configured (Keychain service "
                    f"{keychain_service() or '(off)'!r}, SPL_MCP_CARD_NUMBER/SPL_MCP_PIN, "
                    "or secrets.toml)",
                )
            )
    elif not library_ok:
        checks.append(_check("login", None, "not checked: the library is unreachable"))
    else:
        had_session = session.has_session
        try:
            await session.authenticated_get(config.holds_url)
            how = "saved session still valid" if had_session else "logged in with the card and PIN"
            checks.append(_check("login", True, f"{how}; holds page readable"))
        except SplMcpError as exc:
            checks.append(_check("login", False, str(exc)))

    results = [c["ok"] for c in checks]
    if all(r is True for r in results):
        status = "ok"
    elif not library_ok:
        status = "down"
    else:
        status = "degraded"
    return {"status": status, "checks": checks}


def format_report(report: dict[str, Any]) -> str:
    marks = {True: "  ok  ", False: " FAIL ", None: " skip "}
    lines = [f"[{marks[c['ok']]}] {c['check']}: {c['detail']}" for c in report["checks"]]
    lines.append(f"status: {report['status']}")
    return "\n".join(lines)


async def _run() -> int:
    config = load_config()
    session = SplSession(config)
    try:
        report = await run_health(config, session)
    finally:
        await session.aclose()
    print(format_report(report))
    return 0 if report["status"] == "ok" else 1


def main() -> None:
    try:
        code = asyncio.run(_run())
    except SplMcpError as exc:
        print(f"[ FAIL ] config: {exc}\nstatus: down")
        code = 1
    except Exception as exc:  # one plain line, never a stack trace
        print(f"[ FAIL ] health check crashed: {type(exc).__name__}: {exc}\nstatus: down")
        code = 2
    sys.exit(code)


if __name__ == "__main__":
    main()
