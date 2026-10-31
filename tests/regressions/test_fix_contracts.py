"""Focused integration checks for MIME fallback and definite browser negatives."""

import pytest

from browser import BrowserProbe
from pages import FetchResult
from part2 import Book, analyze_url


@pytest.mark.parametrize("html_pdf_url", [False, True])
def test_real_browser_preserves_mime_and_target_readiness(html_pdf_url):
    pytest.importorskip("playwright.sync_api")
    url = "https://offline.fix/book" + (".pdf" if html_pdf_url else "")
    book = Book("north", "Alice Example", "The Northern Wind")
    other = '<article><h2>South Road</h2><p class="book-author">Bob Different</p><p>Public bibliographic details and reading formats.</p><a href="/south.epub">Download EPUB</a></article>'
    own = ('<article><h1>The Northern Wind</h1><p class="book-author">Alice Example</p>'
           '<p>Public bibliographic details and reading formats.</p><div id="actions"></div></article>')
    script = "<script>setTimeout(() => { document.querySelector('#actions').innerHTML = '<a href=\"/north.epub\">Download EPUB</a>'; }, 350);</script>"
    body = '<html><head><meta charset="utf-8"></head><body><main>' + (own if html_pdf_url else '') + other + '</main>' + (script if html_pdf_url else '') + '</body></html>'
    initial = (FetchResult(url, url, 200, {"content-type": "text/html"}, body.encode()) if html_pdf_url
               else FetchResult(url, url, 403, error_code="http_403"))
    with BrowserProbe(timeout_ms=3500, fixture_routes={url: (200, body)}) as probe:
        if not probe.start():
            pytest.skip(probe.failure)
        result = analyze_url({"url": url, "expected_book_id": book.id}, [book],
                             fetcher=lambda _: initial, browser=probe)
    assert result["status"] == (1 if html_pdf_url else 0)
    assert result["browser_attempt"]["ready"]
    assert result["method"] == "Chromium"
    assert all(signal["url"] == "https://offline.fix/north.epub" for signal in result["content_signals"])
