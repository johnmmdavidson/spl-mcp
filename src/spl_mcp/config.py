"""Configuration and credential loading.

Config comes from a TOML file; every field can be overridden by an env var of
the form ``SPL_MCP_<FIELD>``. Credentials are loaded separately, from a file
that is never part of the repo, or from the environment.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

CONFIG_SEARCH_PATHS = (
    Path("config.toml"),
    Path("~/.config/spl-mcp/config.toml"),
)
SECRETS_SEARCH_PATHS = (
    Path("secrets.toml"),
    Path("~/.config/spl-mcp/secrets.toml"),
)

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _as_bool(raw: str, *, name: str) -> bool:
    v = raw.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ConfigError(f"{name} must be a boolean-ish value, got {raw!r}")


def _first_existing(paths: tuple[Path, ...]) -> Path | None:
    for p in paths:
        expanded = p.expanduser()
        if expanded.is_file():
            return expanded
    return None


def _load_toml(path: Path) -> dict:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


@dataclass(frozen=True)
class Credentials:
    card_number: str
    pin: str

    def __repr__(self) -> str:  # pragma: no cover - defensive, keeps secrets out of tracebacks
        return "Credentials(card_number='***', pin='***')"


@dataclass
class Config:
    # library
    slug: str = "seattle"
    base_url: str = "https://seattle.bibliocommons.com"
    gateway_url: str = "https://gateway.bibliocommons.com"
    # holds
    default_pickup_branch: str | None = None
    # safety -- ships engaged
    read_only: bool = True
    # ebooks
    use_overdrive_thunder: bool = True
    overdrive_library: str = "spl"
    # http
    timeout_seconds: float = 30.0
    user_agent: str = (
        "spl-mcp/0.1 (personal library agent; +https://github.com/johnmmdavidson/spl-mcp)"
    )
    # paths
    cookie_store: Path = field(
        default_factory=lambda: Path("~/.local/state/spl-mcp/cookies.json").expanduser()
    )
    # credentials, resolved lazily so unauthenticated tools work without them
    credentials: Credentials | None = None

    # -- derived URLs -------------------------------------------------

    @property
    def search_url(self) -> str:
        return f"{self.base_url}/v2/search"

    @property
    def login_url(self) -> str:
        return f"{self.base_url}/user/login"

    @property
    def holds_url(self) -> str:
        return f"{self.base_url}/v2/holds"

    @property
    def holds_gateway_url(self) -> str:
        return f"{self.gateway_url}/v2/libraries/{self.slug}/holds"

    def record_url(self, bib_id: str) -> str:
        return f"{self.base_url}/v2/record/{bib_id}"

    def require_credentials(self) -> Credentials:
        if self.credentials is None:
            raise ConfigError(
                "no library credentials configured. Set SPL_MCP_CARD_NUMBER and SPL_MCP_PIN, "
                "or copy secrets.example.toml to ~/.config/spl-mcp/secrets.toml and fill it in."
            )
        return self.credentials


def load_credentials(path: Path | None = None) -> Credentials | None:
    """Env vars win; otherwise read the first secrets file we find."""
    card = os.environ.get("SPL_MCP_CARD_NUMBER")
    pin = os.environ.get("SPL_MCP_PIN")
    if card and pin:
        return Credentials(card_number=card.strip(), pin=pin.strip())

    secrets_path = path or _first_existing(SECRETS_SEARCH_PATHS)
    if secrets_path is None:
        return None

    data = _load_toml(secrets_path).get("credentials", {})
    card = card or str(data.get("card_number", "")).strip()
    pin = pin or str(data.get("pin", "")).strip()
    if not card or not pin:
        raise ConfigError(
            f"{secrets_path} is missing credentials.card_number or credentials.pin "
            "(see secrets.example.toml)"
        )
    return Credentials(card_number=card, pin=pin)


def load_config(path: Path | None = None) -> Config:
    """Build a Config from file + environment. Environment always wins."""
    cfg = Config()

    config_path = path or _first_existing(CONFIG_SEARCH_PATHS)
    if config_path is not None:
        data = _load_toml(config_path)
        lib = data.get("library", {})
        cfg.slug = lib.get("slug", cfg.slug)
        cfg.base_url = str(lib.get("base_url", cfg.base_url)).rstrip("/")
        cfg.gateway_url = str(lib.get("gateway_url", cfg.gateway_url)).rstrip("/")

        holds = data.get("holds", {})
        cfg.default_pickup_branch = holds.get("default_pickup_branch", cfg.default_pickup_branch)

        safety = data.get("safety", {})
        cfg.read_only = bool(safety.get("read_only", cfg.read_only))

        ebooks = data.get("ebooks", {})
        cfg.use_overdrive_thunder = bool(
            ebooks.get("use_overdrive_thunder", cfg.use_overdrive_thunder)
        )
        cfg.overdrive_library = ebooks.get("overdrive_library", cfg.overdrive_library)

        http = data.get("http", {})
        cfg.timeout_seconds = float(http.get("timeout_seconds", cfg.timeout_seconds))
        cfg.user_agent = http.get("user_agent", cfg.user_agent)

        paths = data.get("paths", {})
        if "cookie_store" in paths:
            cfg.cookie_store = Path(paths["cookie_store"]).expanduser()

    # -- environment overrides ---------------------------------------
    env = os.environ
    cfg.slug = env.get("SPL_MCP_SLUG", cfg.slug)
    cfg.base_url = env.get("SPL_MCP_BASE_URL", cfg.base_url).rstrip("/")
    cfg.gateway_url = env.get("SPL_MCP_GATEWAY_URL", cfg.gateway_url).rstrip("/")
    cfg.default_pickup_branch = env.get(
        "SPL_MCP_DEFAULT_PICKUP_BRANCH", cfg.default_pickup_branch
    )
    if "SPL_MCP_READ_ONLY" in env:
        cfg.read_only = _as_bool(env["SPL_MCP_READ_ONLY"], name="SPL_MCP_READ_ONLY")
    if "SPL_MCP_USE_OVERDRIVE_THUNDER" in env:
        cfg.use_overdrive_thunder = _as_bool(
            env["SPL_MCP_USE_OVERDRIVE_THUNDER"], name="SPL_MCP_USE_OVERDRIVE_THUNDER"
        )
    cfg.overdrive_library = env.get("SPL_MCP_OVERDRIVE_LIBRARY", cfg.overdrive_library)
    if "SPL_MCP_TIMEOUT_SECONDS" in env:
        cfg.timeout_seconds = float(env["SPL_MCP_TIMEOUT_SECONDS"])
    cfg.user_agent = env.get("SPL_MCP_USER_AGENT", cfg.user_agent)
    if "SPL_MCP_COOKIE_STORE" in env:
        cfg.cookie_store = Path(env["SPL_MCP_COOKIE_STORE"]).expanduser()

    cfg.credentials = load_credentials()
    return cfg
