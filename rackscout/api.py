"""Web API with protected manual editing/collection and a same-origin static frontend."""
from __future__ import annotations

import json
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
from starlette.concurrency import run_in_threadpool

from . import cpu_specs, db, hardware_platforms, manual, providers, queries, refresh
from .config import load_settings
from .domain import SUPPORTED_VENDORS, normalize_vendor

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 +\-]{0,59}")
MAX_MODELS = 100
MAX_PROVIDERS = 20
MAX_HARDWARE_VALUES = 50
REFRESH_PATH = "/api/refresh"
MAX_REFRESH_BODY = 1024
MAX_EDIT_BODY = 32768
MANUAL_PATH_RE = re.compile(r"/api/listings/[0-9]+/manual")
SOLD_PATH_RE = re.compile(r"/api/listings/[0-9]+/sold")
SELECTED_REFRESH_PATH_RE = re.compile(r"/api/listings/[0-9]+/refresh")

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
    allowed = ("GET", "HEAD")
    if request.url.path == REFRESH_PATH or SELECTED_REFRESH_PATH_RE.fullmatch(request.url.path):
        allowed = ("GET", "HEAD", "POST")
    elif SOLD_PATH_RE.fullmatch(request.url.path):
        allowed = ("GET", "HEAD", "POST")
    elif MANUAL_PATH_RE.fullmatch(request.url.path):
        allowed = ("GET", "HEAD", "POST", "PATCH", "DELETE")
    if request.method not in allowed:
        return JSONResponse({"detail": "Method not allowed"}, status_code=405,
                            headers={**SECURITY_HEADERS, "Allow": ", ".join(allowed)})
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


def _labels(name: str, raw: list[str] | None, allowed: tuple[str, ...]) -> tuple[str, ...]:
    """Repeated reference-label parameter: trimmed, case-insensitive, canonical, deduplicated; empty ignored."""
    if raw is not None and len(raw) > MAX_HARDWARE_VALUES:
        raise HTTPException(status_code=422, detail=f"at most {MAX_HARDWARE_VALUES} {name} values may be supplied")
    canonical = {label.casefold(): label for label in allowed}
    out: list[str] = []
    for value in raw or []:
        value = value.strip()
        if not value:
            continue
        label = canonical.get(value.casefold())
        if label is None:
            raise HTTPException(status_code=422, detail=f"{name} must be one of {', '.join(allowed)}")
        if label not in out:
            out.append(label)
    return tuple(out)


def parse_filters(
    provider: Annotated[list[str] | None, Query(max_length=MAX_PROVIDERS)] = None,
    vendor: str | None = None,
    model: Annotated[list[str] | None, Query(max_length=MAX_MODELS)] = None,
    generation: str | None = None,
    cpu_manufacturer: Annotated[list[str] | None, Query()] = None,
    cpu_architecture: Annotated[list[str] | None, Query()] = None,
    memory_platform: Annotated[list[str] | None, Query()] = None,
    modern_only: str | None = None,
    height: str | None = None,
    min_ram: str | None = None,
    min_bays: str | None = None,
    drive_size: str | None = None,
    cpu_count: str | None = None,
    max_total: str | None = None,
    diskless: str | None = None,
    hide_damaged: str | None = None,
    include_sold: str | None = None,
    confirmed_only: str | None = None,
    match_status: str | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    sort: str | None = None,
    page: str | None = None,
    page_size: str | None = None,
) -> queries.Filters:
    if provider is not None and len(provider) > MAX_PROVIDERS:
        raise HTTPException(status_code=422, detail=f"at most {MAX_PROVIDERS} provider values may be supplied")
    known = providers.names()
    selected: list[str] = []
    for raw_provider in provider or []:
        value = raw_provider.strip()
        if not value:
            continue
        if value not in known:
            raise HTTPException(status_code=422, detail=f"provider must be one of {', '.join(known)}")
        if value not in selected:
            selected.append(value)
    vendor_raw = (vendor or "").strip()
    vendor_name = normalize_vendor(vendor_raw) if vendor_raw else None
    if vendor_raw and vendor_name is None:
        raise HTTPException(status_code=422, detail=f"vendor must be one of {', '.join(SUPPORTED_VENDORS)} or empty")
    gen = _opt_int("generation", generation, None, 1, 100)
    if gen is not None and vendor_name is None:
        raise HTTPException(status_code=422, detail="generation is vendor-specific; select a vendor first")
    if model is not None and len(model) > MAX_MODELS:
        raise HTTPException(status_code=422, detail=f"at most {MAX_MODELS} model values may be supplied")
    models: list[str] = []
    for raw_model in model or []:
        value = raw_model.strip()
        if not value:
            continue
        if not MODEL_RE.fullmatch(value):
            raise HTTPException(status_code=422, detail="invalid model")
        if value not in models:
            models.append(value)
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
    status = (match_status or "all").strip().lower() or "all"
    if status not in ("all", "match", "lead"):
        raise HTTPException(status_code=422, detail="match_status must be all, match, or lead")
    text = (q or "").strip() or None
    return queries.Filters(
        providers=tuple(selected),
        vendor=vendor_name,
        models=tuple(models),
        generation=gen,
        cpu_manufacturers=_labels("cpu_manufacturer", cpu_manufacturer, hardware_platforms.MANUFACTURERS),
        cpu_architectures=_labels("cpu_architecture", cpu_architecture, hardware_platforms.architectures()),
        memory_platforms=_labels("memory_platform", memory_platform, hardware_platforms.MEMORY_PLATFORMS),
        modern_only=_bool("modern_only", modern_only, True),
        height=_opt_int("height", height, 1, 1, 10),
        min_ram=_opt_int("min_ram", min_ram, 16, 0, 65536),
        min_bays=_opt_int("min_bays", min_bays, 4, 0, 64),
        drive_size=size,
        cpu_count=count,
        max_total=total,
        diskless=_bool("diskless", diskless, True),
        hide_damaged=_bool("hide_damaged", hide_damaged, True),
        include_sold=_bool("include_sold", include_sold, False),
        confirmed_only=_bool("confirmed_only", confirmed_only, False),
        match_status=status,
        q=text,
        sort=sort_key,
        page=_opt_int("page", page, 1, 1, 100000) or 1,
        page_size=_opt_int("page_size", page_size, 50, 1, 100) or 50,
    )


