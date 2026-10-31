"""Files and JSON-LD must keep their own book identity; no network access."""

import json
from urllib.parse import quote

import pytest
import requests

from pages import FetchResult, extract_page
from part2 import Book, evaluate_page


SUN = Book("sun", "Анна Лесная", "Солнце")
MOON = Book("moon", SUN.author, "Солнце и Луна")
URL = "https://offline.test/books/12345/"


@pytest.fixture(autouse=True)
def no_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Live HTTP forbidden")
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


def document(content, book=SUN, structured=None, heading=None, attributes=""):
    schema = '' if structured is None else (
        '<script type="application/ld+json">' + json.dumps(structured) + '</script>')
    identity = (f'<h1>{heading or book.title}</h1><p class="book-author">{book.author}</p>'
                if book else '<h1>Библиотека</h1>')
    html = (f'<html><head><meta charset="utf-8">{schema}</head><body><main {attributes}>'
            f'{identity}<p>Карточки произведений и доступные форматы чтения.</p>{content}</main></body></html>')
    return FetchResult(URL, URL, 200, {"Content-Type": "text/html; charset=utf-8"}, html.encode())


def schema(book):
    return {"@type": "Book", "name": book.title, "author": {"name": book.author}}


@pytest.mark.parametrize("href", [
    "/files/Солнце.epub", "/files/" + quote("Солнце") + ".pdf",
    "/books/12345/file.epub", "/files/99999.pdf", "/storage/book-99999.epub",
    "https://cdn.example/books/99999/file.epub", "/files/sun.pdf?object_id=99999&signature=abc",
    "/files/Анна_Лесная_Солнце.epub", "/files/9af0e125-1da2-4dff-aeff-123456789012.pdf",
])
def test_own_file_url_formats_do_not_create_conflicts(href):
    result = evaluate_page(document(f'<a href="{href}">Скачать EPUB</a>'), [SUN], SUN.id)
    assert (result["status"], result["reason_code"]) == (1, "placement_signs")
    assert result["content_signals"][0]["binding"] == "bound"


def test_download_attribute_can_name_an_opaque_own_resource():
    result = evaluate_page(document('<a href="/objects/8765" download="Солнце.epub">Скачать EPUB</a>'),
                           [SUN], SUN.id)
    assert result["status"] == 1


@pytest.mark.parametrize("content", [
    '<a href="/files/Солнце-и-Луна.epub">Скачать EPUB</a>',
    '<a href="/files/' + quote('Солнце и Луна') + '.pdf">Скачать PDF</a>',
    '<a href="/files/8765.epub" download="Солнце и Луна.epub">Скачать EPUB</a>',
    '<a href="/files/mystery-story.epub">Скачать EPUB</a>',
    '<a href="/books/99999/file.epub">Скачать EPUB</a>',
    '<a href="/download/book/99999/file.pdf">Скачать PDF</a>',
    '<a href="/files/8765.epub?book_id=99999">Скачать EPUB</a>',
], ids=["foreign-filename", "encoded-name", "download-name", "ambiguous-name",
        "book-route", "download-book-route", "explicit-book-query"])
def test_contradictory_or_ambiguous_file_stays_observed_but_unbound(content):
    result = evaluate_page(document(content), [SUN], SUN.id)
    assert (result["status"], result["reason_code"]) == (2, "content_scope_unresolved")
    assert not result["content_signals"]
    observed = result["extracted"]["observed_content_signals"]
    assert len(observed) == 1
    assert observed[0]["kind"] == "file_link"
    assert observed[0]["binding"] == "unresolved"
    reason = observed[0]["binding_reason"]
    assert reason and any(reason in item for item in result["evidence"])


def test_explicit_book_data_id_conflict_is_not_a_storage_id():
    initial = document('<a href="/files/sun.epub" data-book-id="moon">Скачать EPUB</a>',
                       attributes='data-book-id="sun"')
    result = evaluate_page(initial, [SUN], SUN.id)
    assert (result["status"], result["reason_code"]) == (2, "content_scope_unresolved")
    assert result["extracted"]["observed_content_signals"][0]["binding"] == "unresolved"


def test_foreign_caption_remains_protected():
    result = evaluate_page(document('<a href="/files/sun.epub">Скачать Солнце и Луна EPUB</a>'), [SUN], SUN.id)
    assert (result["status"], result["reason_code"]) == (2, "content_scope_unresolved")


