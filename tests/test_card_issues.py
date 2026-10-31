"""Independent card errors stay local; unowned and overlapping conflicts remain cautious."""

import json

import openpyxl
import pytest
import requests

from pages import FetchResult, extract_page
from part2 import Book, analyze_url, classify, evaluate_page, match_book, write_results


SUN = Book("sun", "Анна Лесная", "Солнце")
MOON = Book("moon", "Борис Северный", "Луна")
URL = "https://offline.test/library"


@pytest.fixture(autouse=True)
def no_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Live HTTP forbidden")
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


def schema(book, error=None):
    return {"@type": "Book", "name": "Чужое заглавие" if error == "title" else book.title,
            "author": {"name": "Вера Другая" if error == "author" else book.author}}


def script(data):
    return '<script type="application/ld+json">' + json.dumps(data) + '</script>'


def card(book, error=None, own_file=True, extension="epub", structured=True, extra="", identifier=None):
    metadata = script(schema(book, error)) if structured else ''
    file = f'<a href="/files/{111 if book.id == SUN.id else 222}.{extension}">Скачать {extension.upper()}</a>' if own_file else ''
    return (f'<article id="{identifier or book.id}"><h1>{book.title}</h1>'
            f'<p class="book-author">{book.author}</p><p>Произведение и доступные форматы чтения.</p>'
            f'{metadata}{file}{extra}</article>')


def document(cards, heading=None, metadata=None, title="Библиотека"):
    h1 = '' if heading is None else f'<h1>{heading}</h1>'
    structured = '' if metadata is None else script(metadata)
    html = (f'<html><head><meta charset="utf-8"><title>{title}</title>{structured}</head>'
            f'<body><main>{h1}{"".join(cards)}</main></body></html>')
    return FetchResult(URL, URL, 200, {"Content-Type": "text/html; charset=utf-8"}, html.encode())


