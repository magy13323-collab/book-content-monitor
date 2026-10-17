"""Agreed work-level placement rules; all inputs are local/synthetic."""

from pathlib import Path

import pytest

from pages import FetchResult
from part2 import Book, evaluate_page, analyze_url


FIXTURES = Path(__file__).parent / "fixtures"
CATALOG = [Book("north", "Лада Макетова", "Учебный маршрут. Том 1", "ebook")]


@pytest.fixture(autouse=True)
def no_requests(monkeypatch):
    import requests
    monkeypatch.setattr(requests.sessions.Session, "request", lambda *a, **k: pytest.fail("Live HTTP forbidden"))


def document(content, url="https://example.org/book"):
    html = ('<html><head><meta charset="utf-8"><title>Учебный маршрут. Том 1</title></head>'
            '<body><main><h1>Учебный маршрут. Том 1</h1><p class="book-author">Лада Макетова</p>'
            '<p>Карточка первого тома с информацией об авторе и доступных вариантах чтения.</p>'
            + content + '</main></body></html>')
    return FetchResult(url, url, 200, {"Content-Type": "text/html; charset=utf-8"}, html.encode())


@pytest.mark.parametrize(("content", "kind"), [
    ('<a href="/read/book">Читать онлайн</a>', "reading_link"),
    ('<a href="/download/book">Скачать книгу</a>', "download_link"),
    ('<a href="/download/epub/book">EPUB</a>', "download_link"),
    ('<a href="/index.php?do=download&amp;id=7124">Скачать .torrent</a>', "download_link"),
])
def test_scoped_reading_and_extensionless_downloads_are_signs(content, kind):
    result = evaluate_page(document(content), CATALOG, "north")
    assert result["status"] == 1
    assert any(signal["kind"] == kind and signal["binding"] == "bound" for signal in result["content_signals"])


@pytest.mark.parametrize("content", [
    '<a href="/offer">Скачать</a>',
    '<div class="advertisement"><a href="/download/book">Скачать книгу</a></div>',
    '<section><h2>Другой маршрут</h2><a href="/read/south">Читать онлайн</a></section>',
    '<a href="/read/book">Хочу прочитать</a>',
    '<a href="#">Скачать книгу</a>',
])
def test_generic_ads_other_books_wishlist_and_empty_actions_are_not_signs(content):
    result = evaluate_page(document(content), CATALOG, "north")
    assert result["status"] != 1
    assert not result["content_signals"]


def test_explicitly_different_book_identifier_is_not_inherited():
    result = evaluate_page(document('<a href="/download/book/99999/">Скачать книгу</a>',
                                    url="https://example.org/book-12345/"), CATALOG, "north")
    assert result["status"] != 1


def test_collection_requires_explicit_listed_membership_not_a_prose_mention():
    html = (FIXTURES / "collection.html").read_text(encoding="utf-8")
    html = html.replace('<ul><li>Учебный маршрут. Том 1</li><li>Другой маршрут</li></ul>',
                        '<p>Возможно, вас заинтересует Учебный маршрут. Том 1.</p>')
    result = evaluate_page(FetchResult("https://example.org/collection", "https://example.org/collection",
                                      200, {"Content-Type": "text/html; charset=utf-8"}, html.encode()), CATALOG, "north")
    assert result["status"] == 2
    assert not result["content_signals"]


def test_collection_toc_does_not_turn_an_advertised_foreign_archive_into_content():
    html = (FIXTURES / "collection.html").read_text(encoding="utf-8").replace(
        '<a href="/files/collection.zip">Скачать сборник ZIP</a>',
        '<section><p>Дополнительное предложение: программа для чтения.</p>'
        '<a href="/files/program.zip">Скачать</a></section>')
    result = evaluate_page(FetchResult("https://example.org/collection", "https://example.org/collection",
                                      200, {"Content-Type": "text/html; charset=utf-8"}, html.encode()), CATALOG, "north")
    assert result["status"] == 2
    assert not result["content_signals"]


@pytest.mark.parametrize("title", ["Загрузка — Библиотека", "Подождите", "Loading…"])
def test_loading_is_not_a_content_negative(title):
    html = f'<html><head><meta charset="utf-8"><title>{title}</title></head><body><h1>{title}</h1><p>Секундочку… Страница откроется автоматически. Включите JavaScript и подождите.</p></body></html>'
    result = evaluate_page(FetchResult("https://example.org/book", "https://example.org/book", 200,
                                      {"Content-Type": "text/html; charset=utf-8"}, html.encode()), CATALOG, "north")
    assert result["status"] == 2
    assert result["reason_code"] == "loading_shell"


