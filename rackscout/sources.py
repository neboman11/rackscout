"""Fixed-origin source acquisition and strict parsing.

Only allowlisted HTTPS destinations are fetched. Redirects are followed
manually and every hop is validated. Publisher JavaScript is never executed;
the embedded ``d``/``cfg`` JSON payload is parsed and validated instead.
"""
from __future__ import annotations

import email.utils
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup, Comment, Doctype, NavigableString, ProcessingInstruction, Declaration, CData

from . import domain
from .config import DESCRIPTION_HOST, EBAY_HOST, INDEX_HOSTS, USER_AGENT

log = logging.getLogger(__name__)

EXPECTED_COLS = ["int", "text", "gb", "text", "text"]
RECORD_LENGTH = 17
CHALLENGE_RE = re.compile(
    r"pardon our interruption|verify you are human|are you a human|captcha|access denied|just a moment\.\.\.|"
    r"attention required|cf-chl|security check|unusual traffic|bot detection|request unsuccessful",
    re.IGNORECASE,
)
ITEM_PATH_RE = re.compile(r"^/itm/(?:[^/]+/)?(\d{9,15})/?$")
DESCRIPTION_PATH_RE = re.compile(r"^/itmdesc/(\d{9,15})$")
BLOCK_TAGS = frozenset(
    "address article aside blockquote body center dd details dialog div dl dt fieldset figcaption figure footer "
    "form h1 h2 h3 h4 h5 h6 header hr html li main nav ol p pre section summary table tbody tfoot thead tr ul".split()
)
CELL_TAGS = frozenset({"td", "th"})
DROP_TAGS = ("script", "style", "noscript", "template", "iframe", "object", "embed", "svg", "head", "meta", "link", "canvas")
MAX_SEGMENTS = 2000
# Structural bounds for untrusted seller markup: traversal is iterative and
# rejects hostile nesting/size explicitly instead of recursing without limit.
MAX_HTML_DEPTH = 256
MAX_HTML_NODES = 200_000


class SourceError(Exception):
    """Source response is unusable: challenge, schema change or invalid payload."""

    blocked = False


class SourceBlocked(SourceError):
    """Source refused or challenged the request; back off before retrying."""

    blocked = True

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class IndexSnapshot:
    updated_at: datetime
    records: list[dict[str, Any]]
    rejected: int = 0
    duplicates: int = 0
    flags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------


def canonical_item_url(url: str) -> tuple[str, str]:
    """Return (item_id, canonical URL) for an eBay US item link or raise SourceError."""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in {"www.ebay.com", "ebay.com"} or parts.port not in (None, 443):
        raise SourceError("listing URL is not an https www.ebay.com item link")
    if parts.username or parts.password:
        raise SourceError("listing URL contains credentials")
    m = ITEM_PATH_RE.match(parts.path)
    if not m:
        raise SourceError("listing URL is not an eBay /itm/<id> link")
    item_id = m.group(1)
    return item_id, f"https://{EBAY_HOST}/itm/{item_id}"


def description_url(item_id: str) -> str:
    if not re.fullmatch(r"\d{9,15}", item_id):
        raise SourceError("invalid item id")
    return f"https://{DESCRIPTION_HOST}/itmdesc/{item_id}"


def _allowed_index(url: str) -> bool:
    p = urlsplit(url)
    return p.scheme == "https" and p.hostname in INDEX_HOSTS and p.port in (None, 443) and not p.username


def _allowed_description(url: str) -> bool:
    p = urlsplit(url)
    return (p.scheme == "https" and p.hostname == DESCRIPTION_HOST and p.port in (None, 443)
            and not p.username and bool(DESCRIPTION_PATH_RE.match(p.path)))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