@app.get("/api/cpu-specs")
def cpu_specs_lookup(
    cpu_model: Annotated[str, Query(min_length=1, max_length=100)],
    cpu_count: Annotated[int | None, Query(ge=1, le=8)] = None,
) -> dict[str, Any]:
    return cpu_specs.lookup(cpu_model, cpu_count)


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
    return queries.status(conn, retailers=_settings.retailers_enabled)


@app.get(REFRESH_PATH)
def refresh_status(conn: Conn) -> dict[str, Any]:
    return refresh.status(conn, _settings)


def _enqueue_refresh(configuration_id: int | None = None) -> tuple[str, dict[str, Any]]:
    try:
        conn = db.connect(_settings.db_connect_timeout)
    except db.DatabaseUnavailable:
        log.warning("database unavailable")
        raise HTTPException(status_code=503, detail="Database unavailable") from None
    try:
        if configuration_id is None:
            return refresh.enqueue(conn, _settings)
        return refresh.enqueue(conn, _settings, configuration_id=configuration_id)
    except refresh.ListingRefreshNotFound:
        raise HTTPException(status_code=404, detail="Listing not found") from None
    except refresh.ListingRefreshCooldown as exc:
        raise HTTPException(status_code=429, detail="This listing was checked recently; try again after its cooldown.",
                            headers={"Retry-After": str(exc.retry_after_seconds)}) from None
    except psycopg.Error as exc:
        log.error("refresh enqueue failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Database unavailable") from None
    finally:
        conn.close()


async def _mutation_body(request: Request, origins: tuple[str, ...], header: str, limit: int,
                         disabled_message: str) -> dict[str, Any]:
    """Reject cross-site, oversized and malformed writes before opening a database."""
    if not origins:
        raise HTTPException(status_code=403, detail=disabled_message)
    if request.headers.get("origin") not in origins:
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")
    site = request.headers.get("sec-fetch-site")
    if site is not None and site != "same-origin":
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")
    if request.headers.get(header) != "1":
        raise HTTPException(status_code=403, detail=f"Missing {header} header")
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(status_code=415, detail="Content-Type must be application/json")
    declared = request.headers.get("content-length")
    if declared is not None and (not re.fullmatch(r"[0-9]{1,10}", declared.strip()) or int(declared) > limit):
        raise HTTPException(status_code=413, detail="Request body too large")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(status_code=413, detail="Request body too large")
        body.extend(chunk)
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):
        raise HTTPException(status_code=422, detail="Body must be a JSON object") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Body must be a JSON object")
    return payload


def _refresh_response(outcome: str, state: dict[str, Any]) -> JSONResponse:
    if outcome == "accepted":
        return JSONResponse(state, status_code=202)
    if outcome == "cooldown":
        return JSONResponse(state, status_code=429, headers={"Retry-After": str(state["retry_after_seconds"])})
    return JSONResponse(state, status_code=409)


@app.post(REFRESH_PATH)
async def request_refresh(request: Request) -> JSONResponse:
    payload = await _mutation_body(request, _settings.refresh_origins, "X-RackScout-Refresh", MAX_REFRESH_BODY,
                                   "Manual refresh disabled")
    if payload:
        raise HTTPException(status_code=422, detail="Manual refresh accepts no options; send {}")
    return _refresh_response(*await run_in_threadpool(_enqueue_refresh))


