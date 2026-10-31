"""Literal author names in titles must survive identity and resource matching."""

import hashlib
import json

import openpyxl
import pytest
import requests

from pages import FetchResult, extract_page
from part1 import process_workbook
from part2 import Book, analyze_url, evaluate_page, load_inputs, match_book, title_relation, write_results
from test_title_pipe import document
from test_yo_identity import source


AUTHOR = "Анна Лесная"
TITLES = ["Анна Лесная. Письма", AUTHOR, "Анна Лесная и её письма",
          "Письма, которые написала Анна Лесная, сохранились", "Письма. Анна Лесная",
          "Анна Лесная | Письма", "Анна Лесная. C++ и C#",
          "Анна Лесная — Письма", "Письма — Анна Лесная"]


@pytest.fixture(autouse=True)
def no_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Live HTTP forbidden")
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


@pytest.mark.parametrize("structured", [False, True], ids=["html", "jsonld"])
@pytest.mark.parametrize("title", TITLES)
def test_full_title_takes_priority_over_author_removal(title, structured):
    book = Book("target", AUTHOR, title)
    initial = document(title, title=title, structured=title if structured else None)
    result = evaluate_page(initial, [book], book.id)
    assert title_relation(book, title) == "exact"
    assert (result["status"], result["reason_code"]) == (1, "placement_signs")
    assert result["extracted"]["h1"] == [title]
    assert match_book(extract_page(initial), [book], book.id)["subject"]["title"] == title
    assert result["content_signals"][0]["binding"] == "bound"


@pytest.mark.parametrize("heading", ["  «АННА ЛЕСНАЯ. ПИСЬМА»  ", "Анна\u00a0Лесная.   Письма"])
def test_literal_title_keeps_equivalent_spacing_and_quotes(heading):
    result = evaluate_page(document(heading), [Book("target", AUTHOR, TITLES[0])], "target")
    assert (result["status"], result["reason_code"]) == (1, "placement_signs")


@pytest.mark.parametrize("heading", [TITLES[0], "Письма. Анна Лесная", "Письма Анна Лесная"])
def test_shorter_book_cannot_match_by_deleting_author(heading):
    result = evaluate_page(document(heading), [Book("letters", AUTHOR, "Письма")], "letters")
    assert (result["status"], result["reason_code"]) == (2, "ambiguous")
    assert result["match"]["state"] == "ambiguous"
    assert result["extracted"]["observed_content_signals"][0]["kind"] == "file_link"


@pytest.mark.parametrize("h1,schema", [(TITLES[0], "Письма"), ("Письма", TITLES[0]),
                                      (AUTHOR, "Письма"), ("Письма", AUTHOR)])
@pytest.mark.parametrize("target", ["html", "jsonld"])
def test_conflicting_jsonld_cannot_inherit_another_title_file(h1, schema, target):
    initial = document(h1, structured=schema)
    title = h1 if target == "html" else schema
    result = evaluate_page(initial, [Book("target", AUTHOR, title)], "target")
    assert (result["status"], result["reason_code"]) == (2, "metadata_title_conflict")
    structured = next(s for s in extract_page(initial).subjects if s["source"] == "jsonld_book")
    assert not structured["signals"]
    assert result["extracted"]["observed_content_signals"]


@pytest.mark.parametrize("seo", ["Письма — Анна Лесная", "Анна Лесная — Письма"])
def test_confirmed_h1_identifies_the_book_despite_seo_author_signature(seo):
    result = evaluate_page(document("Письма", title=seo), [Book("letters", AUTHOR, "Письма")], "letters")
    assert (result["status"], result["reason_code"]) == (1, "placement_signs")
    assert result["extracted"]["title"] == seo


@pytest.mark.parametrize("heading", ["Письма — Анна Лесная", "Анна Лесная — Письма"])
@pytest.mark.parametrize("structured", [False, True], ids=["unconfirmed", "corroborated"])
def test_author_signature_requires_independent_title_corroboration(heading, structured):
    result = evaluate_page(document(heading, structured="Письма" if structured else None),
                           [Book("letters", AUTHOR, "Письма")], "letters")
    expected = (1, "placement_signs") if structured else (2, "ambiguous")
    assert (result["status"], result["reason_code"]) == expected


