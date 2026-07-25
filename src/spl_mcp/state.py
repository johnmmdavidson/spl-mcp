"""Extracting BiblioCommons' embedded app state from an HTML page.

Every BiblioCommons v2 page ships its entire Redux-style app state as JSON in a
``<script data-iso-key="_0">`` node. We read that instead of scraping the DOM:
the JSON is the same data the page's own JavaScript renders from, so it is far
more stable than markup, and it carries fields the rendered page never shows.

The one trap: **two** elements carry ``data-iso-key="_0"`` -- a ``<div>`` holding
rendered HTML and a ``<script>`` holding the JSON. Always take the ``<script>``.
"""

from __future__ import annotations

import json
from typing import Any

from bs4 import BeautifulSoup

from .errors import ParseError

ISO_KEY = "_0"

# Sentinel so dig() can tell "key absent" from "key present but null".
_MISSING = object()


def extract_state(html: str, *, url: str | None = None) -> dict[str, Any]:
    """Pull the embedded app-state JSON out of a BiblioCommons page.

    Returns the state object itself. Note there is no ``data`` wrapper key --
    ``entities``, ``search``, ``borrowing`` and friends sit at the top level.
    """
    soup = BeautifulSoup(html, "lxml")
    nodes = soup.find_all(attrs={"data-iso-key": ISO_KEY})
    if not nodes:
        raise ParseError(
            f"no element with data-iso-key={ISO_KEY!r} found in the page "
            f"({len(html)} bytes). If the page looks like a login form, the session expired.",
            url=url,
        )

    # The <div> sibling holds rendered HTML, not JSON. Take the <script>.
    script = next((n for n in nodes if n.name == "script"), None)
    if script is None:
        found = ", ".join(sorted({n.name for n in nodes}))
        raise ParseError(
            f"found data-iso-key={ISO_KEY!r} on <{found}> but not on a <script> node, "
            "so there was no JSON payload to read",
            url=url,
        )

    raw = script.string or script.get_text()
    if not raw or not raw.strip():
        raise ParseError("the app-state <script> node was empty", url=url)

    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ParseError(
            f"app-state <script> node was not valid JSON: {exc} "
            f"(payload started with {raw[:120]!r})",
            url=url,
        ) from exc

    if not isinstance(state, dict):
        raise ParseError(
            f"expected the app state to be a JSON object, got {type(state).__name__}", url=url
        )
    return state


def dig(state: Any, path: str, *, url: str | None = None, default: Any = _MISSING) -> Any:
    """Walk a dotted path through nested dicts, failing loudly by default.

    ``dig(state, "search.catalogSearch.results")`` returns the list, or raises a
    ParseError naming the exact path that went missing. Pass ``default`` to opt
    into tolerance for a genuinely optional field.
    """
    cur = state
    walked: list[str] = []
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            if default is not _MISSING:
                return default
            where = ".".join(walked) or "<root>"
            available = (
                ", ".join(sorted(cur)[:12]) if isinstance(cur, dict) else f"<{type(cur).__name__}>"
            )
            raise ParseError(
                f"missing {part!r} under {where}; keys present there: {available}",
                url=url,
                path=path,
            )
        cur = cur[part]
        walked.append(part)
    return cur