class HttpFetcher:
    """Serialized fixed-origin fetcher with per-origin minimum delay."""

    def __init__(self, timeout: float, delay: float, client: httpx.Client | None = None):
        self.delay = delay
        self._last: dict[str, float] = {}
        # Collector checks ownership after pacing, immediately before every redirect/request.
        self.before_request: Callable[[], None] | None = None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.8"},
            trust_env=False,
        )

    def close(self) -> None:
        self.client.close()

    def _wait(self, origin: str) -> None:
        last = self._last.get(origin)
        if last is not None:
            remaining = self.delay - (time.monotonic() - last)
            if remaining > 0:
                time.sleep(remaining)

    def get_text(self, url: str, allowed: Callable[[str], bool], max_bytes: int) -> tuple[int, str]:
        """GET an allowlisted URL; return (status, text). Redirect hops must stay allowlisted."""
        for _ in range(5):
            if not allowed(url):
                raise SourceError("refusing non-allowlisted URL")
            origin = urlsplit(url).hostname or ""
            self._wait(origin)
            if self.before_request is not None:
                self.before_request()
            self._last[origin] = time.monotonic()
            try:
                with self.client.stream("GET", url) as response:
                    status = response.status_code
                    if status in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise SourceError(f"redirect without location from {origin}")
                        url = urljoin(url, location)
                        continue
                    if status in (403, 429, 503):
                        raise SourceBlocked(f"{origin} returned HTTP {status}", parse_retry_after(response.headers.get("retry-after")))
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise SourceError(f"{origin} response exceeds {max_bytes} bytes")
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise SourceError(f"{origin} response exceeds {max_bytes} bytes")
                        chunks.append(chunk)
                    encoding = response.encoding or "utf-8"
                    return status, b"".join(chunks).decode(encoding, errors="replace")
            except httpx.TimeoutException as exc:
                raise SourceError(f"{origin} request timed out") from exc
            except httpx.TransportError as exc:
                raise SourceError(f"{origin} transport error: {type(exc).__name__}") from exc
        raise SourceError("too many redirects")

    def fetch_index(self, url: str, max_bytes: int) -> IndexSnapshot:
        status, text = self.get_text(url, _allowed_index, max_bytes)
        if status != 200:
            raise SourceError(f"index returned HTTP {status}")
        return parse_index(text)

    def fetch_description(self, item_id: str, max_bytes: int) -> dict[str, Any] | None:
        """Return parsed description claims, or None when the listing has no description (404)."""
        status, text = self.get_text(description_url(item_id), _allowed_description, max_bytes)
        if status == 404:
            return None
        if status != 200:
            raise SourceError(f"description returned HTTP {status}")
        if _looks_like_challenge(text):
            raise SourceBlocked("description origin returned a challenge page")
        return parse_description(text)


