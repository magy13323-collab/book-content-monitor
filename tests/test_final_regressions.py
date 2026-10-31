"""Minimal reproductions of the final classifier review, using synthetic pages only."""

import json

import pytest

from pages import FetchResult
from part2 import Book, analyze_url, evaluate_page
from test_new_rules import CATALOG, document, no_requests


@pytest.mark.parametrize("offer", [
    'Рекомендуем другую книгу Южный берег <a href="https://ads.example/south.epub">Скачать EPUB</a>',
    'Выгодное предложение <a href="/south.epub">Скачать EPUB</a>',
    'Рекомендуем <span>другую книгу <a href="/south.epub">Скачать EPUB</a></span>',
    '<span>Реклама чужой книги <a href="/south.epub">Скачать EPUB</a></span>',
    'Реклама чужой книги <a href="/download/epub/offer">Скачать EPUB</a>',
])
def test_prose_advert_cannot_inherit_book_identity(offer):
    result = evaluate_page(document(f'<p>{offer}</p>'), CATALOG, "north")
    assert result["status"] == 2
    assert result["reason_code"] == "content_scope_unresolved"
    assert not result["content_signals"]


@pytest.mark.parametrize("url", ["/index.php?do=download&amp;id=7124", "https://files.example/index.php?do=download&amp;id=7124"])
@pytest.mark.parametrize("bibliography", [True, False])
def test_native_download_in_description_retains_explicit_binding(url, bibliography):
    metadata = (f'<p>{CATALOG[0].title}<br>Автор: {CATALOG[0].author}</p>' if bibliography else '')
    counter = '' if bibliography else ' title="Скачиваний: "'
    initial = document(f'<div>{metadata}<p>Начало истории путешествия по северной дороге.'
                       f'<a href="{url}"{counter}>Скачать .torrent</a></p></div>')
    result = evaluate_page(initial, CATALOG, "north")
    assert result["status"] == 1
    assert result["content_signals"][0]["binding"] == "bound"


@pytest.mark.parametrize(("content", "calls", "status"), [
    ('<noscript>Для загрузки содержимого включите JavaScript.</noscript><script src="/reader.js"></script>', 1, 1),
    ('<div id="reader"></div><script src="/reader.js"></script>', 1, 1),
    ('<p>Содержимое загружается с помощью JavaScript.</p><script src="/reader.js"></script>', 1, 1),
    ('<script src="/analytics.js"></script>', 0, 0),
    ('<noscript>Включите JavaScript.</noscript><a href="/north.epub">Скачать EPUB</a>', 0, 1),
])
def test_metadata_only_dynamic_content_uses_browser_when_needed(content, calls, status):
    initial = document(content)

    class Browser:
        calls = 0

        def check(self, url):
            self.calls += 1
            return document('<a href="/north.epub">Скачать EPUB</a>')

    browser = Browser()
    result = analyze_url({"url": initial.url, "expected_book_id": "north"}, CATALOG,
                         fetcher=lambda url: initial, browser=browser)
    assert browser.calls == calls
    assert result["status"] == status
    if calls:
        assert result["http_attempt"]["reason_code"] == "content_requires_js"
        assert result["method"] == "Chromium"
        assert analyze_url({"url": initial.url, "expected_book_id": "north"}, CATALOG,
                           fetcher=lambda url: initial)["status"] == 2


def test_standalone_book_named_story_collection_keeps_direct_epub():
    book = Book("stories", "Анна Лесная", "Сборник рассказов")
    initial = document('<a href="/stories.epub">Скачать EPUB</a>')
    initial.body = initial.body.decode().replace(CATALOG[0].title, book.title).encode()
    result = evaluate_page(initial, [book], book.id)
    assert result["status"] == 1
    assert result["content_signals"][0]["kind"] == "file_link"


