"""Configuration and credential loading.

Config comes from a TOML file; every field can be overridden by an env var of
the form ``SPL_MCP_<FIELD>``. Credentials are loaded separately, from a file
that is never part of the repo, or from the environment.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
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

# macOS Keychain: generic-password items under this service, one per account.
KEYCHAIN_SERVICE_DEFAULT = "cornice-spl"
KEYCHAIN_ACCOUNTS = ("card_number", "pin")
# `security` exits 44 (errSecItemNotFound) when the item does not exist. Any
# other non-zero exit is an unreadable Keychain, not an absent item.
_SECURITY_NOT_FOUND = 44

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
    # why credentials could not be read, when they exist but are unreadable.
    # Kept rather than raised so search and the health check still work.
    credentials_error: str | None = None

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
    def checkouts_url(self) -> str:
        # /v2/checkouts and /v2/borrowing/checkedout also return 200 with a
        # valid app state, but with an empty entities.checkouts. This is the
        # only path that actually carries the loans.
        return f"{self.base_url}/v2/checkedout"

    @property
    def holds_gateway_url(self) -> str:
        return f"{self.gateway_url}/v2/libraries/{self.slug}/holds"

    def record_url(self, bib_id: str) -> str:
        return f"{self.base_url}/v2/record/{bib_id}"

    def require_credentials(self) -> Credentials:
        if self.credentials is None:
            if self.credentials_error:
                raise ConfigError(self.credentials_error)
            raise ConfigError(
                "no library credentials configured. Add them to the macOS Keychain "
                f"(service {keychain_service()!r}, accounts 'card_number' and 'pin'), "
                "set SPL_MCP_CARD_NUMBER and SPL_MCP_PIN, "
                "or copy secrets.example.toml to ~/.config/spl-mcp/secrets.toml and fill it in."
            )
        return self.credentials


def keychain_service() -> str:
    """The Keychain service to read; SPL_MCP_KEYCHAIN_SERVICE='' turns it off."""
    return os.environ.get("SPL_MCP_KEYCHAIN_SERVICE", KEYCHAIN_SERVICE_DEFAULT).strip()


def _run_security(service: str, account: str) -> subprocess.CompletedProcess[str]:
    """The one place that shells out to the Keychain. Tests replace it."""
    return subprocess.run(
        ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _keychain_read(service: str, account: str) -> str | None:
    """One Keychain item's value, or None when the item does not exist.

    Raises ConfigError when the item may exist but could not be read (access
    denied, Keychain locked): "unreadable" must not be reported as "absent".
    The value itself never appears in an error message.
    """
    try:
        out = _run_security(service, account)
    except FileNotFoundError:
        return None  # no `security` binary: not a Mac
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(
            f"could not read Keychain item {service}/{account}: {type(exc).__name__}"
        ) from None
    if out.returncode == _SECURITY_NOT_FOUND:
        return None
    if out.returncode != 0:
        raise ConfigError(
            f"could not read Keychain item {service}/{account} "
            f"(security exited {out.returncode}); the Keychain may be locked or access denied"
        )
    value = out.stdout.rstrip("\n").strip()
    return value or None


def load_keychain_credentials() -> Credentials | None:
    """Card number and PIN from the macOS Keychain, or None if not stored there."""
    service = keychain_service()
    if not service or sys.platform != "darwin" or shutil.which("security") is None:
        return None
    card, pin = (_keychain_read(service, account) for account in KEYCHAIN_ACCOUNTS)
    if card and pin:
        return Credentials(card_number=card, pin=pin)
    if card or pin:
        missing = "pin" if card else "card_number"
        raise ConfigError(
            f"the Keychain has only half the library login: service {service!r} "
            f"has no {missing!r} item"
        )
    return None


def load_credentials(path: Path | None = None) -> Credentials | None:
    """Env vars win; then the macOS Keychain; then the first secrets file we find."""
    card = os.environ.get("SPL_MCP_CARD_NUMBER")
    pin = os.environ.get("SPL_MCP_PIN")
    if card and pin:
        return Credentials(card_number=card.strip(), pin=pin.strip())

    if not (card or pin):
        from_keychain = load_keychain_credentials()
        if from_keychain is not None:
            return from_keychain

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

    try:
        cfg.credentials = load_credentials()
    except ConfigError as exc:
        cfg.credentials = None
        cfg.credentials_error = str(exc)
    return cfg