def test_http_to_browser_uses_the_same_extraction_rules():
    loading = FetchResult("https://example.org/book", "https://example.org/book", 200,
                          {"Content-Type": "text/html; charset=utf-8"}, (FIXTURES / "dynamic_loading.html").read_bytes())
    target = {"url": loading.url, "expected_book_id": "north", "source_row": 2}

    class Browser:
        calls = 0

        def check(self, url):
            self.calls += 1
            return document('<a href="/read/book">Читать онлайн</a>')

    browser = Browser()
    result = analyze_url(target, CATALOG, fetcher=lambda url: loading, browser=browser)
    assert browser.calls == 1
    assert result["status"] == 1
    assert result["method"] == "Chromium"
    assert result["initial_http_status"] == 200
    assert result["http_attempt"]["reason_code"] == "loading_shell"


def test_disabled_browser_preserves_uncertainty():
    loading = FetchResult("https://example.org", "https://example.org", 403, error_code="http_403")
    result = analyze_url({"url": loading.url, "expected_book_id": "north"}, CATALOG,
                         fetcher=lambda url: loading, browser=None)
    assert result["status"] == 2
    assert result["method"] == "HTTP"
    assert not result["browser_attempt"]["attempted"]


def test_unavailable_browser_is_not_reported_as_http_success():
    class Unavailable:
        def check(self, url):
            return FetchResult(url, url, error_code="browser_unavailable", error_detail="Нет браузера")

    denied = FetchResult("https://example.org", "https://example.org", 403, error_code="http_403")
    result = analyze_url({"url": denied.url, "expected_book_id": "north"}, CATALOG,
                         fetcher=lambda url: denied, browser=Unavailable())
    assert result["status"] == 2
    assert result["reason_code"] == "browser_unavailable"
    assert result["initial_http_status"] == 403


def test_pdf_server_ignoring_range_is_not_downloaded():
    from pages import fetch_url

    class Response:
        status_code = 200
        headers = {"Content-Type": "application/pdf", "Content-Length": "1600000"}
        closed = False

        def iter_content(self, *args, **kwargs):
            pytest.fail("A full PDF body must not be consumed")

        def close(self):
            self.closed = True

    class Session:
        def get(self, url, **kwargs):
            assert kwargs["headers"]["Range"] == "bytes=0-65535"
            return response

    response = Response()
    result = fetch_url("https://example.org/book.pdf", session=Session())
    assert result.error_code == "pdf_range_not_supported"
    assert result.body == b"" and response.closed


def test_pdf_range_budget_prevents_another_network_request():
    from pages import PDFRangeStream, PDFPreviewLimit

    class Session:
        def get(self, *args, **kwargs):
            pytest.fail("Range budget must be checked before GET")

    stream = PDFRangeStream("https://example.org/book.pdf", Session(), b"x" * 65536,
                            "bytes 0-65535/1000000", limit=65536)
    stream.seek(65536)
    with pytest.raises(PDFPreviewLimit):
        stream.read(1)
    assert stream.total == 65536


def test_adjacent_feedback_button_does_not_make_a_book_action_foreign():
    result = evaluate_page(document('<div><a href="/read/book">Читать онлайн</a>'
                                    '<a href="#reviews">Оставить отзыв</a></div>'), CATALOG, "north")
    assert result["status"] == 1


def test_article_paragraph_with_download_counter_keeps_its_action():
    result = evaluate_page(document('<p>Начало истории путешествия к северному мосту.'
                                    '<a href="/index.php?do=download&amp;id=7124" title="Скачиваний: ">'
                                    'Скачать .torrent</a></p>'), CATALOG, "north")
    assert result["status"] == 1


def test_paired_book_author_headings_and_semantic_reader_are_supported():
    html = ('<html><head><meta charset="utf-8"><title>Учебный маршрут. Том 1, Лада Макетова - читать онлайн</title></head>'
            '<body><h1>Учебный маршрут. Том 1</h1><div><h2>Учебный маршрут. Том 1</h2><h2>Лада Макетова</h2>'
            '<select><option>Arial</option><option>Georgia</option></select>'
            '<div class="block_read_text">' + '<p>Учебный текст путешествия по северной дороге.</p>' * 40
            + '</div></div></body></html>')
    result = evaluate_page(FetchResult("https://example.org/read/book", "https://example.org/read/book", 200,
                                      {"Content-Type": "text/html; charset=utf-8"}, html.encode()), CATALOG, "north")
    assert result["status"] == 1
    assert result["extracted"]["authors"] == [CATALOG[0].author]


