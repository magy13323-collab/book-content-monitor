"""C, C++ and C# have distinct identities; all fixtures are local and synthetic."""

import json
from itertools import permutations

import openpyxl
import pytest
import requests

from pages import FetchResult, extract_page, text_key
from part1 import HEADERS, process_workbook
from part2 import Book, evaluate_page, load_inputs, load_json_catalog, title_relation
from test_part2 import synthetic_pdf


TITLES = ("C++ для начинающих", "C# для начинающих", "C для начинающих")
AUTHOR = "Анна Лесная"
BOOKS = [Book(str(index), AUTHOR, title) for index, title in enumerate(TITLES)]


@pytest.fixture(autouse=True)
def no_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Live HTTP forbidden")
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


def document(title, structured=None, label="Скачать EPUB"):
    schema = '' if structured is None else (
        '<script type="application/ld+json">' + json.dumps({
            "@type": "Book", "name": structured, "author": {"name": AUTHOR}}) + '</script>')
    html = (f'<html><head><meta charset="utf-8">{schema}</head><body><main>'
            f'<h1>{title}</h1><p class="book-author">{AUTHOR}</p>'
            '<p>Карточка книги и доступные форматы для чтения произведения.</p>'
            f'<a href="/files/123.epub">{label}</a></main></body></html>')
    return FetchResult("https://offline.test/book", "https://offline.test/book", 200,
                       {"Content-Type": "text/html; charset=utf-8"}, html.encode())


def test_excel_catalogue_keeps_three_distinct_books(tmp_path):
    source = tmp_path / "catalogue.xlsx"
    workbook = openpyxl.Workbook()
    for sheet in [workbook.active, workbook.create_sheet("Targets")]:
        sheet.append(HEADERS)
        for index, title in enumerate(TITLES):
            sheet.append(("Publisher", AUTHOR, title, "ebook", f"https://offline.test/{index}", "offline.test"))
    workbook.save(source)
    workbook.close()
    analysis = process_workbook(source)
    catalog, targets = load_inputs(source)
    assert [record.title for record in analysis.records] == [
        "c++ для начинающих", "c# для начинающих", "c для начинающих"]
    assert len(analysis.books) == len(catalog) == 3
    assert len({book.id for book in catalog}) == 3
    assert [record.book_id for record in analysis.records] == [target["expected_book_id"] for target in targets]
    assert [book.title for book in catalog] == list(TITLES)


def test_json_catalogue_keeps_three_distinct_books(tmp_path):
    source = tmp_path / "catalogue.json"
    source.write_text(json.dumps([{"author": AUTHOR, "title": title} for title in TITLES]), encoding="utf-8")
    catalog = load_json_catalog(source)
    assert len(catalog) == len({book.id for book in catalog}) == 3
    assert [book.title for book in catalog] == list(TITLES)


@pytest.mark.parametrize(("title", "expected"), [
    (TITLES[0], "c++ для начинающих"), (TITLES[1], "c# для начинающих"), (TITLES[2], "c для начинающих"),
])
def test_search_key_retains_significant_symbols(title, expected):
    assert text_key(title) == expected


@pytest.mark.parametrize("structured", [False, True], ids=["html", "jsonld"])
@pytest.mark.parametrize("observed", TITLES)
@pytest.mark.parametrize("book", BOOKS, ids=["cpp", "csharp", "c"])
def test_title_identity_matrix_includes_final_status(book, observed, structured):
    result = evaluate_page(document(observed, observed if structured else None), [book], book.id)
    expected = (1, "placement_signs") if book.title == observed else (0, "no_match")
    assert (result["status"], result["reason_code"]) == expected
    assert title_relation(book, observed) == ("exact" if book.title == observed else "none")


@pytest.mark.parametrize(("dom", "metadata"), list(permutations(TITLES, 2)))
def test_jsonld_cannot_inherit_a_different_languages_file(dom, metadata):
    initial = document(dom, metadata)
    book = Book("target", AUTHOR, metadata)
    result = evaluate_page(initial, [book], book.id)
    assert (result["status"], result["reason_code"]) == (2, "metadata_title_conflict")
    assert not result["content_signals"]
    subject = next(item for item in extract_page(initial).subjects if item["source"] == "jsonld_book")
    assert not subject["signals"]


@pytest.mark.parametrize("structured", [False, True], ids=["html", "jsonld"])
@pytest.mark.parametrize("book", BOOKS, ids=["cpp", "csharp", "c"])
def test_full_catalogue_selects_only_the_actual_book(book, structured):
    result = evaluate_page(document(book.title, book.title if structured else None), BOOKS)
    assert (result["status"], result["reason_code"]) == (1, "placement_signs")
    assert result["match"]["book_id"] == book.id
    assert not result["match"]["other_catalog_matches"]


@pytest.mark.parametrize("structured", [False, True], ids=["html", "jsonld"])
@pytest.mark.parametrize(("title", "heading"), [
    (TITLES[0], '  «c++»\u00a0ДЛЯ   начинающих!  '),
    (TITLES[0], 'C + + для начинающих'),
    (TITLES[0], 'Ｃ＋＋ для начинающих'),
    (TITLES[1], 'c#\u00a0 для\tНАЧИНАЮЩИХ'),
    (TITLES[1], 'C # — для начинающих'),
    (TITLES[2], 'Читать «C для начинающих» онлайн'),
])
def test_equivalent_case_spacing_and_decoration_keep_positive(title, heading, structured):
    book = Book("target", AUTHOR, title)
    result = evaluate_page(document(heading, title if structured else None), [book], book.id)
    assert (result["status"], result["reason_code"]) == (1, "placement_signs")


@pytest.mark.parametrize(("title", "foreign"), list(permutations(TITLES, 2)))
def test_descriptive_file_label_cannot_erase_the_language_difference(title, foreign):
    book = Book("target", AUTHOR, title)
    result = evaluate_page(document(title, label=f"Скачать {foreign} EPUB"), [book], book.id)
    assert (result["status"], result["reason_code"]) == (2, "content_scope_unresolved")
    assert not result["content_signals"]
    assert result["extracted"]["observed_content_signals"][0]["binding"] == "unresolved"


@pytest.mark.parametrize("observed", ["C++ for Beginners", "C# for Beginners", "C for Beginners"])
@pytest.mark.parametrize("expected", ["C++ for Beginners", "C# for Beginners", "C for Beginners"])
def test_shared_key_also_keeps_pdf_identity_distinct(expected, observed):
    initial = FetchResult("https://offline.test/book.pdf", "https://offline.test/book.pdf", 200,
                          {"Content-Type": "application/pdf"}, synthetic_pdf(
                              title=observed, metadata={"/Title": observed, "/Author": "Alice Example"}))
    book = Book("target", "Alice Example", expected)
    result = evaluate_page(initial, [book], book.id)
    assert (result["status"], result["reason_code"]) == (
        (1, "placement_signs") if expected == observed else (0, "different_pdf_book"))
