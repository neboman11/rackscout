"""US retailer catalog connectors (TechMikeNY, SaveMyServer, Cloud Ninjas).

All transport goes through the shared :class:`rackscout.sources.HttpFetcher`
(configured per-origin pacing, manual allowlisted redirects, 403/429/503 ->
``SourceBlocked``). This module only builds fixed, allowlisted GET URLs, parses
public JSON and normalises it. Nothing here posts, builds carts, or follows
arbitrary URLs.

Truthfulness rules enforced by the parsers:

* USD only, and only on explicit publisher evidence (TechMikeNY ``price.currency``;
  Shopify ``variants[].price_currency`` from the single-product JSON).
* A native variant is *paired* (a concrete purchasable configuration) only when
  its explicit price belongs to a known installed configuration. Builder, chassis,
  generic platform and capacity text ("up to", "supports", "max") never become
  installed hardware.
* Shipping is never invented. Quote-only / call-for-availability products carry
  no price and are never available.
* Catalog snapshots report ``complete=False`` whenever pagination stopped early,
  a row was rejected, or the first page was unusable, so callers cannot archive
  rows from an incomplete view.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import cache
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit

from . import domain
from .sources import HttpFetcher, SourceBlocked, SourceError, _looks_like_challenge, html_segments

PAGE_SIZE = 100
MAX_PAGES = 25
MAX_DESCRIPTION_CHARS = 8000
MAX_VARIANTS = 250
MAX_ROWS_PER_PAGE = 250
MAX_PRICE = Decimal("10000000")


@dataclass(frozen=True)
class Retailer:
    provider: str
    name: str
    origin: str
    catalog_url: str
    country: str = "US"


RETAILERS: dict[str, Retailer] = {
    "TechMikeNY": Retailer("TechMikeNY", "TechMikeNY", "techmikeny.com",
                           "https://techmikeny.com/api/agent/collections/servers"),
    "SaveMyServer": Retailer("SaveMyServer", "SaveMyServer", "savemyserver.com",
                             "https://savemyserver.com/collections/pre-configured-servers/products.json"),
    "CloudNinjas": Retailer("CloudNinjas", "Cloud Ninjas", "cloudninjas.com",
                            "https://cloudninjas.com/collections/cloud-ninjas-servers/products.json"),
}


@dataclass
class CatalogSnapshot:
    records: list[dict[str, Any]]
    observed_at: datetime
    upstream_updated_at: datetime | None = None
    complete: bool = True
    rejected: int = 0
    duplicates: int = 0


class _Skip(Exception):
    """Well-formed product that is intentionally out of scope (tower/workstation/accessory); not a failure."""


class _Reject(Exception):
    """One product row is malformed or fails a safety rule; the rest of the catalog stays usable."""


# ---------------------------------------------------------------------------
# URL grammar
# ---------------------------------------------------------------------------

_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,254}$")
_CURSOR_RE = re.compile(r"^[A-Za-z0-9+/=_-]{1,512}$")
_DIGITS_RE = re.compile(r"^[0-9]{1,4}$")
_TM_PRODUCT_PATH = re.compile(r"^/api/agent/products/([a-z0-9][a-z0-9_-]{0,254})$")
_PAGE_PATH = re.compile(r"^/products/([a-z0-9][a-z0-9_-]{0,254})$")
_JSON_PATH = re.compile(r"^/products/([a-z0-9][a-z0-9_-]{0,254})\.json$")
_JS_PATH = re.compile(r"^/products/([a-z0-9][a-z0-9_-]{0,254})\.js$")


def _retailer(provider: str) -> Retailer:
    try:
        return RETAILERS[provider]
    except (KeyError, TypeError):
        raise ValueError(f"unknown retail provider: {provider!r}") from None


def _query(parts, allowed: set[str]) -> dict[str, str] | None:
    if not parts.query:
        return {}
    try:
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys) or not set(keys) <= allowed:
        return None
    return dict(pairs)


def allowed_url(provider: str, url: str) -> bool:
    """True only for this provider's fixed HTTPS hostname/path/query grammar."""
    retailer = RETAILERS.get(provider) if isinstance(provider, str) else None
    if retailer is None or not isinstance(url, str) or len(url) > 2048:
        return False
    if any(ord(c) <= 32 or ord(c) == 127 or c in "\\#" for c in url) or url.endswith("?"):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if (parts.scheme != "https" or parts.hostname != retailer.origin or port is not None
            or parts.username is not None or parts.password is not None or parts.fragment or "%" in parts.path):
        return False
    path = parts.path
    catalog = urlsplit(retailer.catalog_url).path
    if path == catalog:
        if provider == "TechMikeNY":
            q = _query(parts, {"first", "after"})
            if q is None:
                return False
            if "first" in q and not (_DIGITS_RE.match(q["first"]) and 1 <= int(q["first"]) <= 100):
                return False
            return "after" not in q or bool(_CURSOR_RE.match(q["after"]))
        q = _query(parts, {"limit", "page"})
        if q is None:
            return False
        if "limit" in q and not (_DIGITS_RE.match(q["limit"]) and 1 <= int(q["limit"]) <= 250):
            return False
        return "page" not in q or bool(_DIGITS_RE.match(q["page"]) and 1 <= int(q["page"]) <= MAX_PAGES)
    if parts.query:
        return False
    if _PAGE_PATH.match(path):
        return True
    if provider == "TechMikeNY":
        return bool(_TM_PRODUCT_PATH.match(path))
    return bool(_JSON_PATH.match(path) or _JS_PATH.match(path))


