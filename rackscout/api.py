"""Read-only web API and same-origin static frontend."""
from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db, queries
from .config import load_settings
from .domain import SUPPORTED_VENDORS, normalize_vendor

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 +\-]{0,59}")

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
        "font-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}

app = FastAPI(title="RackScout", docs_url=None, redoc_url=None, openapi_url=None)
_settings = load_settings()


@app.middleware("http")
async def security_headers(request: Request, call_next: Any) -> Any:
    if request.method not in ("GET", "HEAD"):
        return JSONResponse({"detail": "Method not allowed"}, status_code=405, headers={**SECURITY_HEADERS, "Allow": "GET, HEAD"})
    response = await call_next(request)
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    if request.url.path.startswith("/api/") or request.url.path in ("/health", "/ready"):
        response.headers["Cache-Control"] = "no-store"
    return response


def get_conn() -> Iterator[psycopg.Connection]:
    try:
        conn = db.connect(_settings.db_connect_timeout)
    except db.DatabaseUnavailable:
        log.warning("database unavailable")
        raise HTTPException(status_code=503, detail="Database unavailable") from None
    try:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '10s'")
        yield conn
    except psycopg.Error as exc:
        log.error("database query failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Database unavailable") from None
    finally:
        conn.close()


Conn = Annotated[psycopg.Connection, Depends(get_conn)]


def _opt_int(name: str, raw: str | None, default: int | None, lo: int, hi: int) -> int | None:
    """Absent → default; empty string → no constraint; otherwise bounded integer."""
    if raw is None:
        return default
    raw = raw.strip()
    if raw == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{name} must be an integer") from None
    if not lo <= value <= hi:
        raise HTTPException(status_code=422, detail=f"{name} must be between {lo} and {hi}")
    return value


def _bool(name: str, raw: str | None, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise HTTPException(status_code=422, detail=f"{name} must be true or false")


def parse_filters(
    vendor: str | None = None,
    model: str | None = None,
    generation: str | None = None,
    modern_only: str | None = None,
    height: str | None = None,
    min_ram: str | None = None,
    min_bays: str | None = None,
    drive_size: str | None = None,
    cpu_count: str | None = None,
    max_total: str | None = None,
    diskless: str | None = None,
    hide_damaged: str | None = None,
    confirmed_only: str | None = None,
    include_leads: str | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    sort: str | None = None,
    page: str | None = None,
    page_size: str | None = None,
) -> queries.Filters:
    vendor_raw = (vendor or "").strip()
    vendor_name = normalize_vendor(vendor_raw) if vendor_raw else None
    if vendor_raw and vendor_name is None:
        raise HTTPException(status_code=422, detail=f"vendor must be one of {', '.join(SUPPORTED_VENDORS)} or empty")
    gen = _opt_int("generation", generation, None, 1, 100)
    if gen is not None and vendor_name is None:
        raise HTTPException(status_code=422, detail="generation is vendor-specific; select a vendor first")
    model = (model or "").strip() or None
    if model is not None and not MODEL_RE.fullmatch(model):
        raise HTTPException(status_code=422, detail="invalid model")
    size = (drive_size or "").strip().upper() or None
    if size not in (None, "SFF", "LFF"):
        raise HTTPException(status_code=422, detail="drive_size must be SFF, LFF or empty")
    count = _opt_int("cpu_count", cpu_count, None, 1, 2)
    total: Decimal | None = None
    if max_total is not None and max_total.strip():
        try:
            total = Decimal(max_total.strip())
        except InvalidOperation:
            raise HTTPException(status_code=422, detail="max_total must be a decimal") from None
        if not total.is_finite() or total < 0 or total > Decimal("1000000"):
            raise HTTPException(status_code=422, detail="max_total out of range")
    sort_key = (sort or "price").strip() or "price"
    if sort_key not in queries.SORTS:
        raise HTTPException(status_code=422, detail="sort must be price, ram, bays or benchmark")
    text = (q or "").strip() or None
    return queries.Filters(
        vendor=vendor_name,
        model=model,
        generation=gen,
        modern_only=_bool("modern_only", modern_only, True),
        height=_opt_int("height", height, 1, 1, 10),
        min_ram=_opt_int("min_ram", min_ram, 16, 0, 65536),
        min_bays=_opt_int("min_bays", min_bays, 4, 0, 64),
        drive_size=size,
        cpu_count=count,
        max_total=total,
        diskless=_bool("diskless", diskless, True),
        hide_damaged=_bool("hide_damaged", hide_damaged, True),
        confirmed_only=_bool("confirmed_only", confirmed_only, False),
        include_leads=_bool("include_leads", include_leads, True),
        q=text,
        sort=sort_key,
        page=_opt_int("page", page, 1, 1, 100000) or 1,
        page_size=_opt_int("page_size", page_size, 50, 1, 100) or 50,
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> JSONResponse:
    try:
        conn = db.connect(_settings.db_connect_timeout)
    except db.DatabaseUnavailable:
        return JSONResponse({"status": "unavailable", "detail": "Database unavailable"}, status_code=503)
    try:
        if not db.schema_ready(conn):
            return JSONResponse({"status": "unavailable", "detail": "Database schema not migrated"}, status_code=503)
        return JSONResponse({"status": "ready"})
    except psycopg.Error as exc:
        log.error("readiness query failed: %s", type(exc).__name__)
        return JSONResponse({"status": "unavailable", "detail": "Database unavailable"}, status_code=503)
    finally:
        conn.close()


@app.get("/api/listings")
def list_listings(conn: Conn, filters: Annotated[queries.Filters, Depends(parse_filters)]) -> dict[str, Any]:
    return queries.query_listings(conn, filters)


@app.get("/api/listings/{listing_id}")
def listing_detail(listing_id: int, conn: Conn) -> dict[str, Any]:
    if not 0 < listing_id < 2**63:
        raise HTTPException(status_code=404, detail="Not found")
    item = queries.get_listing(conn, listing_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Not found")
    return item


@app.get("/api/facets")
def facets(conn: Conn) -> dict[str, Any]:
    return queries.facets(conn)


@app.get("/api/status")
def status(conn: Conn) -> dict[str, Any]:
    return queries.status(conn)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")