@app.post("/api/listings/{listing_id}/refresh")
async def request_listing_refresh(listing_id: int, request: Request) -> JSONResponse:
    payload = await _mutation_body(request, _settings.refresh_origins, "X-RackScout-Refresh", MAX_REFRESH_BODY,
                                   "Manual refresh disabled")
    if payload:
        raise HTTPException(status_code=422, detail="Listing refresh accepts no options; send {}")
    if not 0 < listing_id < 2**63:
        raise HTTPException(status_code=404, detail="Listing not found")
    if _settings.max_details <= 0:
        raise HTTPException(status_code=403, detail="Detail fetching disabled: no detail-check budget configured")
    return _refresh_response(*await run_in_threadpool(_enqueue_refresh, listing_id))


def _valid_configuration_id(configuration_id: int) -> None:
    if not 0 < configuration_id < 2**63:
        raise HTTPException(status_code=404, detail="Configuration not found")


def _manual_error(exc: Exception) -> HTTPException:
    if isinstance(exc, manual.ManualNotFound):
        return HTTPException(status_code=404, detail="Configuration not found")
    if isinstance(exc, manual.ManualConflict):
        return HTTPException(status_code=409, detail=str(exc) or "Configuration changed; reload before saving")
    return HTTPException(status_code=422, detail=str(exc) or "Invalid manual configuration")


def _write_manual(operation: str, configuration_id: int, parsed: Any) -> dict[str, Any]:
    try:
        conn = db.connect(_settings.db_connect_timeout, autocommit=True)
    except db.DatabaseUnavailable:
        raise HTTPException(status_code=503, detail="Database unavailable") from None
    try:
        conn.execute("SET statement_timeout = '10s'")
        conn.execute("SET lock_timeout = '10s'")
        if operation == "delete":
            return manual.delete(conn, configuration_id, parsed)
        if operation == "create":
            result_id = manual.create(conn, configuration_id, parsed)
        elif operation == "sold":
            result_id = manual.mark_sold(conn, configuration_id, parsed)
        else:
            result_id = manual.update(conn, configuration_id, parsed)
        item = queries.get_listing(conn, result_id)
        if item is None:
            raise HTTPException(status_code=409, detail="Listing changed; reload to view the saved configuration")
        return {"item": item}
    except (manual.ManualValidationError, manual.ManualNotFound, manual.ManualConflict) as exc:
        raise _manual_error(exc) from None
    except psycopg.Error as exc:
        log.error("manual configuration write failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Database unavailable") from None
    finally:
        conn.close()


@app.get("/api/listings/{listing_id}/manual")
def manual_editor(listing_id: int, conn: Conn) -> dict[str, Any]:
    _valid_configuration_id(listing_id)
    try:
        return {**manual.editor(conn, listing_id), "enabled": bool(_settings.edit_origins),
                "fetch_enabled": bool(_settings.refresh_origins) and _settings.max_details > 0}
    except (manual.ManualValidationError, manual.ManualNotFound, manual.ManualConflict) as exc:
        raise _manual_error(exc) from None


async def _manual_payload(request: Request, listing_id: int) -> Any:
    payload = await _mutation_body(request, _settings.edit_origins, "X-RackScout-Edit", MAX_EDIT_BODY,
                                   "Manual editing disabled")
    _valid_configuration_id(listing_id)
    try:
        return manual.parse_write(payload)
    except manual.ManualValidationError as exc:
        raise _manual_error(exc) from None


@app.post("/api/listings/{listing_id}/manual")
async def create_manual(listing_id: int, request: Request) -> JSONResponse:
    parsed = await _manual_payload(request, listing_id)
    result = await run_in_threadpool(_write_manual, "create", listing_id, parsed)
    return JSONResponse(result, status_code=201)


@app.patch("/api/listings/{listing_id}/manual")
async def update_manual(listing_id: int, request: Request) -> dict[str, Any]:
    parsed = await _manual_payload(request, listing_id)
    return await run_in_threadpool(_write_manual, "update", listing_id, parsed)


@app.delete("/api/listings/{listing_id}/manual")
async def delete_manual(listing_id: int, request: Request) -> dict[str, Any]:
    payload = await _mutation_body(request, _settings.edit_origins, "X-RackScout-Edit", MAX_EDIT_BODY,
                                   "Manual editing disabled")
    _valid_configuration_id(listing_id)
    revision = payload.get("revision")
    if set(payload) != {"revision"} or type(revision) is not int or not 0 < revision < 2**63:
        raise HTTPException(status_code=422, detail="Body must contain only a positive integer revision")
    return await run_in_threadpool(_write_manual, "delete", listing_id, revision)


@app.post("/api/listings/{listing_id}/sold")
async def mark_listing_sold(listing_id: int, request: Request) -> dict[str, Any]:
    payload = await _mutation_body(request, _settings.edit_origins, "X-RackScout-Edit", MAX_EDIT_BODY,
                                   "Manual editing disabled")
    _valid_configuration_id(listing_id)
    try:
        sold = manual.parse_sold(payload)
    except manual.ManualValidationError as exc:
        raise _manual_error(exc) from None
    return await run_in_threadpool(_write_manual, "sold", listing_id, sold)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")
