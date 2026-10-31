"""Fixed expectations for complete coauthor sets; synthetic HTML only."""

import json

import pytest
import requests

from pages import FetchResult
from part2 import Book, author_matches, evaluate_page


A, B, C = "Анна Лесная", "Борис Северный", "Вера Другая"
AUTHORS = "Анна Лесная; Борис Северный"
TITLE = "Учебный маршрут. Том 1"
FILE = '<a href="/north.epub">Скачать EPUB</a>'


@pytest.fixture(autouse=True)
def no_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Live HTTP forbidden")
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


def evaluate(authors, structured=None, expected=AUTHORS, content=FILE, heading=TITLE):
    head = ""
    if structured is not None:
        data = {"@type": "Book", "name": TITLE, "author": structured}
        head = '<script type="application/ld+json">' + json.dumps(data) + '</script>'
    bylines = ''.join(f'<p class="book-author">{name}</p>' for name in authors)
    html = (f'<html><head><meta charset="utf-8">{head}</head><body><main>'
            f'<h1>{heading}</h1>{bylines}<p>Карточка книги и доступные форматы чтения.</p>'
            f'{content}</main></body></html>')
    document = FetchResult("https://offline.test/book", "https://offline.test/book", 200,
                           {"Content-Type": "text/html; charset=utf-8"}, html.encode())
    return evaluate_page(document, [Book("north", expected, TITLE)], "north")


@pytest.mark.parametrize("authors", [
    [A, B], [B, A], ["Лесная Анна", "Северный Борис"], [AUTHORS],
    ["Борис Северный ; Анна Лесная"], [A, B, A],
], ids=["elements", "order", "surname-first", "semicolon", "semicolon-order", "duplicate"])
def test_equivalent_html_coauthors_match(authors):
    result = evaluate(authors)
    assert (result["status"], result["reason_code"]) == (1, "placement_signs")


@pytest.mark.parametrize(("authors", "structured"), [
    ([], [{"@type": "Person", "name": A}, {"@type": "Person", "name": B}]),
    ([], [B, A]),
    ([], AUTHORS),
    ([AUTHORS], [{"name": B}, {"name": A}]),
    ([A, B], "Борис Северный; Анна Лесная"),
], ids=["person-array", "string-array", "string", "dom-string-array", "dom-elements-string"])
def test_equivalent_jsonld_coauthors_match(authors, structured):
    result = evaluate(authors, structured)
    assert (result["status"], result["reason_code"]) == (1, "placement_signs")
    assert "metadata_author_conflict" not in result["extracted"]["issues"]


@pytest.mark.parametrize("authors", [
    [A], [B], [A, C], [A, B, C], [AUTHORS, C], [A, A],
    ["Анна Северный", "Борис Лесная"],
    ["Анна Лесная, Борис Северный"], ["Анна Лесная / Борис Северный"],
    ["Анна Лесная & Борис Северный"], ["Анна Лесная и Борис Северный"],
    ["Анна Лесная and Борис Северный"], ["Анна Лесная Борис Северный"],
    [AUTHORS + ";"], ["Анна Лесная;; Борис Северный"],
], ids=["first-only", "second-only", "foreign", "extra", "extra-after-string", "repeated-first",
        "mixed-name-tokens", "comma", "slash", "ampersand", "and-ru", "and-en", "no-separator",
        "trailing-empty", "empty-member"])
def test_incomplete_foreign_or_ambiguous_html_authors_conflict(authors):
    result = evaluate(authors)
    assert (result["status"], result["reason_code"]) == (2, "author_conflict")
    assert result["match"]["state"] == "author_conflict"


@pytest.mark.parametrize("expected", ["Анна Лесная, Борис Северный", AUTHORS + ";"])
def test_catalog_separator_is_not_guessed(expected):
    result = evaluate([A, B], expected=expected)
    assert (result["status"], result["reason_code"]) == (2, "author_conflict")


@pytest.mark.parametrize(("authors", "structured", "reason"), [
    ([], [A], "author_conflict"),
    ([], [A, C], "author_conflict"),
    ([], [A, B, C], "author_conflict"),
    ([A, B], [A], "metadata_author_conflict"),
    ([A], [A, B], "metadata_author_conflict"),
    ([A, B], [A, C], "metadata_author_conflict"),
    ([A, B], [A, B, C], "metadata_author_conflict"),
], ids=["partial", "foreign", "extra", "ld-partial", "dom-partial", "ld-foreign", "ld-extra"])
def test_jsonld_requires_the_complete_same_set(authors, structured, reason):
    result = evaluate(authors, structured)
    assert (result["status"], result["reason_code"]) == (2, reason)


@pytest.mark.parametrize(("authors", "expected", "status"), [
    ([A], A, 1), (["Лесная Анна"], A, 1), ([A, B], A, 2),
    (["А. Лесная"], A, 2),
], ids=["single", "surname-first", "extra-coauthor", "initial"])
def test_single_author_matching_stays_conservative(authors, expected, status):
    result = evaluate(authors, expected=expected)
    assert result["status"] == status


@pytest.mark.parametrize(("expected", "observed", "matches"), [
    (A, "Лесная Анна", True), ("А. Лесная", "Лесная А.", False),
    ("А. Лесная", "А. Лесная", True), ("", "", False),
    (AUTHORS, "Северный Борис; Лесная Анна", True),
])
def test_author_matches_keeps_its_string_interface(expected, observed, matches):
    assert author_matches(expected, observed) is matches


@pytest.mark.parametrize("byline", [AUTHORS, "Борис Северный; Анна Лесная"])
def test_inline_resource_bibliography_accepts_equivalent_sets(byline):
    result = evaluate([A, B], content=f'<p>{TITLE} by {byline}</p>{FILE}')
    assert (result["status"], result["reason_code"]) == (1, "placement_signs")


def test_inline_resource_bibliography_rejects_partial_set():
    result = evaluate([A, B], content=f'<p>{TITLE} by {A}</p>{FILE}')
    assert (result["status"], result["reason_code"]) == (2, "content_scope_unresolved")


def test_separate_labelled_coauthor_rows_keep_their_resource():
    result = evaluate([], content=f'<p>Автор: {A}</p><p>Автор: {B}</p>{FILE}')
    assert (result["status"], result["reason_code"]) == (1, "placement_signs")


@pytest.mark.parametrize(("byline", "status"), [(AUTHORS, 1), (A, 0)])
def test_resource_owner_requires_the_complete_set(byline, status):
    content = f'<div><b>{TITLE}</b><p class="book-author">{byline}</p>{FILE}</div>'
    result = evaluate([A, B], content=content)
    assert result["status"] == status


def test_decorated_heading_accepts_coauthor_order():
    result = evaluate([A, B], heading=f'{TITLE} Борис Северный; Анна Лесная')
    assert (result["status"], result["reason_code"]) == (1, "placement_signs")