def _canonical_url(provider: str, handle: str) -> str:
    return f"https://{RETAILERS[provider].origin}/products/{handle}"


def _product_url(provider: str, handle: str) -> str:
    origin = RETAILERS[provider].origin
    if provider == "TechMikeNY":
        return f"https://{origin}/api/agent/products/{handle}"
    return f"https://{origin}/products/{handle}.json"


# ---------------------------------------------------------------------------
# Transport helpers (shared HttpFetcher only)
# ---------------------------------------------------------------------------


def _iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("observed_at and fresh_until must be timezone-aware datetimes")
    return value.astimezone(timezone.utc).isoformat()


def _get(provider: str, fetcher: HttpFetcher, url: str, max_bytes: int) -> tuple[int, str]:
    return fetcher.get_text(url, lambda candidate: allowed_url(provider, candidate), max_bytes)


def _load(text: str) -> Any:
    def refuse(_: str) -> Any:
        raise ValueError("non-finite number")

    try:
        return json.loads(text, parse_constant=refuse)
    except (ValueError, RecursionError):
        if _looks_like_challenge(text):
            raise SourceBlocked("retailer returned a challenge page") from None
        raise SourceError("retailer response is not valid JSON") from None


# ---------------------------------------------------------------------------
# Text and fact extraction
# ---------------------------------------------------------------------------

_TM_LABELS = ("Model", "Processors", "Processor", "Memory", "Hard Drives", "Hard Drive", "Power Supplies",
              "RAID Controller", "Optical Drive", "Remote Access Controller", "Front Bezel", "Rail Kit", "Software",
              "Network Interface", "Graphics Card", "Condition", "Warranty")
_TM_LABEL_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(re.escape(x) for x in _TM_LABELS) + r")\s*:")
_TM_KEEP = ("Model", "Processors", "Memory", "Hard Drives", "Power Supplies", "RAID Controller", "Network Interface")
_TM_ALIASES = {"Processor": "Processors", "Hard Drive": "Hard Drives"}
_CONDITION_RE = re.compile(r"\bcondition\s*:\s*(new|used|refurbished|renewed|open box|pre-?owned|like new)\b", re.I)
_QUOTE_RE = re.compile(
    r"call\s+for\s+(?:availability|price|pricing|quote)|(?:request|get)\s+(?:a\s+)?quote|quote[\s-]+only|"
    r"contact\s+(?:us\s+)?for\s+(?:a\s+)?(?:quote|pricing|price|availability)", re.I)
_BUILDER_HANDLE_RE = re.compile(r"design-lab|(?:^|-)configured-\d+$")
_BUILDER_TEXT_RE = re.compile(r"configure\s+your|custom\s+configuration|build\s+your\s+own|design\s+lab", re.I)
_CHASSIS_TYPE_RE = re.compile(r"chassis|\bcto\b|barebone", re.I)
_LEAD_COUNT_RE = re.compile(r"^\s*(\d{1,2})\s*[x×]\s*", re.I)
_CPU_WORD_RE = re.compile(r"\b(?:xeon|epyc|ryzen|threadripper|core\s*i\d|platinum|gold|silver|bronze)\b", re.I)
_CPU_LINE_RE = re.compile(r"^\s*(\d{1,2})\s*[x×]\s*(.+?)\s*$", re.I)
_CORES_RE = re.compile(r"(\d{1,4})\s*[x×]?\s*cores?\s+total|total\s+cores?\s*[:=-]?\s*(\d{1,4})", re.I)
_NO_RAM_RE = re.compile(r"^\s*(?:none\b|no\s+(?:ram|memory|dimms?)\b)|\b(?:without|no)\s+(?:any\s+)?(?:ram|memory)\b", re.I)
_RAM_TOTAL_RE = re.compile(r"(\d{1,5}(?:\.\d+)?)\s*(GB|TB)\s+total\b", re.I)
_RAM_ANY_RE = re.compile(r"(?<![\w.])(\d{1,5}(?:\.\d+)?)\s*(GB|TB)\b", re.I)
_RAM_WORD_RE = re.compile(r"\b(?:memory|ram|dimms?)\b", re.I)
_OFFERED_RE = re.compile(r"^\s*server\s+specifications\s+offered\s*:?\s*$", re.I)
_OPTION_NAME_RE = {
    "ram": re.compile(r"\b(?:memory|ram)\b", re.I),
    "cpu": re.compile(r"\b(?:cpu|processors?)\b", re.I),
    "hw": re.compile(r"\b(?:storage|drives?|hdd|ssd|nvme|raid|disks?)\b", re.I),
}
_VENDOR_HINTS = (
    ("Dell", re.compile(r"\bdell(?:\s+emc)?\b|\bpoweredge\b", re.I)),
    ("HPE", re.compile(r"\bhpe?\b|\bhewlett[\s-]packard\b|\bproliant\b", re.I)),
    ("Supermicro", re.compile(r"\bsuper\s*micro\b|\b(?:sys|as|ssg)-\d{4}", re.I)),
    ("Lenovo", re.compile(r"\blenovo\b|\bthinksystem\b", re.I)),
    ("Cisco", re.compile(r"\bcisco\b|\bucs\b", re.I)),
)
_RACK_RE = re.compile(r"\brack(?:mount)?\b|\b[1-4]\s?u\b", re.I)
_TOWER_RE = re.compile(r"\btower\b|\bworkstation\b|\bthinksystem\s+st\d{2,3}\b|\bpoweredge\s+t\d{2,3}\b|\bproliant\s+ml\d{2,3}\b", re.I)
_SERVER_WORD_RE = re.compile(r"\b(?:server|poweredge|proliant|thinksystem|chassis|barebones?)\b", re.I)
_ACCESSORY_RE = re.compile(
    r"\b(?:ddr[2-5]|rdimm|lrdimm|dimm|memory\s+kit|ssd|hdd|hard\s+drive|rail\s+kit|rails|power\s+supply|psu|cable|"
    r"heatsink|caddy|tray|bezel|nic|transceiver|gpu\s+card|raid\s+card|accessor(?:y|ies))\b", re.I)
