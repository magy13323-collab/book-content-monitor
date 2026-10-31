"""Volume numbers compare by value without rewriting catalogue identity or source data."""

import hashlib
import json

import openpyxl
import pytest
import requests

from pages import FetchResult, extract_page
from part1 import process_workbook
from part2 import Book, analyze_url, evaluate_page, load_inputs, number_value, title_relation, volume_markers, write_results
from test_part2 import synthetic_pdf
from test_title_pipe import document
from test_yo_identity import AUTHOR, source


FORMS = ["13", "XIII", "013", "№13"]
SERIES = "Учебный маршрут. Том "
IDS = ["book_5ddbafb2315102dc", "book_7dd38c3b622b0f4c",
       "book_474c460fb029aff4", "book_e523b0416a6fedab"]


@pytest.fixture(autouse=True)
def no_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Live HTTP forbidden")
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


@pytest.mark.parametrize("expected", FORMS)
@pytest.mark.parametrize("observed", FORMS)
@pytest.mark.parametrize("structured", [False, True], ids=["html", "jsonld"])
def test_equivalent_volume_forms_confirm_the_same_content(expected, observed, structured):
    book = Book("target", AUTHOR, SERIES + expected)
    initial = document(SERIES + observed, structured=book.title if structured else None)
    result = evaluate_page(initial, [book], book.id)
    assert title_relation(book, SERIES + observed) == "exact"
    assert (result["status"], result["reason_code"]) == (1, "placement_signs")
    assert result["extracted"]["h1"] == [SERIES + observed]
    assert result["content_signals"][0]["binding"] == "bound"
    if structured:
        assert next(s for s in extract_page(initial).subjects if s["source"] == "jsonld_book")["signals"]


@pytest.mark.parametrize("expected,observed", [(a, b) for a in ["13", "130", "12"]
                                               for b in ["13", "130", "12"] if a != b])
@pytest.mark.parametrize("structured", [False, True], ids=["html", "conflicting-jsonld"])
def test_different_volume_numbers_never_confirm(expected, observed, structured):
    book = Book("target", AUTHOR, SERIES + expected)
    initial = document(SERIES + observed, structured=book.title if structured else None)
    result = evaluate_page(initial, [book], book.id)
    reason = "metadata_title_conflict" if structured else "volume_conflict"
    assert (result["status"], result["reason_code"]) == (2, reason)
    if structured:
        assert not next(s for s in extract_page(initial).subjects if s["source"] == "jsonld_book")["signals"]


@pytest.mark.parametrize("metadata", FORMS)
@pytest.mark.parametrize("opening", FORMS)
def test_pdf_opening_metadata_and_catalogue_share_number_comparison(metadata, opening):
    title = "Northern Wind. Volume "
    body = synthetic_pdf(title=title + opening.replace("№", "No"),
                         metadata={"/Title": title + metadata, "/Author": "Alice Example"})
    initial = FetchResult("https://offline.test/book.pdf", "https://offline.test/book.pdf", 200,
                          {"Content-Type": "application/pdf"}, body)
    result = evaluate_page(initial, [Book("target", "Alice Example", title + "13")], "target")
    assert (result["status"], result["reason_code"]) == (1, "placement_signs")
    assert result["content_signals"][0]["kind"] == "pdf_text"


@pytest.mark.parametrize("opening", ["130", "12"])
def test_pdf_with_contradictory_volume_metadata_stays_uncertain(opening):
    title = "Northern Wind. Volume "
    initial = FetchResult("https://offline.test/book.pdf", "https://offline.test/book.pdf", 200,
                          {"Content-Type": "application/pdf"}, synthetic_pdf(
                              title=title + opening, metadata={"/Title": title + "13", "/Author": "Alice Example"}))
    result = evaluate_page(initial, [Book("target", "Alice Example", title + "13")], "target")
    assert (result["status"], result["reason_code"]) == (2, "pdf_identity_unresolved")
    assert not result["content_signals"]


@pytest.mark.parametrize("number", [*FORMS, "130", "12"])
@pytest.mark.parametrize("kind", ["filename", "label"])
def test_epub_must_belong_to_the_right_volume(number, kind):
    foreign = number in {"130", "12"}
    href = f'/files/Северный-ветер-Том-{number}.epub' if kind == "filename" else '/files/123.epub'
    label = f'Скачать {SERIES + number} EPUB' if kind == "label" else 'Скачать EPUB'
    result = evaluate_page(document(SERIES + "13", content=f'<a href="{href}">{label}</a>'),
                           [Book("target", AUTHOR, SERIES + "13")], "target")
    expected = (2, "content_scope_unresolved") if foreign else (1, "placement_signs")
    assert (result["status"], result["reason_code"]) == expected
    if foreign:
        assert not result["content_signals"]
        observed = result["extracted"]["observed_content_signals"][0]
        assert observed["binding"] == "unresolved" and observed["binding_reason"]


@pytest.mark.parametrize("href", ["/files/130.epub", "/storage/2013/987654.pdf"])
def test_unlabelled_storage_numbers_are_not_volume_numbers(href):
    result = evaluate_page(document(SERIES + "13", content=f'<a href="{href}">Скачать EPUB</a>'),
                           [Book("target", AUTHOR, SERIES + "13")], "target")
    assert (result["status"], result["reason_code"]) == (1, "placement_signs")