def _looks_like_challenge(text: str) -> bool:
    head = text[:20000]
    soup = BeautifulSoup(head, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if CHALLENGE_RE.search(title):
        return True
    body = soup.get_text(" ", strip=True)[:2000]
    return bool(CHALLENGE_RE.search(body)) and len(body) < 1500


# ---------------------------------------------------------------------------
# Index parsing
# ---------------------------------------------------------------------------


def _decimal(value: Any, places: int) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SourceError("price is not numeric")
    try:
        result = Decimal(str(value)).quantize(Decimal(1).scaleb(-places))
    except InvalidOperation as exc:
        raise SourceError("price is not a finite decimal") from exc
    if not result.is_finite() or result < 0:
        raise SourceError("price is negative or invalid")
    return result


def _parse_updated(raw: Any) -> datetime:
    if not isinstance(raw, str):
        raise SourceError("index cfg.updated missing")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceError("index cfg.updated is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise SourceError("index cfg.updated lacks a timezone")
    return parsed.astimezone(timezone.utc)


def _record(raw: Any, places: int, flag_names: list[str]) -> dict[str, Any]:
    if not isinstance(raw, list) or len(raw) != RECORD_LENGTH:
        raise SourceError("index record shape changed")
    (ppu, price, shipping, condition, title, url, quantity, cores, cpu, ram, model, memory, brand, _memory2, form,
     toggle, flags) = raw
    if not isinstance(ppu, (int, float)) or isinstance(ppu, bool):
        raise SourceError("index per-unit price column changed")
    if not isinstance(title, str) or not isinstance(url, str) or not isinstance(model, str):
        raise SourceError("index text columns changed")
    if condition is not None and not isinstance(condition, str):
        raise SourceError("index condition column changed")
    if cpu is not None and not isinstance(cpu, str):
        raise SourceError("index cpu column changed")
    for value in (quantity, cores, ram):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise SourceError("index integer column changed")
    if not isinstance(flags, str) or toggle not in (0, 1):
        raise SourceError("index flag columns changed")
    item_price = _decimal(price, places)
    shipping_price = None if shipping is None else _decimal(shipping, places)
    try:
        item_id, canonical = canonical_item_url(url)
    except SourceError as exc:
        raise ValueError(str(exc)) from exc  # record-level rejection
    if ram is not None and ram < 0 or cores is not None and cores < 0:
        raise ValueError("negative hardware value")
    flag_list = []
    for token in flags.split():
        if token.isdigit() and int(token) < len(flag_names):
            flag_list.append(flag_names[int(token)])
    cpu_model, cpu_count = domain.parse_index_cpu(cpu)
    vendor = domain.normalize_vendor(brand if isinstance(brand, str) else None)
    clean_title = domain.clean_text(title, 300)
    return {
        "item_id": item_id,
        "source_url": canonical,
        "original_url": url[:2000],
        "title": clean_title,
        "condition": condition,
        "item_price": item_price,
        "shipping_price": shipping_price,
        "quantity": quantity,
        "total_cores": cores,
        "cpu_raw": cpu,
        "cpu_model": cpu_model,
        "cpu_count": cpu_count,
        "source_ram_gb": ram,
        "vendor": vendor,
        "model": domain.normalize_model(vendor, model, clean_title),
        "brand": brand,
        "memory_type": memory,
        "form": form,
        "no_ram_flag": "No RAM" in flag_list,
        "flags": flag_list,
    }


def parse_index(html: str) -> IndexSnapshot:
    """Parse the complete embedded inventory (script#d) with its cfg metadata."""
    soup = BeautifulSoup(html, "html.parser")
    data_el = soup.find("script", id="d")
    cfg_el = soup.find("script", id="cfg")
    if data_el is None or cfg_el is None:
        if _looks_like_challenge(html) or CHALLENGE_RE.search(html[:50000]):
            raise SourceBlocked("index returned a challenge page")
        raise SourceError("index payload (script#d/#cfg) not found; page format changed")
    try:
        records_raw = json.loads(data_el.string or "")
        cfg = json.loads(cfg_el.string or "")
    except json.JSONDecodeError as exc:
        raise SourceError("index payload is not valid JSON") from exc
    if not isinstance(cfg, dict) or not isinstance(records_raw, list):
        raise SourceError("index payload has unexpected types")
    updated = _parse_updated(cfg.get("updated"))
    places = cfg.get("dec")
    if not isinstance(places, int) or not 0 <= places <= 4:
        raise SourceError("index cfg.dec changed")
    cols = cfg.get("cols")
    if not isinstance(cols, list) or [c.get("f") if isinstance(c, dict) else None for c in cols] != EXPECTED_COLS:
        raise SourceError("index cfg.cols changed; refusing to guess field offsets")
    flag_names = cfg.get("flags")
    if not isinstance(flag_names, list) or not all(isinstance(f, str) for f in flag_names):
        raise SourceError("index cfg.flags changed")
    if not records_raw:
        raise SourceError("index payload is empty")
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    rejected = duplicates = 0
    for raw in records_raw:
        try:
            rec = _record(raw, places, flag_names)
        except ValueError:
            rejected += 1
            continue
        if rec["item_id"] in seen:
            duplicates += 1
            continue
        seen.add(rec["item_id"])
        records.append(rec)
    if not records or rejected > max(5, len(records_raw) // 10):
        raise SourceError(f"index rejected {rejected} of {len(records_raw)} records; refusing partial snapshot")
    return IndexSnapshot(updated_at=updated, records=records, rejected=rejected, duplicates=duplicates, flags=flag_names)


# ---------------------------------------------------------------------------
# Seller description parsing
# ---------------------------------------------------------------------------


def _html_segments_with_boundaries(html: str) -> tuple[list[str], set[int]]:
    """Visible text segments split on block boundaries; scripts/styles removed.

    Traversal is iterative with explicit depth and node bounds; markup nested
    deeper than ``MAX_HTML_DEPTH`` or larger than ``MAX_HTML_NODES`` raises
    ``SourceError`` rather than exhausting the interpreter stack.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []
    buf: list[str] = []
    context_ends: set[int] = set()
    groups: dict[int, int] = {}
    list_depth = 0

    def flush() -> None:
        text = " ".join("".join(buf).split())
        buf.clear()
        if text:
            out.append(text)

    # Work items, processed LIFO: ("enter", tag, depth) | ("text", str) | ("flush",)
    stack: list[tuple[Any, ...]] = [("enter", soup, 0)]
    visited = 0
    while stack and len(out) < MAX_SEGMENTS:
        item = stack.pop()
        kind = item[0]
        if kind == "flush":
            flush()
            continue
        if kind == "group_open":
            groups[item[1]] = len(out)
            if item[2] in ("ul", "ol"):
                list_depth += 1
            continue
        if kind == "group_close":
            start = groups.pop(item[1])
            if item[2] in ("ul", "ol"):
                list_depth -= 1
                if list_depth == 0:
                    context_ends.add(len(out))
            elif list_depth == 0 and item[2] in ("p", "div") and len(out) - start > 1:
                context_ends.add(len(out))
            continue
        if kind == "text":
            buf.append(item[1])
            continue
        _, node, depth = item
        if depth > MAX_HTML_DEPTH:
            raise SourceError(f"seller description nests markup deeper than {MAX_HTML_DEPTH} levels; refusing to parse")
        actions: list[tuple[Any, ...]] = []
        for child in node.children:
            visited += 1
            if visited > MAX_HTML_NODES:
                raise SourceError(f"seller description exceeds {MAX_HTML_NODES} markup nodes; refusing to parse")
            if isinstance(child, (Comment, Doctype, ProcessingInstruction, Declaration, CData)):
                continue
            if isinstance(child, NavigableString):
                actions.append(("text", str(child)))
            elif child.name in DROP_TAGS:
                continue
            elif child.name == "br":
                actions.append(("flush",))
            elif child.name in BLOCK_TAGS:
                actions.extend((
                    ("flush",), ("group_open", id(child), child.name), ("enter", child, depth + 1),
                    ("flush",), ("group_close", id(child), child.name),
                ))
            elif child.name in CELL_TAGS:
                actions.extend((("text", " "), ("enter", child, depth + 1), ("text", " : " if child.name == "th" else " ")))
            else:
                actions.append(("enter", child, depth + 1))
        stack.extend(reversed(actions))
    flush()
    return out[:MAX_SEGMENTS], context_ends


def html_segments(html: str) -> list[str]:
    """Visible block-separated text, without exposing markup or structural metadata."""
    return _html_segments_with_boundaries(html)[0]


def parse_description(html: str) -> dict[str, Any]:
    """Extract explicit seller claims; option lists never confirm installed hardware."""
    segments, context_ends = _html_segments_with_boundaries(html)
    result = domain.summarize_claims(domain.extract_claims(segments, contextual=True, context_ends=context_ends))
    result["text_excerpt"] = domain.clean_text(" | ".join(segments), 1500)
    return result