@pytest.mark.parametrize("heading", [None, "Библиотека"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("error", ["title", "author"])
def test_neighbour_conflict_cannot_cancel_an_independent_book(heading, reverse, error):
    cards = [card(SUN), card(MOON, error)]
    initial = document(cards[::-1] if reverse else cards, heading)
    result = evaluate_page(initial, [SUN, MOON], SUN.id)
    alone = evaluate_page(document([card(SUN)], heading), [SUN], SUN.id)
    assert (result["status"], result["reason_code"]) == (alone["status"], alone["reason_code"]) == (1, "placement_signs")
    assert [s["url"] for s in result["content_signals"]] == ["https://offline.test/files/111.epub"]
    assert "metadata_" + error + "_conflict" in result["extracted"]["issues"]
    assert any(MOON.title in item and "независим" in item for item in result["evidence"])


@pytest.mark.parametrize("heading", [None, "Библиотека"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("error", ["title", "author"])
def test_conflict_inside_the_target_card_still_blocks(heading, reverse, error):
    cards = [card(SUN, error), card(MOON)]
    result = evaluate_page(document(cards[::-1] if reverse else cards, heading), [SUN, MOON], SUN.id)
    assert (result["status"], result["reason_code"]) == (2, "metadata_" + error + "_conflict")
    assert any("JSON-LD" in item for item in result["evidence"])


@pytest.mark.parametrize("extension", ["epub", "pdf"])
@pytest.mark.parametrize("own_file", [False, True])
def test_foreign_card_files_cannot_supply_target_evidence(extension, own_file):
    initial = document([card(SUN, own_file=own_file, extension=extension), card(MOON, "title", extension=extension)])
    result = evaluate_page(initial, [SUN, MOON], SUN.id)
    assert (result["status"], result["reason_code"]) == ((1, "placement_signs") if own_file else (0, "content_not_confirmed"))
    expected = [f'https://offline.test/files/111.{extension}'] if own_file else []
    assert [s["url"] for s in result["content_signals"]] == expected


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("heading", [None, "Библиотека"])
@pytest.mark.parametrize("container", ["array", "graph"])
def test_several_head_books_bind_only_to_their_unique_cards(reverse, heading, container):
    cards = [card(SUN, structured=False), card(MOON, structured=False)]
    books = [schema(SUN), schema(MOON)]
    data = books if container == "array" else {"@graph": books}
    initial = document(cards[::-1] if reverse else cards, heading, metadata=data)
    page = extract_page(initial)
    for book, number in [(SUN, 111), (MOON, 222)]:
        result = evaluate_page(initial, [SUN, MOON], book.id)
        assert (result["status"], result["reason_code"]) == (1, "placement_signs")
        assert [s["url"] for s in result["content_signals"]] == [f'https://offline.test/files/{number}.epub']
        metadata = next(s for s in page.subjects if s["source"] == "jsonld_book" and s["title"] == book.title)
        assert [s["url"] for s in metadata["signals"]] == [f'https://offline.test/files/{number}.epub']


@pytest.mark.parametrize("reverse", [False, True])
def test_unique_title_localizes_head_author_conflict(reverse):
    cards = [card(SUN, structured=False), card(MOON, structured=False)]
    initial = document(cards[::-1] if reverse else cards, metadata=[schema(SUN), schema(MOON, "author")])
    sun = evaluate_page(initial, [SUN, MOON], SUN.id)
    moon = evaluate_page(initial, [SUN, MOON], MOON.id)
    assert (sun["status"], sun["reason_code"]) == (1, "placement_signs")
    assert (moon["status"], moon["reason_code"]) == (2, "metadata_author_conflict")


def test_neighbour_jsonld_claiming_target_cannot_borrow_neighbours_epub():
    initial = document([card(SUN, own_file=False), card(MOON, structured=False, extra=script(schema(SUN)))])
    result = evaluate_page(initial, [SUN, MOON], SUN.id)
    assert (result["status"], result["reason_code"]) == (0, "content_not_confirmed")
    assert not result["content_signals"]
    page = extract_page(initial)
    owner = next(s for s in page.subjects if s["source"] == "book_card" and s["title"] == MOON.title)
    metadata = [s for s in page.subjects if s["source"] == "jsonld_book" and s["scope_id"] == owner["scope_id"]]
    assert len(metadata) == 1 and not metadata[0]["signals"]


@pytest.mark.parametrize("neighbour_error", [None, "title"])
def test_unowned_conflicting_book_cannot_be_dismissed_as_a_neighbour(neighbour_error):
    initial = document([card(SUN), card(MOON, neighbour_error)], metadata=schema(Book("unknown", SUN.author, "Неизвестная книга")))
    result = evaluate_page(initial, [SUN, MOON], SUN.id)
    assert (result["status"], result["reason_code"]) == (2, "metadata_title_conflict")


@pytest.mark.parametrize("target", [SUN, Book("other-sun", "Вера Другая", SUN.title)])
def test_ambiguous_head_author_conflict_is_not_assigned_by_card_order(target):
    other = Book("other-sun", "Вера Другая", SUN.title)
    metadata = schema(Book("unknown", "Неизвестный Автор", SUN.title))
    initial = document([card(SUN, structured=False), card(other, structured=False)], metadata=metadata)
    result = evaluate_page(initial, [SUN, other], target.id)
    assert (result["status"], result["reason_code"]) == (2, "metadata_author_conflict")


def test_overlapping_book_boundaries_are_not_proven_independent():
    initial = document([card(SUN, extra=card(MOON, "author"))])
    result = evaluate_page(initial, [SUN, MOON], SUN.id)
    assert result["status"] == 2


@pytest.mark.parametrize("issue", ["extraction_error", "metadata_title_conflict"])
def test_unscoped_errors_remain_blocking(issue):
    initial = document([card(SUN)])
    page = extract_page(initial)
    page.issues.append(issue)
    status, reason, _ = classify(initial, page, match_book(page, [SUN], SUN.id), [SUN])
    assert (status, reason) == (2, issue)


def test_unscoped_occurrence_of_a_scoped_error_still_blocks():
    initial = document([card(SUN), card(MOON, "title")])
    page = extract_page(initial)
    page.issues.append("metadata_title_conflict")
    status, reason, _ = classify(initial, page, match_book(page, [SUN, MOON], SUN.id), [SUN, MOON])
    assert (status, reason) == (2, "metadata_title_conflict")


def test_unknown_selected_scope_cannot_dismiss_a_known_conflict():
    initial = document([card(SUN), card(MOON, "title")])
    page = extract_page(initial)
    match = match_book(page, [SUN, MOON], SUN.id)
    match["subject"] = {**match["subject"], "scope_id": None}
    status, reason, _ = classify(initial, page, match, [SUN, MOON])
    assert (status, reason) == (2, "metadata_title_conflict")


def test_foreign_issue_explanation_survives_the_evidence_limit():
    extra = ''.join(f'<a href="/files/{n}.epub">Скачать EPUB</a>' for n in range(112, 121))
    result = evaluate_page(document([card(SUN, extra=extra), card(MOON, "title")]), [SUN, MOON], SUN.id)
    assert (result["status"], result["reason_code"]) == (1, "placement_signs")
    assert len(result["content_signals"]) == 10
    assert any(MOON.title in item and "независим" in item for item in result["evidence"])


def test_page_wide_service_error_is_not_hidden_by_good_cards():
    result = evaluate_page(document([card(SUN), card(MOON, "title")], title="503 Service unavailable"), [SUN, MOON], SUN.id)
    assert (result["status"], result["reason_code"]) == (2, "service_error")


@pytest.mark.parametrize("target_error", [False, True])
def test_jsonl_and_xlsx_keep_selected_card_decision_and_evidence(tmp_path, target_error):
    initial = document([card(SUN, "title" if target_error else None), card(MOON, None if target_error else "title")])
    target = {"url": URL, "expected_book_id": SUN.id, "expected_title": SUN.title,
              "expected_author": SUN.author, "source_row": 7}
    result = analyze_url(target, [SUN, MOON], fetcher=lambda url: initial)
    expected = (2, "metadata_title_conflict") if target_error else (1, "placement_signs")
    write_results([result], tmp_path / "export")
    exported = json.loads((tmp_path / "export/part2_results.jsonl").read_text(encoding="utf-8"))
    assert (exported["status"], exported["reason_code"]) == expected
    assert exported["expected_book_id"] == SUN.id and exported["source_row"] == 7
    assert [s["url"] for s in exported["content_signals"]] == ['https://offline.test/files/111.epub']
    assert exported["evidence"] and exported["extracted"]["issues"]
    workbook = openpyxl.load_workbook(tmp_path / "export/part2_results.xlsx", read_only=True)
    headers, row = list(workbook.active.values)
    values = dict(zip(headers, row))
    assert (values["status"], values["reason_code"]) == expected
    assert values["expected_book_id"] == SUN.id and values["evidence"]
    workbook.close()