@pytest.mark.parametrize(("filename", "status"), [("north-wind.epub", 1), ("the-south-road.epub", 2)])
def test_abbreviated_own_name_and_unrelated_name_are_distinct(filename, status):
    book = Book("north", "Alice Example", "The Northern Wind")
    result = evaluate_page(document(f'<a href="/files/{filename}">Download EPUB</a>', book=book), [book], book.id)
    assert result["status"] == status


def test_foreign_file_does_not_cancel_an_independent_own_file():
    initial = document('<a href="/files/sun.epub">Скачать EPUB</a>'
                       '<a href="/files/Солнце-и-Луна.epub">Скачать EPUB</a>')
    result = evaluate_page(initial, [SUN], SUN.id)
    assert result["status"] == 1
    assert [signal["url"] for signal in result["content_signals"]] == ['https://offline.test/files/sun.epub']
    assert any(signal["binding"] == "unresolved" for signal in result["extracted"]["observed_content_signals"])


def test_conflicting_book_id_on_the_file_container_is_preserved():
    initial = document('<div data-book-id="moon"><a href="/files/sun.epub">Скачать EPUB</a></div>',
                       attributes='data-book-id="sun"')
    result = evaluate_page(initial, [SUN], SUN.id)
    assert (result["status"], result["reason_code"]) == (2, "content_scope_unresolved")


@pytest.mark.parametrize("own_file", [False, True])
@pytest.mark.parametrize("extension", ["epub", "pdf"])
def test_independent_cards_use_only_their_own_files(own_file, extension):
    first_file = f'<a href="/files/sun.{extension}">Скачать EPUB</a>' if own_file else ''
    cards = (f'<article><h2>{SUN.title}</h2><p class="book-author">{SUN.author}</p>{first_file}</article>'
             f'<article><h2>{MOON.title}</h2><p class="book-author">{MOON.author}</p>'
             f'<a href="/files/moon.{extension}">Скачать EPUB</a></article>')
    initial = document(cards, book=None)
    sun = evaluate_page(initial, [SUN, MOON], SUN.id)
    moon = evaluate_page(initial, [SUN, MOON], MOON.id)
    assert sun["status"] == (1 if own_file else 0)
    assert [signal["url"] for signal in sun["content_signals"]] == (
        [f'https://offline.test/files/sun.{extension}'] if own_file else [])
    assert moon["status"] == 1
    assert [signal["url"] for signal in moon["content_signals"]] == [f'https://offline.test/files/moon.{extension}']


@pytest.mark.parametrize(("dom", "structured"), [(SUN, MOON), (MOON, SUN),
    (SUN, Book("sunburn", SUN.author, "Солнцепёк"))], ids=["longer", "shorter", "partial-word"])
def test_jsonld_cannot_borrow_a_substring_titles_file(dom, structured):
    initial = document('<a href="/files/sun.epub">Скачать EPUB</a>', book=dom, structured=schema(structured))
    result = evaluate_page(initial, [structured], structured.id)
    assert (result["status"], result["reason_code"]) == (2, "metadata_title_conflict")
    assert not result["content_signals"]
    subjects = extract_page(initial).subjects
    assert not next(subject for subject in subjects if subject["source"] == "jsonld_book")["signals"]
    assert result["extracted"]["observed_content_signals"][0]["scope_title"] == dom.title


@pytest.mark.parametrize(("heading", "expected"), [
    (SUN.title, (1, "placement_signs")), ("СОЛНЦЕ!", (1, "placement_signs")),
    ("Читать Солнце онлайн", (1, "placement_signs")), ("Солнце — Анна Лесная", (1, "placement_signs")),
    ("Солнце | Библиотека", (2, "metadata_title_conflict")),
])
def test_jsonld_title_decoration_requires_site_context(heading, expected):
    initial = document('<a href="/files/sun.epub">Скачать EPUB</a>', heading=heading, structured=schema(SUN))
    result = evaluate_page(initial, [SUN], SUN.id)
    assert (result["status"], result["reason_code"]) == expected


def test_jsonld_can_use_each_independent_matching_card():
    cards = ''.join(f'<article><h2>{book.title}</h2><p class="book-author">{book.author}</p>'
                    f'<a href="/files/{book.id}.epub">Скачать EPUB</a></article>' for book in (SUN, MOON))
    initial = document(cards, book=None, structured=[schema(SUN), schema(MOON)])
    for book in (SUN, MOON):
        result = evaluate_page(initial, [SUN, MOON], book.id)
        assert result["status"] == 1
        assert [signal["url"] for signal in result["content_signals"]] == [f'https://offline.test/files/{book.id}.epub']
