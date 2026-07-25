"""Error types.

The guiding rule from the build brief: **fail loudly**. A BiblioCommons markup
change or an expired session must surface as a clear error, never as an empty
result set that reads like "you have no holds".
"""

from __future__ import annotations


class SplMcpError(Exception):
    """Base class for every error this server raises deliberately."""


class ConfigError(SplMcpError):
    """Configuration or credentials are missing or malformed."""


class AuthError(SplMcpError):
    """Login failed, or a session expired and could not be renewed."""


class ParseError(SplMcpError):
    """The page did not contain the JSON shape we expect.

    Raised when BiblioCommons' frontend drifts. Carries enough context to make
    the breakage diagnosable without re-running the scrape by hand.
    """

    def __init__(self, message: str, *, url: str | None = None, path: str | None = None):
        self.url = url
        self.path = path
        detail = message
        if path:
            detail += f" (expected at state path: {path})"
        if url:
            detail += f" [url: {url}]"
        detail += (
            " -- this usually means the BiblioCommons frontend changed shape. "
            "Re-run the parser tests against a fresh fixture."
        )
        super().__init__(detail)


class ReadOnlyError(SplMcpError):
    """A mutating tool was called while the READ_ONLY guardrail is engaged."""

    def __init__(self, tool: str):
        super().__init__(
            f"'{tool}' is disabled: spl-mcp is running in READ_ONLY mode, so no hold was "
            "placed, cancelled, suspended, or reactivated and no request was sent. "
            "To allow changes, set read_only = false under [safety] in config.toml "
            "(or SPL_MCP_READ_ONLY=false)."
        )


class NotSupportedError(SplMcpError):
    """The request is well-formed but out of scope by design.

    Chiefly: mutating a digital (OverDrive) hold. BiblioCommons exposes those
    actions, but this server deliberately keeps digital holds read-only.
    """


class UpstreamError(SplMcpError):
    """BiblioCommons or OverDrive returned something we cannot use."""
