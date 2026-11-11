"""The suite never touches the real state directory (session lock, cookie files)."""

from __future__ import annotations

from pathlib import Path

from spl_mcp.config import Config

REAL_HOME = Path("/Users") if Path("/Users").is_dir() else Path("/home")

def test_default_state_paths_are_under_a_throwaway_home(tmp_path_factory):
    config = Config()
    base = Path(tmp_path_factory.getbasetemp())
    for path in (config.session_lock, config.cookie_store):
        assert base in path.parents, path