_STOCK_BANDS = frozenset({"0", "1-25", "25+", "50+", "100+"})


def _plain(text: str) -> str:
    lines = (" ".join(re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", line).split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)[:MAX_DESCRIPTION_CHARS]


def _html_text(html: str) -> str:
    try:
        return _plain("\n".join(html_segments(html)))
    except SourceError as exc:
        raise _Reject(str(exc)) from None


def _option_kind(name: str) -> str | None:
    for kind, pattern in _OPTION_NAME_RE.items():
        if pattern.search(name):
            return kind
    return None


def _cpu_from_text(text: str) -> str | None:
    """``N× model`` from an explicit CPU statement, or None when absent/ambiguous."""
    models = domain.parse_cpu(text)
    names = {model for model, _ in models}
    if len(names) == 1:
        model = next(iter(names))
        counts = {count for _, count in models if count}
        lead = _LEAD_COUNT_RE.match(text)
        count = int(lead.group(1)) if lead else (next(iter(counts)) if len(counts) == 1 else None)
        return f"{count}× {model}" if count else model
    if not names:
        m = _CPU_LINE_RE.match(text)
        if m and _CPU_WORD_RE.search(m.group(2)):
            return f"{int(m.group(1))}× {m.group(2)}"
    return None


def _gb(number: str, unit: str) -> int | None:
    value = float(number) * (1024 if unit.upper() == "TB" else 1)
    return int(value) if value == int(value) and 1 <= value <= 24576 else None


def _ram_from_memory_text(text: str) -> int | None:
    """Installed GB from text already known to describe memory; 0 only for explicit absence."""
    if domain.CAPACITY_RE.search(text):
        return None
    if _NO_RAM_RE.search(text):
        return 0
    if m := _RAM_TOTAL_RE.search(text):
        return _gb(*m.groups())
    products = {int(a) * int(b) for a, b in domain.RAM_PRODUCT_RE.findall(text)}
    if len(products) == 1:
        value = products.pop()
        return value if 1 <= value <= 24576 else None
    found = {_gb(n, u) for n, u in _RAM_ANY_RE.findall(text)}
    return found.pop() if len(found) == 1 and None not in found else None


def _ram_from_title(title: str) -> int | None:
    found = set()
    multipliers = [m for m in domain.RAM_PRODUCT_RE.finditer(title)
                   if not domain.STORAGE_AFTER_RE.match(title[m.end():])]
    for m in multipliers:
        value = int(m.group(1)) * int(m.group(2))
        found.add(value if 1 <= value <= 24576 else None)
    for m in _RAM_ANY_RE.finditer(title):
        if any(product.start() <= m.start() and m.end() <= product.end() for product in multipliers):
            continue
        if m.group(2).upper() == "GB" and not domain.STORAGE_AFTER_RE.match(title[m.end():]):
            found.add(_gb(m.group(1), m.group(2)))
    return found.pop() if len(found) == 1 and None not in found else None


@dataclass
class _Facts:
    cpu_raw: str | None = None
    ram: int | None = None
    cores: int | None = None
    installed_text: str = ""


def _techmikeny_facts(description: str) -> _Facts:
    """Fixed labelled fields only; capacity phrasing is dropped, never read as installed hardware."""
    flat = " ".join(description.split())
    matches = list(_TM_LABEL_RE.finditer(flat))
    fields: dict[str, str] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(flat)
        label = _TM_ALIASES.get(m.group(1), m.group(1))
        value = flat[m.end():end].strip()
        if value and label not in fields:
            fields[label] = value
    kept = {k: v for k, v in fields.items() if k in _TM_KEEP and not domain.CAPACITY_RE.search(v)}
    facts = _Facts(installed_text="\n".join(f"{k}: {kept[k]}" for k in _TM_KEEP if k in kept))
    if "Processors" in kept:
        facts.cpu_raw = _cpu_from_text(kept["Processors"])
        if c := _CORES_RE.search(kept["Processors"]):
            facts.cores = int(c.group(1) or c.group(2))
    if "Memory" in kept:
        facts.ram = _ram_from_memory_text(kept["Memory"])
    return facts


def _offered_facts(description: str) -> _Facts:
    """Cloud Ninjas 'Server Specifications Offered' list; platform maxima elsewhere are ignored."""
    lines = description.split("\n")
    start = next((i for i, line in enumerate(lines) if _OFFERED_RE.match(line)), None)
    if start is None:
        return _Facts()
    kept: list[str] = []
    for line in lines[start + 1:start + 41]:
        if line.endswith(":"):
            break
        if domain.CAPACITY_RE.search(line):
            continue
        kept.append(line)
    cpus = {c for c in map(_cpu_from_text, kept) if c}
    rams = {r for r in (_ram_from_memory_text(line) for line in kept if _RAM_WORD_RE.search(line)) if r is not None}
    return _Facts(cpu_raw=cpus.pop() if len(cpus) == 1 else None, ram=rams.pop() if len(rams) == 1 else None,
                  installed_text="\n".join(kept))


def _title_facts(title: str) -> _Facts:
    if domain.CAPACITY_RE.search(title):
        return _Facts()
    cpu, ram = _cpu_from_text(title), _ram_from_title(title)
    return _Facts(cpu_raw=cpu, ram=ram, installed_text=domain.clean_text(title, 300) if cpu or ram else "")


@cache
def _catalog_models() -> frozenset[str]:
    data = json.loads((domain.DATA_DIR / "models.json").read_text(encoding="utf-8"))
    return frozenset(m["model"] for m in data["models"])


def _identity(title: str, vendor_field: Any) -> tuple[str | None, str | None]:
    hits = {name for name, pattern in _VENDOR_HINTS if pattern.search(title)}
    if len(hits) == 1:
        vendor = next(iter(hits))
    elif not hits:
        vendor = domain.normalize_vendor(vendor_field if isinstance(vendor_field, str) else None)
    else:
        vendor = None
    if vendor is None:
        return None, None
    model_text: str | None = None
    if vendor == "Dell":
        found = {m.upper().replace("XD", "xd") for m in domain.MODEL_RE.findall(title)}
        model_text = found.pop() if len(found) == 1 else None
    elif vendor == "HPE":
        found = {" ".join(f"{dl} {gen}".split()) for dl, gen in re.findall(r"\b(DL\d{3}[A-Za-z]?)\b\s*(Gen\s*\d+)?", title, re.I)}
        model_text = found.pop().upper().replace("GEN", "Gen") if len(found) == 1 else None
    elif vendor == "Lenovo":
        found = {m.upper() for m in re.findall(r"\b(S[RT]\d{3})\b", title, re.I)}
        model_text = found.pop() if len(found) == 1 else None
    elif vendor == "Cisco":
        found = {f"UCS {c.upper()} {m.upper()}" for c, m in re.findall(r"\b(C\d{3})\s*(M\d)\b", title, re.I)}
        model_text = found.pop() if len(found) == 1 else None
    model = domain.normalize_model(vendor, model_text, title)
    return vendor, model if model in _catalog_models() else None


# ---------------------------------------------------------------------------
# Row normalisation
# ---------------------------------------------------------------------------


def _price(value: Any) -> Decimal | None:
    """Explicit positive USD amount; zero means no quote. Anything malformed rejects the row."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise _Reject("price is not numeric")
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation:
        raise _Reject("price is not numeric") from None
    if not amount.is_finite() or amount < 0 or amount > MAX_PRICE:
        raise _Reject("price out of range")
    amount = amount.quantize(Decimal("0.01"))
    return amount if amount > 0 else None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _required_str(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _Reject(f"{key} missing")
    return value


def _optional_str(row: dict[str, Any], key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _Reject(f"{key} is not text")
    return value


def _handle(row: dict[str, Any]) -> str:
    handle = row.get("handle")
    if not isinstance(handle, str) or not _HANDLE_RE.match(handle):
        raise _Reject("handle is not a safe product handle")
    return handle


def _tm_variants(row: dict[str, Any]) -> list[dict[str, Any]]:
    raw = row.get("variants")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_VARIANTS:
        raise _Reject("variants missing")
    out, seen = [], set()
    for v in raw:
        if not isinstance(v, dict):
            raise _Reject("variant is not an object")
        m = re.fullmatch(r"gid://shopify/ProductVariant/([0-9]{1,20})", v.get("id") if isinstance(v.get("id"), str) else "")
        if not m or m.group(1) in seen or not isinstance(v.get("available"), bool):
            raise _Reject("variant identity or availability invalid")
        seen.add(m.group(1))
        title = v.get("title")
        if title is not None and not isinstance(title, str):
            raise _Reject("variant title invalid")
        price = None
        if v.get("price") is not None:
            quote_ = v["price"]
            if not isinstance(quote_, dict) or quote_.get("currency") != "USD":
                raise _Reject("variant price lacks explicit USD currency")
            price = _price(quote_.get("amount"))
        label = domain.clean_text(title, 120) if title and title.strip() and title != "Default Title" else None
        labels = {}
        if (label and _RAM_WORD_RE.search(label) and not _CPU_WORD_RE.search(label)
                and not re.search(r"\b(?:hdd|ssd|nvme|storage|drives?|raid)\b", label, re.I)
                and (_ram_from_memory_text(label) is not None or domain.CAPACITY_RE.search(label))):
            labels["Memory"] = label
        out.append({"variant_id": m.group(1), "label": label, "labels": labels, "price": price,
                    "available": bool(v["available"]) and price is not None})
    return out


def _shopify_variants(row: dict[str, Any]) -> list[dict[str, Any]]:
    raw = row.get("variants")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_VARIANTS:
        raise _Reject("variants missing")
    options = row.get("options") if isinstance(row.get("options"), list) else []
    names = [o["name"] if isinstance(o, dict) and isinstance(o.get("name"), str) else f"Option {i}"
             for i, o in enumerate(options, 1)]
    out, seen = [], set()
    for v in raw:
        vid = v.get("id") if isinstance(v, dict) else None
        if isinstance(vid, bool) or not isinstance(vid, int) or vid <= 0 or vid in seen:
            raise _Reject("variant id invalid")
        seen.add(vid)
        if not isinstance(v.get("available"), bool):
            raise _Reject("variant availability not explicitly stated")
        price = _price(v.get("price"))
        labels: dict[str, str] = {}
        for i in (1, 2, 3):
            value = v.get(f"option{i}")
            if value is None:
                continue
            if not isinstance(value, str):
                raise _Reject("variant option invalid")
            name = names[i - 1] if i <= len(names) else f"Option {i}"
            if not (name == "Title" and value == "Default Title"):
                labels[domain.clean_text(name, 60)] = domain.clean_text(value, 120)
        title = v.get("title")
        label = domain.clean_text(title, 120) if isinstance(title, str) and title.strip() and title != "Default Title" else None
        out.append({"variant_id": str(vid), "label": label, "labels": labels, "price": price,
                    "available": price is not None and v["available"]})
    return out




def _fact_dims(variants: list[dict[str, Any]]) -> tuple[bool, bool, bool]:
    """(attributable, ram_varies, cpu_varies): whether per-variant labels explain every variant's hardware."""
    if len(variants) == 1:
        kinds = {_option_kind(name) for name in variants[0]["labels"]}
        return True, "ram" in kinds, "cpu" in kinds
    kinds: set[str] = set()
    seen: set[tuple[tuple[str, str], ...]] = set()
    for v in variants:
        if not v["labels"]:
            return False, False, False
        for name in v["labels"]:
            kind = _option_kind(name)
            if kind is None:
                return False, False, False
            kinds.add(kind)
        key = tuple(sorted(v["labels"].items()))
        if key in seen:
            return False, False, False
        seen.add(key)
    return True, "ram" in kinds, "cpu" in kinds


def variant_facts(record: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
    """CPU / RAM that apply to one native variant: product-level common facts overridden by hardware option labels."""
    cpu, ram = record.get("cpu_raw"), record.get("source_ram_gb")
    for name, value in (variant.get("labels") or {}).items():
        kind = _option_kind(name)
        if kind == "cpu":
            cpu = None if domain.CAPACITY_RE.search(value) else _cpu_from_text(value)
        elif kind == "ram":
            ram = _ram_from_memory_text(value)
    return {"cpu_raw": cpu, "source_ram_gb": ram}


def _build(provider: str, *, item_id: str, handle: str, title: str, description: str, product_type: str | None,
           variants: list[dict[str, Any]], vendor_field: Any, upstream: datetime | None, warranty: dict[str, Any] | None,
           stock_band: str | None, original_url: str | None, currency_evidence: str, method: str,
           observed: str, fresh: str, shopify: bool) -> dict[str, Any]:
    clean_title = domain.clean_text(title, 300)
    scope_text = f"{title} {product_type or ''}"
    if _TOWER_RE.search(scope_text) and not _RACK_RE.search(scope_text):
        raise _Skip("tower/workstation product")
    if not _SERVER_WORD_RE.search(scope_text) and _ACCESSORY_RE.search(scope_text):
        raise _Skip("accessory or component product")
    haystack = f"{title}\n{description}"
    priced = [v for v in variants if v["price"] is not None]
    quote_only = not priced or bool(_QUOTE_RE.search(haystack))
    builder = (shopify and bool(_BUILDER_HANDLE_RE.search(handle) or _BUILDER_TEXT_RE.search(haystack))) \
        or bool(_BUILDER_TEXT_RE.search(title))
    chassis = bool(product_type and _CHASSIS_TYPE_RE.search(product_type)) and not shopify
    attributable, ram_varies, cpu_varies = _fact_dims(variants)

    facts = _Facts()
    if attributable and not builder:
        if not shopify:
            facts = _techmikeny_facts(description)
        elif provider == "CloudNinjas":
            facts = _offered_facts(description)
        else:
            facts = _title_facts(title)
    if ram_varies:
        facts.ram = None
    if cpu_varies:
        facts.cpu_raw = None

    record = {
        "provider": provider, "item_id": item_id, "source_url": _canonical_url(provider, handle),
        "original_url": original_url or _canonical_url(provider, handle), "title": clean_title,
        "condition": (m.group(1).title() if (m := _CONDITION_RE.search(description)) else None),
        "item_price": None, "shipping_price": None, "currency": "USD",
        "cpu_raw": facts.cpu_raw, "source_ram_gb": facts.ram, "total_cores": facts.cores,
        "no_ram_flag": facts.ram == 0, "flags": ["No RAM"] if facts.ram == 0 else [],
    }
    record["vendor"], record["model"] = _identity(clean_title, vendor_field)

    configured = False
    if not quote_only and not builder and not chassis and attributable:
        per_variant = [variant_facts(record, v) for v in priced]
        configured = all(f["cpu_raw"] and f["source_ram_gb"] is not None for f in per_variant)
    kind = "quote_only" if quote_only else "configured" if configured else "configurable"

    out_variants = []
    for v in variants:
        out_variants.append({"variant_id": v["variant_id"], "label": v["label"], "labels": v["labels"],
                             "price": None if quote_only or v["price"] is None else f"{v['price']:.2f}",
                             "available": bool(v["available"]) and not quote_only,
                             "paired": configured and v["price"] is not None})
    available = any(v["available"] for v in out_variants)
    shown = [v for v in out_variants if v["price"] is not None]
    pool = [v for v in shown if v["available"]] or shown
    record["item_price"] = min((Decimal(v["price"]) for v in pool), default=None)
    record["item_price"] = None if record["item_price"] is None else f"{record['item_price']:.2f}"
    record["retail"] = {
        "handle": handle, "product_type": product_type, "kind": kind, "description_text": description,
        "installed_text": facts.installed_text, "warranty": warranty,
        "upstream_updated_at": upstream.isoformat() if upstream else None,
        "observed_at": observed, "fresh_until": fresh, "method": method, "currency_evidence": currency_evidence,
        "variants": out_variants, "available": available, "stock_band": stock_band,
    }
    return record


def _normalize_techmikeny(row: Any, *, method: str, observed: str, fresh: str) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise _Reject("row is not an object")
    m = re.fullmatch(r"gid://shopify/Product/([0-9]{1,20})", row.get("id") if isinstance(row.get("id"), str) else "")
    if not m:
        raise _Reject("product id invalid")
    handle = _handle(row)
    title = _required_str(row, "title")
    description = _plain(_optional_str(row, "description") or "")
    variants = _tm_variants(row)
    url = row.get("url")
    original = url if isinstance(url, str) and allowed_url("TechMikeNY", url) else None
    warranty = row.get("warranty")
    if isinstance(warranty, dict):
        years = warranty.get("years")
        label = warranty.get("label")
        warranty = {"included": warranty.get("included") if isinstance(warranty.get("included"), bool) else None,
                    "years": years if isinstance(years, int) and not isinstance(years, bool) else None,
                    "label": domain.clean_text(label, 120) if isinstance(label, str) else None}
    else:
        warranty = None
    band = row.get("totalInventory")
    return _build("TechMikeNY", item_id=m.group(1), handle=handle, title=title, description=description,
                  product_type=_optional_str(row, "productType"), variants=variants, vendor_field=None, upstream=None,
                  warranty=warranty, stock_band=band if isinstance(band, str) and band in _STOCK_BANDS else None, original_url=original,
                  currency_evidence="TechMikeNY API variants[].price.currency=USD", method=method,
                  observed=observed, fresh=fresh, shopify=False)


def _normalize_shopify(provider: str, row: Any, *, evidence: str, method: str, observed: str, fresh: str) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise _Reject("row is not an object")
    pid = row.get("id")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise _Reject("product id invalid")
    handle = _handle(row)
    title = _required_str(row, "title")
    body = _optional_str(row, "body_html") or ""
    description = _html_text(body)
    variants = _shopify_variants(row)
    stamps = [_timestamp(row.get("updated_at"))] + [_timestamp(v.get("updated_at")) for v in row["variants"]]
    stamps = [s for s in stamps if s]
    return _build(provider, item_id=str(pid), handle=handle, title=title, description=description,
                  product_type=_optional_str(row, "product_type"), variants=variants, vendor_field=row.get("vendor"),
                  upstream=max(stamps) if stamps else None, warranty=None, stock_band=None, original_url=None,
                  currency_evidence=evidence, method=method, observed=observed, fresh=fresh, shopify=True)


def _shopify_currency(product: Any, handle: str) -> str:
    """Explicit USD evidence from single-product JSON, else SourceError (never assumed, never `$` alone)."""
    variants = product.get("variants") if isinstance(product, dict) else None
    if not isinstance(variants, list) or not variants:
        raise SourceError("product JSON lacks variants for currency confirmation")
    if not all(isinstance(v, dict) and isinstance(v.get("price_currency"), str)
               and v["price_currency"] == "USD" for v in variants):
        raise SourceError("storefront currency is not explicitly USD")
    return f"Shopify product JSON variants[].price_currency=USD ({handle})"


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class _Collector:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.seen: set[str] = set()
        self.rejected = 0
        self.duplicates = 0

    def add_page(self, rows: list[Any], normalize) -> int:
        """Ingest rows; returns how many were new unique records."""
        new = 0
        for row in rows:
            try:
                record = normalize(row)
            except _Skip:
                continue
            except _Reject:
                self.rejected += 1
                continue
            if record["item_id"] in self.seen:
                self.duplicates += 1
                continue
            self.seen.add(record["item_id"])
            self.records.append(record)
            new += 1
        return new

    def snapshot(self, observed_at: datetime, complete: bool) -> CatalogSnapshot:
        stamps = [t for r in self.records if (t := _timestamp(r["retail"]["upstream_updated_at"]))]
        return CatalogSnapshot(records=self.records, observed_at=observed_at, upstream_updated_at=max(stamps, default=None),
                               complete=complete and self.rejected == 0, rejected=self.rejected,
                               duplicates=self.duplicates)


def _page_rows(text: str) -> tuple[Any, list[Any]]:
    data = _load(text)
    rows = data.get("products") if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) > MAX_ROWS_PER_PAGE:
        raise SourceError("catalog page has no usable products list")
    return data, rows


def _ok_status(status: int, what: str) -> None:
    if status != 200:
        raise SourceError(f"{what} returned HTTP {status}")


def _techmikeny_catalog(fetcher: HttpFetcher, observed_at: datetime, observed: str, fresh: str, max_bytes: int) -> CatalogSnapshot:
    base = RETAILERS["TechMikeNY"].catalog_url
    collector = _Collector()
    normalize = lambda row: _normalize_techmikeny(row, method="retailer_catalog", observed=observed, fresh=fresh)
    cursor: str | None = None
    seen_cursors: set[str] = set()
    complete = True
    for page in range(1, MAX_PAGES + 1):
        url = f"{base}?first={PAGE_SIZE}" + (f"&after={quote(cursor, safe='')}" if cursor else "")
        status, text = _get("TechMikeNY", fetcher, url, max_bytes)
        _ok_status(status, "catalog")
        try:
            data, rows = _page_rows(text)
            info = data.get("pageInfo")
            if not isinstance(info, dict) or not isinstance(info.get("hasNextPage"), bool):
                raise SourceError("catalog pageInfo missing")
        except SourceBlocked:
            raise
        except SourceError:
            if page == 1:
                raise
            collector.rejected += 1
            complete = False
            break
        new = collector.add_page(rows, normalize)
        if rows and new == 0:
            complete = False
            break
        if page == 1 and not rows:
            complete = False  # an empty first page is never evidence of an empty store
        if not info["hasNextPage"]:
            break
        cursor = info.get("endCursor")
        if (not isinstance(cursor, str) or not _CURSOR_RE.match(cursor) or cursor in seen_cursors
                or (rows and new == 0) or page == MAX_PAGES):
            complete = False
            break
        seen_cursors.add(cursor)
    return collector.snapshot(observed_at, complete)


def _shopify_catalog(provider: str, fetcher: HttpFetcher, observed_at: datetime, observed: str, fresh: str,
                     max_bytes: int) -> CatalogSnapshot:
    base = RETAILERS[provider].catalog_url
    collector = _Collector()
    evidence: str | None = None
    complete = True

    def normalize(row: Any) -> dict[str, Any]:
        return _normalize_shopify(provider, row, evidence=evidence or "", method="retailer_catalog",
                                  observed=observed, fresh=fresh)

    for page in range(1, MAX_PAGES + 1):
        status, text = _get(provider, fetcher, f"{base}?limit={PAGE_SIZE}&page={page}", max_bytes)
        _ok_status(status, "catalog")
        try:
            _, rows = _page_rows(text)
        except SourceBlocked:
            raise
        except SourceError:
            if page == 1:
                raise
            collector.rejected += 1
            complete = False
            break
        if page == 1:
            if not rows:
                return collector.snapshot(observed_at, False)
            probe = next((r["handle"] for r in rows if isinstance(r, dict) and isinstance(r.get("handle"), str)
                          and _HANDLE_RE.match(r["handle"])), None)
            if probe is None:
                raise SourceError("no product handle available to confirm currency")
            status, body = _get(provider, fetcher, _product_url(provider, probe), max_bytes)
            _ok_status(status, "currency product JSON")
            product = _load(body)
            evidence = _shopify_currency(product.get("product") if isinstance(product, dict) else None, probe)
        new = collector.add_page(rows, normalize)
        if rows and new == 0:
            complete = False
            break
        if len(rows) < PAGE_SIZE:
            break
        if (rows and new == 0) or page == MAX_PAGES:
            complete = False
            break
    return collector.snapshot(observed_at, complete)


def fetch_catalog(provider: str, fetcher: HttpFetcher, *, observed_at: datetime, fresh_until: datetime,
                  max_bytes: int) -> CatalogSnapshot:
    """Walk one retailer's allowlisted public catalog. Blocks and transport errors propagate."""
    _retailer(provider)
    observed, fresh = _iso(observed_at), _iso(fresh_until)
    if provider == "TechMikeNY":
        return _techmikeny_catalog(fetcher, observed_at, observed, fresh, max_bytes)
    return _shopify_catalog(provider, fetcher, observed_at, observed, fresh, max_bytes)


# ---------------------------------------------------------------------------
# Single product
# ---------------------------------------------------------------------------


def _record_handle(record: dict[str, Any]) -> str:
    retail = record.get("retail")
    handle = retail.get("handle") if isinstance(retail, dict) else None
    if not isinstance(handle, str) or not _HANDLE_RE.match(handle):
        raise SourceError("record has no safe product handle")
    return handle


def _unavailable(record: dict[str, Any], observed: str, fresh: str) -> dict[str, Any]:
    out = copy.deepcopy(record)
    retail = out["retail"]
    retail["available"] = False
    for v in retail.get("variants", []):
        v["available"] = False
    retail.update(method="retailer_product", observed_at=observed, fresh_until=fresh)
    return out


def _explicit_shopify_stock(provider: str, row: dict[str, Any], fetcher: HttpFetcher, max_bytes: int) -> dict[str, Any]:
    """Use Shopify's documented product API when public product JSON omits stock; never infer absence."""
    variants = row.get("variants")
    if not isinstance(variants, list) or not variants or not all(isinstance(v, dict) for v in variants):
        raise SourceError("product availability has invalid variants")
    if all(isinstance(v.get("available"), bool) for v in variants):
        return row
    handle = _handle(row)
    status, text = _get(provider, fetcher, f"https://{RETAILERS[provider].origin}/products/{handle}.js", max_bytes)
    _ok_status(status, "product availability")
    stock = _load(text)
    if not isinstance(stock, dict) or stock.get("id") != row.get("id") or stock.get("handle") != handle:
        raise SourceError("availability response belongs to a different product")
    offers = stock.get("variants")
    if not isinstance(offers, list) or len(offers) != len(variants):
        raise SourceError("availability response has incomplete variants")
    by_id = {}
    for offer in offers:
        vid = offer.get("id") if isinstance(offer, dict) else None
        if isinstance(vid, bool) or not isinstance(vid, int) or vid <= 0 or vid in by_id:
            raise SourceError("availability variant identity is invalid")
        by_id[vid] = offer
    result = copy.deepcopy(row)
    for variant in result["variants"]:
        vid = variant.get("id")
        if isinstance(vid, bool) or not isinstance(vid, int) or vid not in by_id:
            raise SourceError("availability response has different variants")
        offer = by_id[vid]
        cents = offer.get("price")
        if isinstance(cents, bool) or not isinstance(cents, int) or cents < 0:
            raise SourceError("availability response has no numeric variant price")
        if Decimal(cents) / 100 != (_price(variant.get("price")) or Decimal(0)):
            raise SourceError("product price changed while checking availability")
        if not isinstance(offer.get("available"), bool):
            raise SourceError("availability is not explicitly stated")
        variant["available"] = offer["available"]
    return result


def fetch_product(provider: str, record: dict[str, Any], fetcher: HttpFetcher, *, observed_at: datetime,
                  fresh_until: datetime, max_bytes: int) -> dict[str, Any]:
    """Re-quote exactly one product; HTTP 404 yields an explicit unavailable copy, anything else unusable raises."""
    _retailer(provider)
    observed, fresh = _iso(observed_at), _iso(fresh_until)
    if not isinstance(record, dict) or record.get("provider") != provider:
        raise SourceError("record belongs to a different provider")
    item_id = record.get("item_id")
    if not isinstance(item_id, str) or not re.fullmatch(r"[0-9]{1,20}", item_id):
        raise SourceError("record has no native product id")
    handle = _record_handle(record)
    status, text = _get(provider, fetcher, _product_url(provider, handle), max_bytes)
    if status == 404:
        return _unavailable(record, observed, fresh)
    _ok_status(status, "product")
    data = _load(text)
    row = data.get("product") if isinstance(data, dict) else None
    if not isinstance(row, dict):
        raise SourceError("product response has no product object")
    try:
        if provider == "TechMikeNY":
            fresh_record = _normalize_techmikeny(row, method="retailer_product", observed=observed, fresh=fresh)
        else:
            evidence = _shopify_currency(row, handle)
            row = _explicit_shopify_stock(provider, row, fetcher, max_bytes)
            fresh_record = _normalize_shopify(provider, row, evidence=evidence, method="retailer_product",
                                              observed=observed, fresh=fresh)
    except _Skip as exc:
        raise SourceError(f"product is out of scope: {exc}") from None
    except _Reject as exc:
        raise SourceError(f"product payload rejected: {exc}") from None
    if fresh_record["item_id"] != item_id or fresh_record["retail"]["handle"] != handle:
        raise SourceError("product identity does not match the requested record")
    return fresh_record
