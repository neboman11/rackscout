"""Browser network/sandbox policy regressions; Playwright objects are faked, no network."""
import inspect

import pytest

ITEM = "126905936007"
ITEM_URL = f"https://www.ebay.com/itm/{ITEM}"


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b"<html></html>"):
        self.status = status
        self.headers = headers or {"content-type": "text/html"}
        self._body = body
        self.body_reads = 0
        self.disposed = False

    def body(self):
        self.body_reads += 1
        return self._body

    def dispose(self):
        self.disposed = True


class FakeFrame:
    pass


class FakePage:
    def __init__(self):
        self.main_frame = FakeFrame()
        self.closed = False

    def close(self):
        self.closed = True


class FakeRequest:
    def __init__(self, url, *, frame=None, navigation=False, resource_type="script", method="GET"):
        self.url = url
        self.frame = frame
        self.method = method
        self.resource_type = resource_type
        self._navigation = navigation

    def is_navigation_request(self):
        return self._navigation


class FakeRoute:
    def __init__(self, request, response=None):
        self.request = request
        self.response = response or FakeResponse()
        self.fetch_kwargs = None
        self.aborted = None
        self.fulfilled = None

    def fetch(self, **kwargs):
        self.fetch_kwargs = kwargs
        return self.response

    def abort(self, error_code=None):
        self.aborted = error_code or "failed"

    def fulfill(self, **kwargs):
        self.fulfilled = kwargs

    def continue_(self, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("route.continue_ lets the browser follow unchecked redirects")

    def fallback(self, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("route.fallback hands the request to the network unchecked")


def guard_with_page():
    from rackscout.browser import _CheckGuard
    guard = _CheckGuard(ITEM, 5000)
    page = FakePage()
    guard.on_page(page)
    guard.page = page
    return guard, page


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_item_page_redirect_is_aborted_not_followed(status):
    guard, page = guard_with_page()
    route = FakeRoute(
        FakeRequest(ITEM_URL, frame=page.main_frame, navigation=True, resource_type="document"),
        FakeResponse(status, {"location": "https://127.0.0.1/internal"}),
    )
    guard.on_route(route)
    assert route.fetch_kwargs["max_redirects"] == 0
    assert route.aborted and route.fulfilled is None
    assert route.response.disposed
    assert guard.nav_redirect == (status, "https://127.0.0.1/internal")
    from rackscout.sources import SourceBlocked, SourceError
    err = guard.navigation_error()
    assert isinstance(err, SourceError) and not isinstance(err, SourceBlocked)


def test_redirect_to_challenge_is_reported_blocked():
    from rackscout.sources import SourceBlocked
    guard, page = guard_with_page()
    route = FakeRoute(
        FakeRequest(ITEM_URL, frame=page.main_frame, navigation=True, resource_type="document"),
        FakeResponse(307, {"location": "https://www.ebay.com/splashui/challenge?ap=1"}),
    )
    guard.on_route(route)
    assert route.aborted
    assert isinstance(guard.navigation_error(), SourceBlocked)


def test_allowlisted_subresource_redirect_is_aborted():
    guard, _ = guard_with_page()
    route = FakeRoute(
        FakeRequest("https://ir.ebaystatic.com/assets/item.js"),
        FakeResponse(302, {"location": "http://169.254.169.254/latest/meta-data/"}),
    )
    guard.on_route(route)
    assert route.fetch_kwargs["max_redirects"] == 0
    assert route.aborted and route.fulfilled is None
    assert guard.redirects_refused == 1


def test_allowed_item_page_fulfilled_with_restrictive_csp():
    guard, page = guard_with_page()
    route = FakeRoute(
        FakeRequest(ITEM_URL, frame=page.main_frame, navigation=True, resource_type="document"),
        FakeResponse(200, {"content-type": "text/html", "content-encoding": "gzip", "content-length": "5",
                           "link": "<https://evil.invalid>; rel=preconnect"}, b"<html>ok</html>"),
    )
    guard.on_route(route)
    assert route.aborted is None
    assert route.fulfilled["status"] == 200 and route.fulfilled["body"] == b"<html>ok</html>"
    headers = route.fulfilled["headers"]
    assert "content-encoding" not in headers and "link" not in headers and "content-length" not in headers
    csp = headers["content-security-policy"]
    connect = next(part for part in csp.split(";") if part.strip().startswith("connect-src"))
    assert "wss:" not in connect and "ws:" not in connect and "'self'" not in connect and "*" not in connect
    assert "worker-src 'none'" in csp and "frame-src 'none'" in csp


@pytest.mark.parametrize("request_kwargs", [
    {"url": "http://www.ebay.com/x.js"},
    {"url": "https://www.ebay.com:8443/x.js"},
    {"url": "https://user:pw@ir.ebaystatic.com/x.js"},
    {"url": "https://ir.ebaystatic.com.evil.invalid/x.js"},
    {"url": "https://evil.ebay.com/x.js"},
    {"url": "https://10.0.0.1/x.js"},
    {"url": "https://www.ebay.com/collect", "method": "POST", "resource_type": "xhr"},
    {"url": "https://ir.ebaystatic.com/a.png", "resource_type": "image"},
    {"url": "https://www.ebay.com/frame", "resource_type": "document", "navigation": True},
])
def test_disallowed_requests_aborted_before_any_fetch(request_kwargs):
    guard, _ = guard_with_page()
    route = FakeRoute(FakeRequest(**request_kwargs))
    guard.on_route(route)
    assert route.aborted and route.fetch_kwargs is None and route.fulfilled is None


def test_popup_first_request_and_page_rejected():
    guard, page = guard_with_page()
    popup = FakePage()
    # Popup's first request: even the same item URL is refused outside our own top frame.
    route = FakeRoute(FakeRequest(ITEM_URL, frame=popup.main_frame, navigation=True, resource_type="document"))
    guard.on_route(route)
    assert route.aborted and route.fetch_kwargs is None
    guard.on_page(popup)
    assert popup.closed and not page.closed
    assert guard.pages_closed == 1


def test_top_navigation_to_other_item_rejected():
    guard, page = guard_with_page()
    route = FakeRoute(FakeRequest("https://www.ebay.com/itm/187634883092", frame=page.main_frame,
                                  navigation=True, resource_type="document"))
    guard.on_route(route)
    assert route.aborted and route.fetch_kwargs is None
    assert guard.nav_failure


def test_websocket_closed_without_server_connection():
    guard, _ = guard_with_page()

    class FakeWebSocketRoute:
        url = "wss://www.ebay.com/socket"
        closed_with = None

        def connect_to_server(self):  # pragma: no cover - must never be reached
            raise AssertionError("websocket must not reach the server")

        def close(self, code=None, reason=None):
            self.closed_with = code

    ws = FakeWebSocketRoute()
    guard.on_websocket(ws)
    assert ws.closed_with is not None
    assert guard.websockets_closed == 1


def test_request_budget_bounded():
    from rackscout.browser import MAX_CHECK_REQUESTS
    guard, _ = guard_with_page()
    guard.requests = MAX_CHECK_REQUESTS
    route = FakeRoute(FakeRequest("https://ir.ebaystatic.com/assets/item.js"))
    guard.on_route(route)
    assert route.aborted and route.fetch_kwargs is None


def test_oversized_response_not_fulfilled():
    from rackscout.browser import MAX_RESPONSE_BYTES
    guard, _ = guard_with_page()
    route = FakeRoute(
        FakeRequest("https://ir.ebaystatic.com/assets/item.js"),
        FakeResponse(200, {"content-length": str(MAX_RESPONSE_BYTES + 1)}),
    )
    guard.on_route(route)
    assert route.aborted and route.fulfilled is None and route.response.body_reads == 0


def test_unsandboxed_launch_is_impossible(monkeypatch):
    import playwright.sync_api as sync_api
    from playwright.sync_api import Error as PlaywrightError

    from rackscout.browser import BrowserChecker

    launches = []
    stopped = []

    class FakeChromium:
        def launch(self, **kwargs):
            launches.append(kwargs)
            raise PlaywrightError("No usable sandbox!")

    class FakePlaywright:
        chromium = FakeChromium()

        def stop(self):
            stopped.append(True)

    class FakeManager:
        def start(self):
            return FakePlaywright()

    monkeypatch.setenv("RACKSCOUT_BROWSER_SANDBOX", "false")
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: FakeManager())
    assert "sandbox" not in inspect.signature(BrowserChecker).parameters
    checker = BrowserChecker(10.0, 5)
    with pytest.raises(PlaywrightError):
        checker.check(ITEM)
    assert len(launches) == 1, "sandbox failure must not be retried another way"
    assert launches[0]["chromium_sandbox"] is True
    assert not any("no-sandbox" in arg for arg in launches[0].get("args") or [])
    assert "ignore_default_args" not in launches[0]
    assert not checker.started and stopped == [True]


def test_sandbox_setting_removed(monkeypatch):
    from rackscout.config import Settings, load_settings
    monkeypatch.setenv("RACKSCOUT_BROWSER_SANDBOX", "false")
    assert "browser_sandbox" not in Settings.__dataclass_fields__
    assert not hasattr(load_settings(), "browser_sandbox")