def test_collection_download_heading_is_not_confused_with_bold_link_labels():
    html = (FIXTURES / "collection.html").read_text(encoding="utf-8").replace(
        '<a href="/files/collection.zip">Скачать сборник ZIP</a>',
        '<b>Скачать Лада Макетова. Сборник книг</b><div>'
        '<a href="https://files.example/opaque"><b>Скачать Host</b></a>'
        '<a href="https://files.example/collection.zip"><b>Скачать Host2</b></a></div>')
    result = evaluate_page(FetchResult("https://example.org/collection", "https://example.org/collection", 200,
                                      {"Content-Type": "text/html; charset=utf-8"}, html.encode()), CATALOG, "north")
    assert result["status"] == 1


def test_explicit_bibliographic_context_identifies_its_download_group():
    html = ('<div class="entry-content"><p>Учебный маршрут. Том 1<br>Автор: Лада Макетова<br>Год: 2026</p>'
            '<p>История о путешествии по северной дороге.'
            '<a href="/index.php?do=download&amp;id=7124">Скачать .torrent</a></p></div>')
    assert evaluate_page(document(html), CATALOG, "north")["status"] == 1


def test_format_download_label_can_include_a_file_size():
    result = evaluate_page(document('<a href="/books/42/download/epub">Скачать EPUB ≈ 301 КБ</a>'), CATALOG, "north")
    assert result["status"] == 1


def test_failed_browser_preserves_observations_without_claiming_success():
    failed = document('<a href="/read/book">Читать онлайн</a>')
    failed.error_code = "browser_not_ready"
    result = evaluate_page(failed, CATALOG, "north")
    assert result["status"] == 2
    assert result["match"]["state"] == "unavailable"
    assert result["extracted"]["authors"] == [CATALOG[0].author]


def test_font_controls_inside_reader_are_not_another_book_heading():
    html = ('<div class="block_read_text"><h3>Шрифт:</h3><select><option>Arial</option></select>'
            + '<p>Учебный текст путешествия по северной дороге.</p>' * 40 + '</div>')
    result = evaluate_page(document(html), CATALOG, "north")
    assert result["status"] == 1
    assert result["content_signals"][0]["binding"] == "bound"


def test_semantic_metadata_card_can_identify_its_sibling_legacy_reader():
    html = ('<html><head><meta charset="utf-8"><title>Учебный маршрут. Том 1, Лада Макетова - читать онлайн</title></head>'
            '<body><h1>Учебный маршрут. Том 1</h1><div class="center_col">'
            '<div itemscope itemtype="https://schema.org/Book"><h2>Учебный маршрут. Том 1</h2><h2>Лада Макетова</h2></div>'
            '<div class="block_read_text"><h3>Шрифт:</h3><select><option>Arial</option></select>'
            + '<p>Учебный текст путешествия по северной дороге.</p>' * 40 + '</div></div></body></html>')
    result = evaluate_page(FetchResult("https://example.org/read/book", "https://example.org/read/book", 200,
                                      {"Content-Type": "text/html; charset=utf-8"}, html.encode()), CATALOG, "north")
    assert result["status"] == 1
    assert result["content_signals"][0]["scope_authors"] == [CATALOG[0].author]


def test_verified_reader_scope_is_not_replaced_by_an_outer_sibling_heading():
    html = ('<html><head><meta charset="utf-8"><title>Учебный маршрут. Том 1, Лада Макетова - читать онлайн</title></head>'
            '<body><h1>Учебный маршрут. Том 1</h1><div class="layout">'
            '<div><h2>Другой маршрут</h2></div><div class="reading-column">'
            '<div itemscope itemtype="https://schema.org/Book"><h2>Учебный маршрут. Том 1</h2><h2>Лада Макетова</h2></div>'
            '<div class="block_read_text"><h3>Шрифт:</h3>'
            + '<p>Учебный текст путешествия по северной дороге.</p>' * 40 + '</div></div></div></body></html>')
    result = evaluate_page(FetchResult("https://example.org/book", "https://example.org/book", 200,
                                      {"Content-Type": "text/html; charset=utf-8"}, html.encode()), CATALOG, "north")
    assert result["status"] == 1
    assert result["content_signals"][0]["scope_locator"] == "div.reading-column"
