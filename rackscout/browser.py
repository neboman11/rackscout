"""Bounded best-effort eBay item page checks with Playwright.

Runs only in the collector process, always inside Chromium's sandbox (there is
no unsandboxed mode or fallback; a sandbox launch failure is a failed check).

Network policy, installed on a fresh browser context before any page exists:

* Every HTTP(S) request goes through one context-wide route. Only the requested
  item's canonical ``https://www.ebay.com/itm/<id>`` page may load as the
  top-level document of the checker's own page; subresources are limited to
  GET/HEAD scripts, stylesheets and XHR/fetch on exact https/default-port,
  credential-free allowlisted hosts (see :func:`allowed_browser_url`).
* Allowed requests are fetched by Playwright with ``max_redirects=0``; every
  3xx answer is aborted, never followed, so no redirect hop is contacted. Only
  bounded non-redirect bodies are fulfilled. Nothing is ``continue``d.
* WebSockets are closed before connecting; popups and other unexpected pages
  are closed; a restrictive CSP is added to fulfilled responses; and the
  browser's own network stack points at a loopback sink with DNS disabled so
  any request that escapes interception cannot reach a real host.

No login, CAPTCHA solving, proxying or fingerprint evasion: a challenge is
recorded as ``blocked`` and the origin backs off.
"""
from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from .config import EBAY_HOST
from .sources import CHALLENGE_RE, SourceBlocked, SourceError, canonical_item_url

log = logging.getLogger(__name__)

ITEM_PAGE_HOST = EBAY_HOST
ASSET_HOSTS = frozenset({"ir.ebaystatic.com"})
SUBRESOURCE_HOSTS = frozenset({ITEM_PAGE_HOST}) | ASSET_HOSTS
SUBRESOURCE_TYPES = frozenset({"script", "stylesheet", "xhr", "fetch"})
SUBRESOURCE_METHODS = frozenset({"GET", "HEAD"})
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_CHECK_BYTES = 40 * 1024 * 1024
MAX_CHECK_REQUESTS = 250
# Added to every fulfilled response. connect-src lists https origins only (no
# 'self', which would also match wss:), so the page cannot open WebSockets,
# beacons or fetches elsewhere; frames, workers, plugins and form posts are off.
INJECTED_CSP = (
    "connect-src " + " ".join(f"https://{host}" for host in sorted(SUBRESOURCE_HOSTS))
    + "; frame-src 'none'; worker-src 'none'; object-src 'none'; form-action 'none'"
)
# Response headers never relayed: framing/encoding is recomputed for the
# decoded body, and Link/Alt-Svc would let the page open connections outside
# request interception (preconnect, alternative services).
DROPPED_RESPONSE_HEADERS = frozenset({
    "content-length", "content-encoding", "transfer-encoding", "connection", "keep-alive",
    "link", "alt-svc",
})
CHALLENGE_REDIRECT_RE = re.compile(r"splashui|challenge|captcha|signin", re.IGNORECASE)
# Chromium's own network stack gets no usable route: DNS resolution fails for
# every name and anything not served by the interception route is sent to a
# closed loopback port (loopback included, not bypassed). WebRTC may not use UDP.
CHROMIUM_ARGS = (
    "--host-resolver-rules=MAP * ~NOTFOUND",
    "--proxy-server=http://127.0.0.1:9",
    "--proxy-bypass-list=<-loopback>",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--disable-background-networking",
)
PRICE_RE = re.compile(r"US\s*\$\s*([\d,]+(?:\.\d{2})?)", re.IGNORECASE)
ENDED_RE = re.compile(
    r"this listing (?:was ended|has ended|sold on)|this item is out of stock|bidding has ended|"
    r"the seller has ended this listing|this listing is no longer available",
    re.IGNORECASE,
)


def _money(text: str | None) -> Decimal | None:
    if not text:
        return None
    m = PRICE_RE.search(text)
    if not m:
        return None
    try:
        return Decimal(m.group(1).replace(",", "")).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _find_price(obj: Any, depth: int = 0) -> Decimal | None:
    """Find a USD amount inside an eBay variation JSON object."""
    if depth > 8:
        return None
    if isinstance(obj, dict):
        value, currency = obj.get("value"), obj.get("currency")
        if currency == "USD" and isinstance(value, (int, float, str)) and not isinstance(value, bool):
            try:
                return Decimal(str(value)).quantize(Decimal("0.01"))
            except InvalidOperation:
                return None
        for key, child in obj.items():
            if "price" in key.lower() or key in ("value", "binModel"):
                found = _find_price(child, depth + 1)
                if found is not None:
                    return found
    return None