@pytest.mark.parametrize("heading", ["Новинки недели", "Выбор редакции"])
@pytest.mark.parametrize("own_file", [True, False])
def test_individual_cards_on_editorial_lists_have_local_evidence(heading, own_file):
    link = '<a href="/north.epub">Скачать EPUB</a>' if own_file else ''
    html = (f'<main><h1>{heading}</h1><article class="book-card"><h2>{CATALOG[0].title}</h2>'
            f'<p class="book-author">{CATALOG[0].author}</p>{link}</article>'
            '<article><h2>Южный берег</h2><p class="book-author">Иван Другой</p>'
            '<a href="/south.epub">Скачать EPUB</a></article></main>')
    initial = FetchResult("https://example.org/list", "https://example.org/list", 200,
                          {"Content-Type": "text/html; charset=utf-8"}, html.encode())
    result = evaluate_page(initial, CATALOG, "north")
    assert result["status"] == (1 if own_file else 0)
    assert [signal["url"] for signal in result["content_signals"]] == (
        ["https://example.org/north.epub"] if own_file else [])


@pytest.mark.parametrize(("title", "author"), [
    ("Южный берег", "Иван Другой"),
    (CATALOG[0].title, "Иван Другой"),
])
def test_recommended_jsonld_book_cannot_cancel_verified_primary_card(title, author):
    metadata = {"@type": "Book", "name": CATALOG[0].title,
                "author": {"name": CATALOG[0].author},
                "isRelatedTo": {"@type": "Book", "name": title, "author": {"name": author}}}
    initial = document('<a href="/north.epub">Скачать EPUB</a>')
    initial.body = initial.body.replace(b'</head>', (
        '<script type="application/ld+json">' + json.dumps(metadata) + '</script></head>').encode())
    result = evaluate_page(initial, CATALOG, "north")
    assert result["status"] == 1
    assert result["match"]["state"] == "matched"
    assert result["content_signals"][0]["url"] == "https://example.org/north.epub"


@pytest.mark.parametrize("failure_stage", ["new_context", "new_page", "route", "wait", "close"])
def test_browser_context_failure_is_structured_and_next_url_is_processed(failure_stage):
    from browser import BrowserProbe

    class Page:
        url = "https://example.org/next"

        def on(self, *args):
            pass

        def goto(self, *args, **kwargs):
            pass

        def content(self):
            if failing and failure_stage == "wait":
                raise RuntimeError("Context closed")
            return document('<a href="/north.epub">Скачать EPUB</a>').body.decode()

        def evaluate(self, script):
            return self.content()

        def locator(self, selector):
            return self

        def count(self):
            return 0

        def wait_for_timeout(self, timeout):
            raise RuntimeError("Context closed")

    class Context:
        def new_page(self):
            if failing and failure_stage == "new_page":
                raise RuntimeError("Context closed")
            return Page()

        def route(self, *args):
            if failing and failure_stage == "route":
                raise RuntimeError("Context closed")

        def close(self):
            if failing and failure_stage == "close":
                raise RuntimeError("Context closed")

    class Browser:
        def new_context(self, **kwargs):
            if failing and failure_stage == "new_context":
                raise RuntimeError("Browser disconnected")
            return Context()

    probe = BrowserProbe()
    probe.browser = Browser()
    failing = True
    results = []
    for url in ["https://example.org/failed", "https://example.org/next"]:
        results.append(analyze_url({"url": url, "expected_book_id": "north"}, CATALOG,
                                   fetcher=lambda url: FetchResult(url, url, 403, error_code="http_403"),
                                   browser=probe))
        failing = False
    assert [result["status"] for result in results] == [2, 1]
    assert results[0]["url"] == "https://example.org/failed"
    assert results[0]["fetch_error"] == "browser_error"
    assert "RuntimeError" in results[0]["fetch_error_detail"]
    assert not results[0]["browser_attempt"]["ready"]


def test_browser_failure_after_a_404_observation_still_has_status_two():
    failed = FetchResult("https://example.org/failed", "https://example.org/failed", 404,
                         error_code="browser_error", error_detail="Context closed")
    result = evaluate_page(failed, CATALOG, "north")
    assert result["status"] == 2
    assert result["reason_code"] == "browser_error"
