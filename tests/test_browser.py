"""Real Chromium/JS, deterministic fulfilled routes only. No internet."""

from pathlib import Path

import pytest

from part2 import Book, analyze_url
from pages import FetchResult


FIXTURES = Path(__file__).parent / "fixtures"
CATALOG = [Book("north", "Анна Лесная", "Учебный маршрут. Том 1", "ebook")]


@pytest.fixture
def browser_factory():
    pytest.importorskip("playwright.sync_api", reason="Optional browser dependencies not installed")
    from browser import BrowserProbe
    probes = []

    def make(resolved="book_file.html", never_ready=False):
        routes = {
            "https://offline.test/start": (200, (FIXTURES / "dynamic_loading.html").read_text(encoding="utf-8")),
            "https://offline.test/resolved": (200, (FIXTURES / resolved).read_text(encoding="utf-8")),
        }
        if never_ready:
            routes["https://offline.test/resolved"] = routes["https://offline.test/start"]
        probe = BrowserProbe(timeout_ms=2500 if never_ready else 7000, fixture_routes=routes)
        if not probe.start():
            probe.close()
            pytest.skip("Local Chromium/Edge unavailable; no browser is installed by tests")
        probes.append(probe)
        return probe

    yield make
    for probe in probes:
        probe.close()


def test_real_offline_js_replaces_loading_with_book(browser_factory):
    probe = browser_factory()
    initial = FetchResult("https://offline.test/start", "https://offline.test/start", 200,
                          {"Content-Type": "text/html; charset=utf-8"}, (FIXTURES / "dynamic_loading.html").read_bytes())
    result = analyze_url({"url": initial.url, "expected_book_id": "north"}, CATALOG,
                         fetcher=lambda url: initial, browser=probe)
    assert result["status"] == 1
    assert result["method"] == "Chromium"
    assert result["browser_attempt"]["ready"] is True


def test_real_offline_js_can_reveal_a_different_book(browser_factory):
    probe = browser_factory(resolved="recommendations.html")
    result = probe.check("https://offline.test/start")
    from part2 import evaluate_page
    assert evaluate_page(result, CATALOG, "north")["status"] == 0


def test_real_offline_js_keeps_unresolved_loading_as_two(browser_factory):
    probe = browser_factory(never_ready=True)
    from part2 import evaluate_page
    assert evaluate_page(probe.check("https://offline.test/start"), CATALOG, "north")["status"] == 2


def test_browser_does_not_solve_a_captcha(browser_factory):
    probe = browser_factory()
    probe.fixture_routes["https://offline.test/captcha"] = (
        200, '<title>CAPTCHA</title><form><input name="captcha"><p>Введите код проверки</p></form>')
    result = probe.check("https://offline.test/captcha")
    assert result.error_code == "browser_captcha"


def test_browser_server_error_is_not_a_content_negative(browser_factory):
    probe = browser_factory()
    probe.timeout_ms = 1500
    probe.fixture_routes["https://offline.test/error"] = (
        503, '<title>503 Service Temporarily Unavailable</title><h1>503 Service Temporarily Unavailable</h1><p>nginx: retry later</p>')
    from part2 import evaluate_page
    assert evaluate_page(probe.check("https://offline.test/error"), CATALOG, "north")["status"] == 2


def test_browser_waits_for_reader_after_metadata_appears(browser_factory):
    probe = browser_factory(resolved="dynamic_reader_shell.html")
    from part2 import evaluate_page
    result = evaluate_page(probe.check("https://offline.test/start"), CATALOG, "north")
    assert result["status"] == 1
    assert any(signal["kind"] == "reader_text" for signal in result["content_signals"])


def test_http_metadata_shell_without_reading_title_waits_for_dynamic_reader(browser_factory):
    probe = browser_factory(resolved="dynamic_reader_shell.html")
    status, html = probe.fixture_routes["https://offline.test/resolved"]
    html = html.replace(" — читать онлайн", "")
    probe.fixture_routes["https://offline.test/resolved"] = (status, html)
    initial = FetchResult("https://offline.test/resolved", "https://offline.test/resolved", 200,
                          {"Content-Type": "text/html; charset=utf-8"}, html.encode())
    result = analyze_url({"url": initial.url, "expected_book_id": "north"}, CATALOG,
                         fetcher=lambda url: initial, browser=probe)
    assert result["http_attempt"]["reason_code"] == "content_requires_js"
    assert result["method"] == "Chromium"
    assert result["status"] == 1
    assert any(signal["kind"] == "reader_text" for signal in result["content_signals"])
