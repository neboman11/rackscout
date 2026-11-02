"""Runtime configuration from environment variables.

Database settings come from ``DATABASE_URL`` or the standard libpq ``PG*``
variables. Discovery reads a fixed set of allowlisted PerUnitPrice vendor
index pages; configuration never selects an arbitrary host. The optional
benchmark dataset is an external private file and is never bundled.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Fixed discovery pages, in collection order. Each embeds that vendor's complete
# inventory; the combined /servers/ page embeds only a truncated 1000-row subset.
VENDOR_INDEX_URLS: tuple[tuple[str, str], ...] = (
    ("Dell", "https://perunitprice.com/servers/dell/"),
    ("HPE", "https://perunitprice.com/servers/hpe/"),
    ("Supermicro", "https://perunitprice.com/servers/supermicro/"),
    ("Lenovo", "https://perunitprice.com/servers/lenovo/"),
    ("Cisco", "https://perunitprice.com/servers/cisco/"),
)
INDEX_HOSTS = frozenset({"perunitprice.com", "www.perunitprice.com"})
DESCRIPTION_HOST = "itm.ebaydesc.com"
EBAY_HOST = "www.ebay.com"
USER_AGENT = "RackScout/0.1 (private personal deal finder; respects robots.txt)"
BENCHMARKS_FILE_ENV = "RACKSCOUT_BENCHMARKS_FILE"


class ConfigError(ValueError):
    """Invalid runtime configuration."""


def _int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


@dataclass(frozen=True)
class Settings:
    source_pages: tuple[tuple[str, str], ...]
    max_details: int
    browser_enabled: bool
    browser_timeout: float
    request_timeout: float
    request_delay: float
    recheck_cooldown_hours: int
    page_verification_hours: int
    max_index_bytes: int
    max_description_bytes: int
    max_variants: int
    db_connect_timeout: int
    migrate_wait_seconds: int


def load_settings() -> Settings:
    recheck_cooldown_hours = _int("RACKSCOUT_RECHECK_COOLDOWN_HOURS", 6, 1, 24 * 30)
    # How long a successful eBay page verification stays current. It must outlast the
    # recheck cooldown plus the collection schedule interval so rechecks land in time.
    page_verification_hours = _int("RACKSCOUT_PAGE_VERIFICATION_HOURS", 12, 1, 24 * 30)
    if page_verification_hours < recheck_cooldown_hours:
        raise ConfigError("RACKSCOUT_PAGE_VERIFICATION_HOURS must not be shorter than RACKSCOUT_RECHECK_COOLDOWN_HOURS")
    return Settings(
        source_pages=VENDOR_INDEX_URLS,
        max_details=_int("RACKSCOUT_MAX_DETAILS", 25, 0, 500),
        browser_enabled=_bool("RACKSCOUT_BROWSER_ENABLED", True),
        browser_timeout=_float("RACKSCOUT_BROWSER_TIMEOUT", 45.0, 5.0, 300.0),
        request_timeout=_float("RACKSCOUT_REQUEST_TIMEOUT", 30.0, 1.0, 120.0),
        request_delay=_float("RACKSCOUT_REQUEST_DELAY", 3.0, 1.0, 120.0),
        recheck_cooldown_hours=recheck_cooldown_hours,
        page_verification_hours=page_verification_hours,
        max_index_bytes=_int("RACKSCOUT_MAX_INDEX_BYTES", 8 * 1024 * 1024, 4 * 1024 * 1024, 64 * 1024 * 1024),
        max_description_bytes=_int("RACKSCOUT_MAX_DESCRIPTION_BYTES", 2 * 1024 * 1024, 64 * 1024, 16 * 1024 * 1024),
        max_variants=_int("RACKSCOUT_MAX_VARIANTS", 12, 1, 50),
        db_connect_timeout=_int("RACKSCOUT_DB_CONNECT_TIMEOUT", 10, 1, 120),
        migrate_wait_seconds=_int("RACKSCOUT_MIGRATE_WAIT_SECONDS", 180, 0, 3600),
    )


def benchmarks_file() -> Path | None:
    """Path of the optional external CPU benchmark dataset, or None when unconfigured."""
    raw = os.environ.get(BENCHMARKS_FILE_ENV, "").strip()
    return Path(raw) if raw else None


def database_conninfo() -> str:
    """Return a libpq conninfo string. Empty string lets libpq read PG* vars."""
    return os.environ.get("DATABASE_URL", "").strip()
