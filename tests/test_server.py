"""The MCP layer itself: the server must import and register its tools.

Until this file existed no test imported server.py, so an mcp release that
removed ``mcp.server.fastmcp`` broke every fresh install with the suite green.
"""

from __future__ import annotations

from spl_mcp import catalog, server

EXPECTED_TOOLS = {
    "search_titles",
    "get_title",
    "check_ebook_formats",
    "list_holds",
    "list_checkouts",
    "list_ready_to_borrow",
    "health_check",
    "place_hold",
    "cancel_hold",
    "suspend_hold",
    "reactivate_hold",
}


async def test_server_imports_and_registers_every_tool():
    tools = await server.mcp.list_tools()
    assert {t.name for t in tools} == EXPECTED_TOOLS


async def test_get_title_reports_the_queue(monkeypatch):
    bib = {
        "briefInfo": {"title": "The Road", "format": "EBOOK"},
        "availability": {"heldCopies": 16, "totalCopies": 16, "availableCopies": 0},
        "policy": {"materialType": "DIGITAL", "holdable": True},
    }

    async def fake_fetch(session, config, bib_id):
        return bib

    monkeypatch.setattr(catalog, "fetch_record", fake_fetch)
    result = await server.get_title("S246C000001")
    assert result["queue"]["holds"] == 16
    assert result["queue"]["summary"] == "16 holds on 16 copies"
    assert result["queue"]["position_if_placed"] is None