@pytest.mark.parametrize("other", ["130", "12"])
def test_two_volume_cards_keep_independent_files(other):
    books = [Book("first", AUTHOR, SERIES + "13"), Book("other", AUTHOR, SERIES + other)]
    cards = ''.join(f'<article><h2>{SERIES + number}</h2><p class="book-author">{AUTHOR}</p>'
                    f'<a href="/files/{index}.epub">Скачать EPUB</a></article>'
                    for index, number in enumerate(["XIII", other], 123))
    initial = document("Библиотека", content=cards)
    for index, book in enumerate(books, 123):
        result = evaluate_page(initial, books, book.id)
        assert (result["status"], result["reason_code"]) == (1, "placement_signs")
        assert [s["url"] for s in result["content_signals"]] == [f'https://offline.test/files/{index}.epub']


@pytest.mark.parametrize("number", FORMS)
def test_literal_pipe_keeps_the_volume_comparison(number):
    title = "Учебный маршрут | Том "
    result = evaluate_page(document(title + number, structured=title + "13"),
                           [Book("target", AUTHOR, title + "13")], "target")
    assert (result["status"], result["reason_code"]) == (1, "placement_signs")


@pytest.mark.parametrize("observed", ["Учебный маршрут | Том 130", "Учебный маршрут. Том 13 | Том 12",
                                     "Учебный маршрут. Том 130 | Том 13"])
def test_conflicting_volume_after_pipe_cannot_confirm(observed):
    result = evaluate_page(document(observed), [Book("target", AUTHOR, SERIES + "13")], "target")
    assert result["status"] == 2


@pytest.mark.parametrize("expected,observed", [
    ("Клуб XIII", "Клуб 13"), ("XIII", "13"), ("MIX", "1009"),
    ("Учебный маршрут. Глава XIII", "Учебный маршрут. Глава 13"),
    ("Учебный маршрут 2013", "Учебный маршрут 20130"),
    ("Учебный маршрут ISBN 978013", "Учебный маршрут ISBN 97813"),
])
def test_arbitrary_numbers_and_roman_titles_do_not_become_volumes(expected, observed):
    result = evaluate_page(document(observed), [Book("target", AUTHOR, expected)], "target")
    assert (result["status"], result["reason_code"]) == (0, "no_match")


@pytest.mark.parametrize("title", ["Клуб XIII", "Учебный маршрут. Глава 13", "Учебный маршрут 2013"])
def test_nonvolume_numbers_keep_exact_positive_control(title):
    result = evaluate_page(document(title), [Book("target", AUTHOR, title)], "target")
    assert (result["status"], result["reason_code"]) == (1, "placement_signs")


@pytest.mark.parametrize("expected,observed,status", [
    ("Книга C++", "Книга 100++", 0), ("Книга C#", "Книга 100#", 0),
    ("Книга C#", "Книга C #", 1),
])
def test_language_symbols_are_not_part_of_a_roman_volume(expected, observed, status):
    result = evaluate_page(document(observed), [Book("target", AUTHOR, expected)], "target")
    assert result["status"] == status
    assert result["reason_code"] == ("placement_signs" if status else "no_match")


def test_existing_number_helpers_keep_their_result_types():
    assert volume_markers(SERIES + "13") == {"volume": 13}
    assert number_value("xiii") == 13


def test_catalogue_and_export_keep_all_existing_ids_and_source_rows(tmp_path):
    titles = [SERIES + number for number in FORMS]
    path = source(tmp_path, [(AUTHOR, title) for title in titles])
    original = hashlib.sha256(path.read_bytes()).digest()
    analysis = process_workbook(path)
    catalog, targets = load_inputs(path)
    assert [b.id for b in catalog] == IDS
    assert len(analysis.books) == len(catalog) == 4
    assert [r.raw[1:3] for r in analysis.records] == [(AUTHOR, title) for title in titles]
    assert [r.book_id for r in analysis.records] == IDS
    results = []
    for target in targets:
        initial = document(SERIES + "№13", structured=SERIES + "XIII")
        results.append(analyze_url(target, catalog, fetcher=lambda url, initial=initial: initial))
    write_results(results, tmp_path / "export")
    exported = [json.loads(line) for line in (tmp_path / "export/part2_results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["expected_title"] for r in exported] == titles
    assert [r["source_row"] for r in exported] == [2, 3, 4, 5]
    assert [r["expected_book_id"] for r in exported] == IDS
    assert [(r["status"], r["reason_code"]) for r in exported] == [(1, "placement_signs")] * 4
    workbook = openpyxl.load_workbook(tmp_path / "export/part2_results.xlsx", read_only=True)
    rows = list(workbook.active.values)
    headers = list(rows[0])
    assert [row[headers.index("expected_book_id")] for row in rows[1:]] == IDS
    assert [row[headers.index("status")] for row in rows[1:]] == [1] * 4
    workbook.close()
    assert hashlib.sha256(path.read_bytes()).digest() == original


def test_equivalent_catalogue_candidates_are_not_silently_overwritten_or_chosen(tmp_path):
    catalog, _ = load_inputs(source(tmp_path, [(AUTHOR, SERIES + n) for n in FORMS]))
    result = evaluate_page(document(SERIES + "13"), catalog)
    assert len(catalog) == len({b.id for b in catalog}) == 4
    assert (result["status"], result["reason_code"]) == (2, "ambiguous")
