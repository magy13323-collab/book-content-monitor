"""Offline contracts. An autouse guard forbids accidental live HTTP in tests."""

from io import BytesIO
import json
from pathlib import Path

import openpyxl
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
import pytest
import requests

from pages import FetchResult, Limits, extract_page, fetch_url
from part2 import Book, evaluate_page, load_inputs, load_json_catalog, write_results


FIXTURES = Path(__file__).parent / "fixtures"
SOURCE = Path(__file__).resolve().parents[1] / "examples" / "synthetic_input.xlsx"


@pytest.fixture(autouse=True)
def forbid_live_http(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Live HTTP is forbidden in offline tests")
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


@pytest.fixture
def catalog():
    return [Book("north", "Лада Макетова", "Учебный маршрут. Том 1", "ebook")]


def html_document(name, transform=lambda text: text):
    html = transform((FIXTURES / name).read_text(encoding="utf-8"))
    return FetchResult("https://example.org/book", "https://example.org/book", 200,
                       {"Content-Type": "text/html; charset=utf-8"}, html.encode("utf-8"))


def test_concrete_file_link_confirms_placement(catalog):
    result = evaluate_page(html_document("book_file.html"), catalog, "north")
    assert result["status"] == 1
    assert result["match"]["state"] == "matched"
    assert result["match"]["book_id"] == "north"
    assert any(signal["kind"] == "file_link" for signal in result["content_signals"])
    assert any("https://example.org/files/north.epub" in item for item in result["evidence"])


def test_layout_sidebar_and_comments_classes_do_not_erase_the_document(catalog):
    document = html_document("book_file.html", lambda html: html.replace(
        "<body>", '<body class="comments-open wp-has-sidebar kz-sidebar-mobile">'
    ))
    result = evaluate_page(document, catalog, "north")
    assert result["status"] == 1
    assert result["extracted"]["h1"] == [catalog[0].title]


def test_split_author_label_is_not_lost_or_confused_with_blog_editor(catalog):
    document = html_document("book_file.html", lambda html: html.replace(
        '<p class="book-author">Лада Макетова</p>',
        '<p><span>Автор:</span><a href="/xfsearch/avtor/anna/">Лада Макетова</a></p>'
        '<span class="posted-by author">admin</span>'
    ))
    result = evaluate_page(document, catalog, "north")
    assert result["status"] == 1
    assert result["extracted"]["authors"] == ["Лада Макетова"]


def test_unrelated_program_archive_is_not_a_book_file(catalog):
    document = html_document("book_file.html", lambda html: html.replace(
        '/files/north.epub">Скачать EPUB', '/files/program.zip">Скачать бесплатно'
    ))
    assert evaluate_page(document, catalog, "north")["status"] == 0


def test_annotation_and_advert_download_are_not_placement(catalog):
    result = evaluate_page(html_document("annotation.html"), catalog, "north")
    assert result["status"] == 0
    assert result["match"]["state"] == "matched"
    assert not result["content_signals"]


def test_foreign_page_with_target_in_recommendations_is_not_a_match(catalog):
    result = evaluate_page(html_document("recommendations.html"), catalog, "north")
    assert result["status"] == 0
    assert result["match"]["state"] == "no_match"
    assert not result["content_signals"]


def test_downloadable_collection_with_explicit_membership_is_placement(catalog):
    result = evaluate_page(html_document("collection.html"), catalog, "north")
    assert result["status"] == 1
    assert any(signal["kind"] == "collection_download" for signal in result["content_signals"])


def test_individual_book_in_collection_needs_its_own_scoped_evidence(catalog):
    html = '<main><h1>Подборка книг</h1><article class="book-card"><h2>Учебный маршрут. Том 1</h2>'
    html += '<p class="book-author">Лада Макетова</p><a href="/north.epub">Скачать EPUB</a></article>'
    html += '<article><h2>Другая книга</h2><p>Книга другого автора</p></article></main>'
    document = FetchResult("https://example.org", "https://example.org", 200,
                           {"Content-Type": "text/html"}, html.encode())
    result = evaluate_page(document, catalog, "north")
    assert result["status"] == 1


def test_same_work_in_audio_confirms_placement_despite_ebook_catalog(catalog):
    result = evaluate_page(html_document("audio.html"), catalog, "north")
    assert result["status"] == 1
    assert result["observed_formats"] == ["audio"]
    assert result["format_difference"] is True
    assert result["match"]["state"] == "matched"


def test_audio_catalog_accepts_bound_audio_source():
    catalog = [Book("north", "Лада Макетова", "Учебный маршрут. Том 1", "audio")]
    assert evaluate_page(html_document("audio.html"), catalog, "north")["status"] == 1


def test_unknown_catalog_format_is_not_silently_treated_as_ebook(catalog):
    catalog = [Book("north", catalog[0].author, catalog[0].title, "printed")]
    result = evaluate_page(html_document("book_file.html"), catalog, "north")
    assert result["status"] == 2
    assert result["reason_code"] == "catalog_format_unknown"


def test_successful_but_empty_http_response_is_not_negative(catalog):
    document = FetchResult("https://example.org", "https://example.org", 200,
                           {"Content-Type": "text/html"}, b"")
    result = evaluate_page(document, catalog, "north")
    assert result["status"] == 2
    assert result["match"]["state"] == "unavailable"
    assert result["evidence"]
    assert "Проблемы извлечения: not_loaded" in result["evidence"]


@pytest.mark.parametrize(("old", "new", "code"), [
    ("Лада Макетова", "Елена Другая", "author_conflict"),
    ("Том 1", "Том 2", "volume_conflict"),
    ("Лада Макетова", "", "author_missing"),
])
def test_identity_conflicts_never_produce_positive(catalog, old, new, code):
    result = evaluate_page(html_document("book_file.html", lambda text: text.replace(old, new)),
                           catalog, "north")
    assert result["status"] == 2
    assert result["reason_code"] == code


def test_visible_reader_is_content_but_export_does_not_contain_full_text(catalog):
    paragraph = "Учебный пример: путник вышел на северную дорогу и остановился у старого моста. "
    full_text = "".join(f"<p>{paragraph * 4}</p>" for _ in range(6))
    document = html_document("reader.html", lambda html: html.replace("{{TEXT}}", full_text))
    result = evaluate_page(document, catalog, "north")
    assert result["status"] == 1
    assert result["extracted"]["text_chars"] > 1000
    assert len(result["extracted"]["text_excerpt"]) <= 240
    assert "main_text" not in result["extracted"]
    assert paragraph * 4 not in json.dumps(result, ensure_ascii=False)


def test_in_page_book_content_anchor_is_a_scoped_reader(catalog):
    document = html_document("reader.html", lambda html: html.replace(
        'class="reader-text"', 'id="book-content-container"'
    ).replace("{{TEXT}}", "<p>Учебный пример путешествия по северной дороге. </p>" * 30))
    assert evaluate_page(document, catalog, "north")["status"] == 1


def test_hidden_reader_cannot_confirm_placement(catalog):
    document = html_document("reader.html", lambda html: html.replace(
        'class="reader-text"', 'class="reader-text" style="display:none"'
    ).replace("{{TEXT}}", "<p>Скрытый текст " + "слово " * 300 + "</p>"))
    assert evaluate_page(document, catalog, "north")["status"] != 1


def test_jsonld_book_metadata_is_extracted(catalog):
    data = {"@context": "https://schema.org", "@type": "Book",
            "name": catalog[0].title, "author": {"@type": "Person", "name": catalog[0].author}}
    document = html_document("book_file.html", lambda html: html.replace(
        "</head>", '<script type="application/ld+json">' + json.dumps(data) + "</script></head>"
    ))
    result = evaluate_page(document, catalog, "north")
    assert result["status"] == 1
    assert any(subject["source"] == "jsonld_book" for subject in result["extracted"]["subjects"])


def test_foreign_jsonld_cannot_claim_another_pages_download(catalog):
    data = {"@type": "Book", "name": catalog[0].title,
            "author": {"@type": "Person", "name": catalog[0].author}}
    document = html_document("book_file.html", lambda html: html.replace(
        "<h1>Учебный маршрут. Том 1</h1>", ""
    ).replace("Учебный маршрут. Том 1", "Другой маршрут").replace("Лада Макетова", "Олег Макетный").replace(
        "</head>", '<script type="application/ld+json">' + json.dumps(data) + "</script></head>"
    ))
    result = evaluate_page(document, catalog, "north")
    assert result["status"] == 2
    assert result["reason_code"] == "metadata_title_conflict"


@pytest.mark.parametrize("body", [
    b'<title>Just a moment...</title><div id="cf-challenge">Verify you are human</div>',
    b'<title>Sign in</title><form><input type="password"></form>',
    b'<div id="app"></div><script src="/bundle.js"></script>',
])
def test_challenge_login_and_js_shell_are_undetermined(catalog, body):
    document = FetchResult("https://example.org", "https://example.org", 200,
                           {"Content-Type": "text/html"}, body)
    result = evaluate_page(document, catalog, "north")
    assert result["status"] == 2
    assert result["match"]["state"] == "unavailable"


def test_russian_browser_challenge_is_identified(catalog):
    document = FetchResult("https://example.org", "https://example.org", 200,
                           {"Content-Type": "text/html; charset=utf-8"},
                           '<p>проверка браузера против ботов</p>'.encode())
    assert evaluate_page(document, catalog, "north")["reason_code"] == "access_challenge"


@pytest.fixture
def pdf_bytes():
    return synthetic_pdf()


def synthetic_pdf(title="The Northern Wind", author="Alice Example", metadata=None, note=""):
    """A real, parseable PDF built in memory; no PDF-generation dependency."""
    writer = PdfWriter()
    writer.add_metadata(metadata if metadata is not None else {
        "/Title": "The Northern Wind", "/Author": "Alice Example"})
    for _ in range(2):
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        text = f"BT /F1 12 Tf 40 740 Td ({title}) Tj 0 -16 Td ({author}) Tj "
        if note:
            text += f"0 -16 Td ({note}) Tj "
        text += " ".join("0 -16 Td (The traveler walked north and reached the bridge before sunset.) Tj"
                         for _ in range(30))
        stream.set_data((text + " ET").encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_pdf_with_title_author_and_body_is_confirmed(pdf_bytes):
    document = FetchResult("https://example.org/book.pdf", "https://example.org/book.pdf", 200,
                           {"Content-Type": "application/pdf"}, pdf_bytes)
    catalog = [Book("pdf-book", "Alice Example", "The Northern Wind", "ebook")]
    result = evaluate_page(document, catalog, "pdf-book")
    assert result["status"] == 1
    assert result["extracted"]["pdf_page_count"] == 2
    assert result["extracted"]["metadata"]["/Author"] == "Alice Example"
    assert any(signal["kind"] == "pdf_text" for signal in result["content_signals"])


@pytest.mark.parametrize(("title", "author"), [
    ("South Road", "Bob Different"),
    ("The Northern Wind", "Bob Different"),
    ("South Road", "Alice Example"),
])
def test_pdf_metadata_cannot_claim_another_works_text(title, author):
    document = FetchResult("https://example.org/book.pdf", "https://example.org/book.pdf", 200,
                           {"Content-Type": "application/pdf"}, synthetic_pdf(title, author))
    catalog = [Book("pdf-book", "Alice Example", "The Northern Wind", "ebook")]
    result = evaluate_page(document, catalog, "pdf-book")
    assert result["extracted"]["pdf_pages_read"] == 2
    assert result["extracted"]["text_chars"] > 1200
    assert result["extracted"]["metadata"]["/Title"] == "The Northern Wind"
    assert result["extracted"]["metadata"]["/Author"] == "Alice Example"
    assert result["status"] == 2
    assert result["reason_code"] == "pdf_identity_unresolved"
    assert not result["content_signals"]
    assert any(title in item and author in item for item in result["evidence"])


def test_pdf_prose_mention_does_not_reconcile_foreign_title_page():
    document = FetchResult("https://example.org/book.pdf", "https://example.org/book.pdf", 200,
                           {"Content-Type": "application/pdf"}, synthetic_pdf(
                               "South Road", "Bob Different",
                               note="The hero once read The Northern Wind by Alice Example in a library."))
    result = evaluate_page(document, [Book("north", "Alice Example", "The Northern Wind")], "north")
    assert result["status"] == 2
    assert result["reason_code"] == "pdf_identity_unresolved"
    assert not result["content_signals"]


@pytest.mark.parametrize(("title", "author", "metadata"), [
    ("The Northern Wind", "Example Alice", None),
    ("The Northern Wind - Alice Example", "", None),
    ("The Northern Wind", "Alice Example", {}),
    ("The Northern Wind", "Alice Example", {"/Title": "The Northern Wind"}),
    ("The Northern Wind", "Alice Example", {"/Author": "Alice Example"}),
])
def test_pdf_consistent_opening_still_confirms_with_varied_metadata(title, author, metadata):
    document = FetchResult("https://example.org/book.pdf", "https://example.org/book.pdf", 200,
                           {"Content-Type": "application/pdf"}, synthetic_pdf(title, author, metadata))
    result = evaluate_page(document, [Book("north", "Alice Example", "The Northern Wind")], "north")
    assert result["status"] == 1


@pytest.mark.parametrize(("container", "description", "url"), [
    ('section class="bonus-offer"', "Bonus item: South Road by Bob Different.", "/files/south-road.epub"),
    ('section', "Included work: Glass Harbor by Carol Other.", "/opaque.epub"),
    ('div class="offer"', "Available book: Desert Sky by David Another.", "/download/123.epub"),
    ('section', '<h2>Download</h2>Included work: Glass Harbor by Carol Other.', "/opaque.epub"),
    ('p', "Included work: Glass Harbor by Carol Other.", "/opaque.epub"),
])
def test_headingless_offer_does_not_inherit_main_book(container, description, url):
    tag = container.split()[0]
    html = ('<main><h1>The Northern Wind</h1><p class="book-author">Alice Example</p>'
            f'<{container}><p>{description}</p><a href="{url}">Download EPUB</a></{tag}></main>')
    document = FetchResult("https://example.org/book", "https://example.org/book", 200,
                           {"Content-Type": "text/html"}, html.encode())
    catalog = [Book("north", "Alice Example", "The Northern Wind", "ebook")]
    result = evaluate_page(document, catalog, "north")
    assert result["status"] == 2
    assert result["reason_code"] == "content_scope_unresolved"
    assert not result["content_signals"]
    assert any(signal["binding"] == "unresolved"
               for signal in result["extracted"]["observed_content_signals"])


@pytest.mark.parametrize("content", [
    '<section><a href="/opaque.epub">Download EPUB</a></section>',
    '<div><p>Download EPUB</p><a href="/opaque.epub">Download EPUB</a></div>',
    '<section><h2>Download</h2><a href="/opaque.epub">Download EPUB</a></section>',
])
def test_action_only_file_container_preserves_target_binding(content):
    html = ('<main><h1>The Northern Wind</h1><p class="book-author">Alice Example</p>' + content + '</main>')
    document = FetchResult("https://example.org/book", "https://example.org/book", 200,
                           {"Content-Type": "text/html"}, html.encode())
    result = evaluate_page(document, [Book("north", "Alice Example", "The Northern Wind")], "north")
    assert result["status"] == 1


def test_single_page_pdf_description_does_not_prove_book_content(pdf_bytes):
    from pypdf import PdfReader
    writer = PdfWriter()
    writer.add_metadata({"/Title": "The Northern Wind", "/Author": "Alice Example"})
    page = PdfReader(BytesIO(pdf_bytes)).pages[0]
    stream = DecodedStreamObject()
    text = "BT /F1 12 Tf 40 740 Td (Synopsis: The Northern Wind - Alice Example) Tj "
    text += " ".join("0 -16 Td (A description of the book, its audience and publishing information.) Tj"
                     for _ in range(30))
    stream.set_data((text + " ET").encode())
    page[NameObject("/Contents")] = stream
    writer.add_page(page)
    output = BytesIO()
    writer.write(output)
    document = FetchResult("https://example.org/book.pdf", "https://example.org/book.pdf", 200,
                           {"Content-Type": "application/pdf"}, output.getvalue())
    catalog = [Book("pdf-book", "Alice Example", "The Northern Wind", "ebook")]
    result = evaluate_page(document, catalog, "pdf-book")
    assert result["extracted"]["text_chars"] > 1200
    assert result["status"] != 1


def test_pdf_mismatching_metadata_is_not_accepted(pdf_bytes):
    catalog = [Book("other", "Bob Other", "The Northern Wind", "ebook")]
    document = FetchResult("https://example.org/book.pdf", "https://example.org/book.pdf", 200,
                           {"Content-Type": "application/pdf"}, pdf_bytes)
    assert evaluate_page(document, catalog, "other")["status"] == 2


def test_identified_foreign_pdf_is_negative_even_with_page_sampling(pdf_bytes):
    from pages import Limits
    from part2 import classify, match_book
    document = FetchResult("https://example.org/book.pdf", "https://example.org/book.pdf", 200,
                           {"Content-Type": "application/pdf"}, pdf_bytes)
    page = extract_page(document, limits=Limits(pdf_max_pages=1))
    catalog = [Book("foreign", "Other Author", "Another Book", "ebook")]
    assert page.partial
    assert classify(document, page, match_book(page, catalog, "foreign"), catalog)[0] == 0


def test_pdf_frontmatter_can_precede_the_identifying_title_page(pdf_bytes):
    from pypdf import PdfReader
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    reader = PdfReader(BytesIO(pdf_bytes))
    writer.add_metadata({"/Title": "The Northern Wind", "/Author": "Alice Example"})
    for page in reader.pages:
        writer.add_page(page)
    output = BytesIO()
    writer.write(output)
    document = FetchResult("https://example.org/book.pdf", "https://example.org/book.pdf", 200,
                           {"Content-Type": "application/pdf"}, output.getvalue())
    page = extract_page(document)
    assert "The Northern Wind" in page.opening
    assert evaluate_page(document, [Book("north", "Alice Example", "The Northern Wind")], "north")["status"] == 1


def test_title_decorations_do_not_replace_missing_author(catalog):
    document = html_document("book_file.html", lambda html: html.replace(
        '<h1>Учебный маршрут. Том 1</h1>', '<h1>Читать книгу Учебный маршрут. Том 1 полностью</h1>'
    ))
    assert evaluate_page(document, catalog, "north")["status"] == 1


@pytest.mark.parametrize("body", [b'%PDF-1.4\nnot-a-pdf', b'not a pdf at all'])
def test_broken_pdf_is_undetermined(catalog, body):
    document = FetchResult("https://example.org/file.pdf", "https://example.org/file.pdf", 200,
                           {"Content-Type": "application/pdf"}, body)
    assert evaluate_page(document, catalog, "north")["status"] == 2


def test_scanned_pdf_without_text_is_undetermined(catalog):
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    output = BytesIO()
    writer.write(output)
    document = FetchResult("https://example.org/scan.pdf", "https://example.org/scan.pdf", 200,
                           {"Content-Type": "application/pdf"}, output.getvalue())
    assert evaluate_page(document, catalog, "north")["status"] == 2


class FakeResponse:
    def __init__(self, status=200, body=b"page", headers=None):
        self.status_code = status
        self.headers = headers or {"Content-Type": "text/html"}
        self.body = body
        self.closed = False

    def iter_content(self, chunk_size):
        yield self.body

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.parametrize("error", [requests.Timeout("timeout"), requests.ConnectionError("DNS failure"),
                                  requests.exceptions.SSLError("certificate failure")])
def test_network_errors_return_undetermined(catalog, error):
    fetched = fetch_url("https://example.org", session=FakeSession([error]))
    result = evaluate_page(fetched, catalog, "north")
    assert result["status"] == 2
    assert fetched.error_code


@pytest.mark.parametrize(("http_status", "expected"), [(404, 0), (410, 0), (403, 2), (429, 2), (503, 2)])
def test_http_statuses_are_not_confused(catalog, http_status, expected):
    fetched = fetch_url("https://example.org", session=FakeSession([FakeResponse(status=http_status)]))
    result = evaluate_page(fetched, catalog, "north")
    assert result["status"] == expected
    assert result["http_status"] == http_status


def test_fetch_timeout_streaming_and_relative_redirect():
    redirect = FakeResponse(302, headers={"Location": "/book"})
    final = FakeResponse()
    session = FakeSession([redirect, final])
    fetched = fetch_url("https://example.org/start", session=session)
    assert fetched.final_url == "https://example.org/book"
    assert len(fetched.redirects) == 1
    assert redirect.closed and final.closed
    assert session.calls[0][1]["timeout"] == (5, 12)
    assert session.calls[0][1]["allow_redirects"] is False
    assert session.calls[0][1]["stream"] is True


def test_redirect_limit_is_enforced():
    session = FakeSession([FakeResponse(302, headers={"Location": "/again"}) for _ in range(3)])
    assert fetch_url("https://example.org", session=session, limits=Limits(max_redirects=1)).error_code == "redirect_limit"


@pytest.mark.parametrize("headers", [{"Content-Length": "1000"}, {}])
def test_response_size_is_enforced_from_header_and_stream(headers):
    session = FakeSession([FakeResponse(body=b"x" * 100, headers=headers)])
    fetched = fetch_url("https://example.org", session=session, limits=Limits(max_bytes=50))
    assert fetched.error_code == "response_too_large"
    assert fetched.body == b""


def test_expected_book_is_not_replaced_by_another_catalog_entry(catalog):
    other = Book("south", "Олег Макетный", "Другой маршрут", "ebook")
    result = evaluate_page(html_document("recommendations.html"), catalog + [other], "north")
    assert result["status"] == 0
    assert result["match"]["book_id"] != "south"
    assert "south" in result["match"]["other_catalog_matches"]


def test_default_catalog_comes_only_from_second_sheet():
    catalog, targets = load_inputs(SOURCE)
    assert len(catalog) == 1 and len(targets) == 7
    assert {target["source_row"] for target in targets} == set(range(2, 9))
    assert all(target["source_sheet"] == "Catalog" for target in targets)
    assert all(target["expected_book_id"] in {book.id for book in catalog} for target in targets)


def test_external_catalog_and_export_are_reusable(tmp_path, catalog):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps([{"id": "north", "author": catalog[0].author,
                                 "title": catalog[0].title, "content_type": "ebook"}]), encoding="utf-8")
    external = load_json_catalog(path)
    result = evaluate_page(html_document("book_file.html"), external)
    assert result["status"] == 1
    result.update(source_row=2, source_sheet="Задание 2", source_file="input.xlsx")
    write_results([result], tmp_path)
    saved = json.loads((tmp_path / "part2_results.jsonl").read_text(encoding="utf-8"))
    assert saved == result
    workbook = openpyxl.load_workbook(tmp_path / "part2_results.xlsx")
    assert workbook["Results"].max_row == 2
    assert workbook["Results"].auto_filter.ref
    assert workbook["Results"].freeze_panes == "A2"
    workbook.close()


def scope_document(content, author="Лада Макетова"):
    html = ('<!doctype html><html><head><meta charset="utf-8"><title>Учебный маршрут. Том 1</title>'
            '</head><body><main><h1>Учебный маршрут. Том 1</h1>'
            f'<p class="book-author">{author}</p>'
            '<p>Карточка проверяемого произведения с описанием первого тома.</p>'
            + content + '</main></body></html>')
    return FetchResult("https://example.org/book", "https://example.org/book", 200,
                       {"Content-Type": "text/html; charset=utf-8"}, html.encode())


def sample_chapter():
    return '<div class="chapter-text">' + '<p>Учебный текст о путешествии и старом мосте.</p>' * 40 + '</div>'


def test_reported_download_of_another_book_is_not_target_content(catalog):
    document = scope_document('<a href="/files/another-book.epub">Скачать другую книгу EPUB</a>')
    result = evaluate_page(document, catalog, "north")
    assert result["status"] == 2
    assert result["reason_code"] == "content_scope_unresolved"
    assert not result["content_signals"]


def test_reported_chapter_in_another_work_section_is_not_target_content(catalog):
    document = scope_document('<section><h2>Другой маршрут</h2>' + sample_chapter() + '</section>')
    result = evaluate_page(document, catalog, "north")
    assert result["status"] == 0
    assert not result["content_signals"]


@pytest.mark.parametrize("label", ["Загрузить роман «Другой маршрут» EPUB", "Download Blue Harbour EPUB"])
def test_non_generic_resource_labels_require_a_book_identity(catalog, label):
    result = evaluate_page(scope_document(f'<a href="/opaque.epub">{label}</a>'), catalog, "north")
    assert result["status"] == 2
    assert not result["content_signals"]


@pytest.mark.parametrize("container", ['article', 'div class="book-card"'])
def test_nested_foreign_card_does_not_inherit_primary_book(catalog, container):
    end_tag = container.split()[0]
    document = scope_document(f'<{container}><h2>Другой маршрут</h2>'
                              '<a href="/south.epub">Скачать EPUB</a>' + sample_chapter()
                              + f'</{end_tag}>')
    result = evaluate_page(document, catalog, "north")
    assert result["status"] == 0
    assert not result["content_signals"]


def test_sibling_heading_changes_the_content_scope(catalog):
    document = scope_document('<h2>Другой маршрут</h2>' + sample_chapter())
    assert evaluate_page(document, catalog, "north")["status"] == 0


@pytest.mark.parametrize("kind", ["ebook", "audio"])
def test_foreign_audio_is_neither_target_content_nor_a_format_conflict(catalog, kind):
    catalog = [Book("north", catalog[0].author, catalog[0].title, kind)]
    document = scope_document('<section><h2>Музыкальная программа</h2>'
                              '<audio src="/music.mp3"></audio></section>')
    result = evaluate_page(document, catalog, "north")
    assert result["status"] == 0
    assert result["reason_code"] != "format_conflict"
    assert not result["content_signals"]
    assert "audio" not in result["extracted"]["detected_formats"]


def test_audio_without_owning_identity_remains_unresolved(catalog):
    result = evaluate_page(scope_document('<article><audio src="/opaque.mp3"></audio></article>'),
                           catalog, "north")
    assert result["status"] == 2
    assert result["reason_code"] == "content_scope_unresolved"
    assert not result["content_signals"]


@pytest.mark.parametrize("label", ["", ' aria-label="Слушать"'])
def test_unlabelled_standalone_player_does_not_establish_book_format(catalog, label):
    result = evaluate_page(scope_document(f'<audio src="/opaque.mp3"{label}></audio>'), catalog, "north")
    assert result["status"] == 2
    assert result["reason_code"] == "content_scope_unresolved"
    assert "audio" not in result["extracted"]["detected_formats"]


def test_target_file_survives_next_to_foreign_audio_and_chapter(catalog):
    document = scope_document('<a href="/north.epub">Скачать EPUB</a>'
                              '<section><h2>Другой маршрут</h2><audio src="/south.mp3"></audio>'
                              + sample_chapter() + '</section>')
    result = evaluate_page(document, catalog, "north")
    assert result["status"] == 1
    assert [item["kind"] for item in result["content_signals"]] == ["file_link"]
    assert result["extracted"]["detected_formats"] == ["ebook"]


def test_named_target_file_and_scope_proof_are_preserved(catalog):
    document = scope_document('<a href="/opaque.epub">Скачать Учебный маршрут. Том 1 EPUB</a>')
    result = evaluate_page(document, catalog, "north")
    assert result["status"] == 1
    signal = result["content_signals"][0]
    assert signal["binding"] == "bound"
    assert signal["scope_title"]
    assert signal["scope_locator"]


def test_collection_card_cannot_claim_a_nested_foreign_reader(catalog):
    html = ('<main><h1>Подборка книг</h1><article class="book-card">'
            '<h2>Учебный маршрут. Том 1</h2><p class="book-author">Лада Макетова</p>'
            '<section><h3>Другой маршрут</h3>' + sample_chapter() + '</section></article></main>')
    document = FetchResult("https://example.org", "https://example.org", 200,
                           {"Content-Type": "text/html; charset=utf-8"}, html.encode())
    result = evaluate_page(document, catalog, "north")
    assert result["status"] == 2
    assert not result["content_signals"]


def test_foreign_card_author_cannot_supply_missing_primary_author(catalog):
    document = scope_document('<a href="/north.epub">Скачать EPUB</a>'
                              '<section><h2>Другой маршрут</h2>'
                              '<p class="book-author">Лада Макетова</p></section>', author="")
    result = evaluate_page(document, catalog, "north")
    assert result["status"] == 2
    assert result["reason_code"] == "author_missing"


@pytest.mark.parametrize("content", [sample_chapter(), '<audio src="/other.mp3"></audio>'])
def test_same_title_with_another_author_is_a_different_scope(catalog, content):
    document = scope_document('<section><h2>Учебный маршрут. Том 1</h2>'
                              '<p class="book-author">Олег Макетный</p>' + content + '</section>')
    result = evaluate_page(document, catalog, "north")
    assert result["status"] == 0
    assert result["reason_code"] != "format_conflict"
    assert result["extracted"]["authors"] == [catalog[0].author]
    assert not result["content_signals"]


def test_matching_metadata_panel_can_identify_a_sibling_reader(catalog):
    html = ('<main><h1>Читать книгу Учебный маршрут. Том 1 полностью</h1>'
            '<div class="metadata"><h2>Учебный маршрут. Том 1</h2>'
            '<p class="book-author">Лада Макетова</p></div>'
            '<div id="book-content-container">' + sample_chapter() + '</div></main>')
    document = FetchResult("https://example.org/book", "https://example.org/book", 200,
                           {"Content-Type": "text/html; charset=utf-8"}, html.encode())
    result = evaluate_page(document, catalog, "north")
    assert result["status"] == 1
    assert result["extracted"]["authors"] == [catalog[0].author]
    assert all(signal["binding"] == "bound" for signal in result["content_signals"])


@pytest.mark.parametrize("content", [sample_chapter(), '<audio src="/other.mp3"></audio>'])
def test_headingless_description_cannot_claim_readers_or_players(catalog, content):
    document = scope_document('<section><p>Другой маршрут — Олег Макетный.</p>' + content + '</section>')
    result = evaluate_page(document, catalog, "north")
    assert result["status"] == 2
    assert result["reason_code"] == "content_scope_unresolved"
    assert not result["content_signals"]
    assert "audio" not in result["extracted"]["detected_formats"]


def test_target_file_survives_next_to_headingless_foreign_offer(catalog):
    document = scope_document('<a href="/north.epub">Скачать EPUB</a>'
                              '<section><p>Другой маршрут — Олег Макетный.</p>'
                              '<a href="/south.epub">Скачать EPUB</a></section>')
    result = evaluate_page(document, catalog, "north")
    assert result["status"] == 1
    assert [signal["url"] for signal in result["content_signals"]] == ["https://example.org/north.epub"]