def extract_msku(html: str) -> dict[str, Any] | None:
    """Return the embedded MSKU variation object if present."""
    idx = html.find('"MSKU":')
    if idx < 0:
        return None
    start = html.find("{", idx)
    if start < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html[start:])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def parse_variations(msku: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """Map MSKU variations to [{variant_id, labels, price}] (bounded)."""
    menus = msku.get("selectMenus") or []
    items = msku.get("menuItemMap") or {}
    variations = msku.get("variationsMap") or {}
    combos = msku.get("variationCombinations") or {}
    if not isinstance(menus, list) or not isinstance(items, dict) or not isinstance(variations, dict):
        return []
    menu_of_item: dict[str, str] = {}
    for menu in menus:
        if not isinstance(menu, dict):
            continue
        label = str(menu.get("displayLabel") or menu.get("displayName") or "Option")[:60]
        for value_id in menu.get("menuItemValueIds") or []:
            menu_of_item[str(value_id)] = label
    labels_by_var: dict[str, dict[str, str]] = {}
    if isinstance(combos, dict) and combos:
        for combo, var_id in combos.items():
            labels: dict[str, str] = {}
            for value_id in str(combo).split("_"):
                item = items.get(value_id)
                if isinstance(item, dict):
                    labels[menu_of_item.get(value_id, "Option")] = str(item.get("displayName") or item.get("valueName") or "")[:80]
            labels_by_var[str(var_id)] = labels
    else:
        for value_id, item in items.items():
            if not isinstance(item, dict):
                continue
            for var_id in item.get("matchingVariationIds") or []:
                labels_by_var.setdefault(str(var_id), {})[menu_of_item.get(str(value_id), "Option")] = str(item.get("displayName") or "")[:80]
    out: list[dict[str, Any]] = []
    for var_id, variation in variations.items():
        if len(out) >= limit:
            break
        if not re.fullmatch(r"\d{1,20}", str(var_id)):
            continue
        price = _find_price(variation)
        labels = labels_by_var.get(str(var_id)) or {}
        if price is None or not labels:
            continue
        out.append({"variant_id": str(var_id), "labels": labels, "price": str(price)})
    return out


def allowed_browser_url(url: str, item_id: str, *, navigation: bool = False) -> bool:
    """Return whether the browser may contact ``url`` while checking ``item_id``.

    Only https on the default port without credentials is allowed. A navigation
    must be the canonical eBay item page for exactly ``item_id``; anything else
    must be on an exact allowlisted subresource host.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or "@" in parts.netloc or port not in (None, 443):
        return False
    host = parts.hostname or ""
    if not navigation:
        return host in SUBRESOURCE_HOSTS
    if host != ITEM_PAGE_HOST:
        return False
    try:
        nav_id, _ = canonical_item_url(url)
    except SourceError:
        return False
    return nav_id == item_id


def _relayed_headers(headers: dict[str, str]) -> dict[str, str]:
    out = {k.lower(): v for k, v in headers.items() if k.lower() not in DROPPED_RESPONSE_HEADERS}
    existing = out.get("content-security-policy")
    # A comma-joined header is a second policy; both are enforced.
    out["content-security-policy"] = f"{existing}, {INJECTED_CSP}" if existing else INJECTED_CSP
    return out


class _CheckGuard:
    """Network/page policy for one item check; its handlers are registered on that check's context."""

    def __init__(self, item_id: str, timeout_ms: int):
        self.item_id = item_id
        self.timeout_ms = timeout_ms
        self.page: Any = None
        self._first_page_seen = False
        self.requests = 0
        self.bytes = 0
        self.rejected = 0
        self.redirects_refused = 0
        self.websockets_closed = 0
        self.pages_closed = 0
        self.nav_redirect: tuple[int, str] | None = None
        self.nav_failure: str | None = None

    def _own_top_frame(self, request: Any) -> bool:
        if self.page is None:
            return False
        try:
            return request.frame == self.page.main_frame
        except Exception:  # noqa: BLE001 - e.g. service-worker requests have no frame
            return False

    def _rejection(self, request: Any, navigation: bool, own_top: bool) -> str | None:
        url, method = request.url, request.method.upper()
        if navigation:
            if not own_top:
                return "navigation outside the checker's own top-level page"
            if method != "GET" or not allowed_browser_url(url, self.item_id, navigation=True):
                return "navigation away from the requested item page"
            return None
        if request.resource_type not in SUBRESOURCE_TYPES or method not in SUBRESOURCE_METHODS:
            return "resource type or method not allowed"
        if not allowed_browser_url(url, self.item_id):
            return "destination not allowlisted"
        return None

    def on_route(self, route: Any) -> None:
        from playwright.sync_api import Error as PlaywrightError

        try:
            self._handle(route)
        except PlaywrightError:
            # Context closing or request cancelled mid-flight; nothing was continued.
            log.debug("browser route handler interrupted")

    def _handle(self, route: Any) -> None:
        request = route.request
        navigation = request.is_navigation_request()
        own_top = navigation and self._own_top_frame(request)
        self.requests += 1
        if self.requests > MAX_CHECK_REQUESTS:
            reason: str | None = "request budget exhausted"
        else:
            reason = self._rejection(request, navigation, own_top)
        if reason is not None:
            self.rejected += 1
            if own_top:
                self.nav_failure = reason
            route.abort("blockedbyclient")
            return
        self._relay(route, own_top)

    def _relay(self, route: Any, main: bool) -> None:
        from playwright.sync_api import Error as PlaywrightError

        try:
            response = route.fetch(max_redirects=0, timeout=self.timeout_ms)
        except PlaywrightError as exc:
            if main:
                self.nav_failure = f"fetch failed: {type(exc).__name__}"
            route.abort("failed")
            return
        try:
            status = response.status
            if 300 <= status < 400:
                self.redirects_refused += 1
                if main:
                    self.nav_redirect = (status, response.headers.get("location", "")[:300])
                route.abort("blockedbyclient")
                return
            declared = response.headers.get("content-length", "")
            body = None if declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES else response.body()
            if body is None or len(body) > MAX_RESPONSE_BYTES or self.bytes + len(body) > MAX_CHECK_BYTES:
                self.rejected += 1
                if main:
                    self.nav_failure = "response exceeds size bound"
                route.abort("blockedbyclient")
                return
            self.bytes += len(body)
            route.fulfill(status=status, headers=_relayed_headers(response.headers), body=body)
        finally:
            response.dispose()

    def on_websocket(self, ws: Any) -> None:
        # Never connect_to_server(): the socket is answered locally and closed.
        self.websockets_closed += 1
        ws.close(code=1008, reason="blocked by RackScout browser policy")

    def on_page(self, page: Any) -> None:
        if page is self.page or (self.page is None and not self._first_page_seen):
            self._first_page_seen = True
            return
        self.pages_closed += 1
        try:
            page.close()
        except Exception:  # noqa: BLE001 - page may already be gone; context close follows anyway
            log.debug("unexpected page close failed")

    def navigation_error(self) -> SourceError:
        if self.nav_redirect is not None:
            status, location = self.nav_redirect
            if CHALLENGE_REDIRECT_RE.search(location):
                return SourceBlocked(f"eBay page redirected to a challenge/sign-in (HTTP {status})")
            return SourceError(f"eBay page answered HTTP {status} redirect; redirects are not followed")
        if self.nav_failure is not None:
            return SourceError(f"eBay page refused by browser policy: {self.nav_failure}")
        return SourceError("eBay page navigation failed")


class BrowserChecker:
    """Lazily started sandboxed headless Chromium; a fresh isolated context per check."""

    def __init__(self, timeout: float, max_variants: int):
        self.timeout_ms = int(timeout * 1000)
        self.max_variants = max_variants
        self._pw = None
        self._browser = None

    @property
    def started(self) -> bool:
        return self._browser is not None

    def __enter__(self) -> "BrowserChecker":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _start(self) -> None:
        if self._browser is not None:
            return
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        try:
            # chromium_sandbox is unconditionally True: no setting, argument or
            # retry path launches Chromium with --no-sandbox.
            self._browser = self._pw.chromium.launch(headless=True, chromium_sandbox=True, args=list(CHROMIUM_ARGS))
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:  # noqa: BLE001 - best-effort shutdown of browser process
            log.warning("browser shutdown error")
        try:
            if self._pw is not None:
                self._pw.stop()
        finally:
            self._pw = self._browser = None

    def check(self, item_id: str) -> dict[str, Any]:
        """Load one item page and return verified page claims or raise SourceError/SourceBlocked."""
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        _, url = canonical_item_url(f"https://www.ebay.com/itm/{item_id}")
        self._start()
        guard = _CheckGuard(item_id, self.timeout_ms)
        context = self._browser.new_context(
            locale="en-US", java_script_enabled=True, service_workers="block", accept_downloads=False,
        )
        try:
            context.set_default_timeout(self.timeout_ms)
            # Policy is in place before any page exists, so popups' first
            # requests and every redirect hop are covered.
            context.route_web_socket(re.compile(r".*"), guard.on_websocket)
            context.route("**/*", guard.on_route)
            context.on("page", guard.on_page)
            page = context.new_page()
            guard.page = page
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            except PlaywrightTimeout as exc:
                if guard.nav_redirect is not None or guard.nav_failure is not None:
                    raise guard.navigation_error() from exc
                raise SourceError("eBay page timed out") from exc
            except PlaywrightError as exc:
                raise guard.navigation_error() from exc
            status = response.status if response else 0
            final_id, _ = canonical_item_url(page.url.split("?")[0].split("#")[0])
            if final_id != item_id:
                raise SourceError("eBay page redirected to a different item")
            title = page.title()
            body_text = page.inner_text("body", timeout=self.timeout_ms)[:200000]
            if status in (403, 429, 503) or CHALLENGE_RE.search(title) or (CHALLENGE_RE.search(body_text[:3000]) and len(body_text) < 5000):
                raise SourceBlocked(f"eBay page challenge or HTTP {status}")
            if status == 404:
                raise SourceError("eBay page not found")
            ended = ENDED_RE.search(body_text)

            def text_of(selector: str) -> str | None:
                loc = page.locator(selector).first
                try:
                    if loc.count() == 0:
                        return None
                    return " ".join(loc.inner_text(timeout=3000).split())[:300]
                except PlaywrightError:
                    return None

            price_text = text_of(".x-price-primary") or text_of("[data-testid='x-price-primary']")
            if not price_text and not ended:
                raise SourceError("eBay page lacks a price element; layout not recognized")
            shipping_text = text_of(".ux-labels-values--shipping") or text_of("[data-testid='x-shipping-cost']")
            seller = text_of(".x-sellercard-atf__info__about-seller") or text_of("[data-testid='x-sellercard-atf'] a")
            condition = text_of(".x-item-condition-text") or text_of("[data-testid='x-item-condition'] .ux-textspans")
            html = page.content()
            msku = extract_msku(html)
            has_variations = msku is not None or page.locator(".x-msku, [data-testid='x-msku-evo']").count() > 0
            variations = parse_variations(msku, self.max_variants) if msku else []
            price_kind = "range" if price_text and re.search(r"\bto\b|–|-\s*US", price_text) else "exact"
            shipping = None
            if shipping_text:
                if re.search(r"\bfree\b", shipping_text, re.IGNORECASE):
                    shipping = Decimal("0.00")
                else:
                    shipping = _money(shipping_text)
            if guard.rejected or guard.redirects_refused or guard.websockets_closed or guard.pages_closed:
                log.info(
                    "browser policy for %s: %d requests refused, %d redirects refused, %d websockets closed, "
                    "%d pages closed", item_id, guard.rejected, guard.redirects_refused, guard.websockets_closed,
                    guard.pages_closed,
                )
            return {
                "status": "verified",
                "price": str(_money(price_text)) if _money(price_text) is not None and price_kind == "exact" else None,
                "price_text": price_text,
                "price_kind": price_kind,
                "shipping": str(shipping) if shipping is not None else None,
                "shipping_text": shipping_text,
                "seller": (seller or "").split(" (")[0][:80] or None,
                "condition": condition[:80] if condition else None,
                "ended": bool(ended),
                "ended_text": ended.group(0) if ended else None,
                "has_variations": has_variations,
                "variations": variations,
            }
        finally:
            try:
                context.close()
            except PlaywrightError:
                log.warning("browser context close error")