@pytest.mark.parametrize("heading", ["Письма — Анна Лесная", "Анна Лесная — Письма"])
@pytest.mark.parametrize("target", ["only-full", "full", "short"])
def test_known_literal_title_overrides_signature_inference(heading, target):
    full, short = Book("full", AUTHOR, heading), Book("short", AUTHOR, "Письма")
    catalog = [full] if target == "only-full" else [full, short]
    result = evaluate_page(document(heading, structured=short.title), catalog,
                           full.id if target == "only-full" else target)
    assert (result["status"], result["reason_code"]) == (2, "ambiguous")
    assert not result["content_signals"]
    assert any(heading in item and "JSON-LD" in item for item in result["evidence"])
    assert result["extracted"]["observed_content_signals"]


def test_foreign_similar_title_does_not_match_the_full_title():
    result = evaluate_page(document("Мария Другая. Письма"), [Book("target", AUTHOR, TITLES[0])], "target")
    assert (result["status"], result["reason_code"]) == (0, "no_match")


@pytest.mark.parametrize("title", [AUTHOR, TITLES[0]])
def test_separate_cards_keep_their_own_epub_when_title_contains_author(title):
    books = [Book("full", AUTHOR, title), Book("short", AUTHOR, "Письма")]
    cards = ''.join(f'<article><h2>{b.title}</h2><p class="book-author">{AUTHOR}</p>'
                    f'<a href="/files/{index}.epub">Скачать EPUB</a></article>'
                    for index, b in enumerate(books, 123))
    html = f'<html><head><meta charset="utf-8"></head><body><main><h1>Библиотека</h1>{cards}</main></body></html>'
    initial = FetchResult("https://offline.test/book", "https://offline.test/book", 200,
                          {"Content-Type": "text/html; charset=utf-8"}, html.encode())
    for index, book in enumerate(books, 123):
        result = evaluate_page(initial, books, book.id)
        assert (result["status"], result["reason_code"]) == (1, "placement_signs")
        assert [s["url"] for s in result["content_signals"]] == [f"https://offline.test/files/{index}.epub"]


def test_foreign_epub_label_stays_unresolved_for_full_title():
    initial = document(TITLES[0], content='<a href="/files/123.epub">Скачать Письма EPUB</a>')
    result = evaluate_page(initial, [Book("full", AUTHOR, TITLES[0])], "full")
    assert (result["status"], result["reason_code"]) == (2, "content_scope_unresolved")
    assert not result["content_signals"]
    assert result["extracted"]["observed_content_signals"][0]["binding"] == "unresolved"


def test_export_preserves_literal_titles_rows_and_existing_ids(tmp_path):
    titles = [TITLES[0], "Письма", AUTHOR]
    path = source(tmp_path, [(AUTHOR, title) for title in titles])
    original = hashlib.sha256(path.read_bytes()).digest()
    analysis = process_workbook(path)
    catalog, targets = load_inputs(path)
    assert len(analysis.books) == len(catalog) == 3
    assert len({b.id for b in catalog}) == 3
    assert [r.raw[1:3] for r in analysis.records] == [(AUTHOR, title) for title in titles]
    results = []
    for target in targets:
        initial = document(target["expected_title"])
        results.append(analyze_url(target, catalog, fetcher=lambda url, initial=initial: initial))
    write_results(results, tmp_path / "export")
    exported = [json.loads(line) for line in (tmp_path / "export/part2_results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["source_row"] for r in exported] == [2, 3, 4]
    assert [r["expected_title"] for r in exported] == titles
    assert [r["expected_book_id"] for r in exported] == [r.book_id for r in analysis.records]
    assert [(r["status"], r["reason_code"]) for r in exported] == [(1, "placement_signs")] * 3
    workbook = openpyxl.load_workbook(tmp_path / "export/part2_results.xlsx", read_only=True)
    assert workbook.active.max_row == 4
    workbook.close()
    assert hashlib.sha256(path.read_bytes()).digest() == original
