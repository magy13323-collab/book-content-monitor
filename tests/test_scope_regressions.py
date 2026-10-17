"""Adversarial identity/ownership pairs; synthetic documents, no live HTTP."""

import json

import pytest

from pages import FetchResult
from part2 import Book, analyze_url, evaluate_page
from test_browser import browser_factory
from test_part2 import synthetic_pdf
from test_new_rules import no_requests


BOOK = Book("north", "Alice Example", "The Northern Wind")
FILE = '<a href="/north.epub">Download EPUB</a>'
IDENTITY = ('<h1>The Northern Wind</h1><p class="book-author">Alice Example</p>'
            '<p>Book details and available reading formats.</p>')


def document(body, head=""):
    html = f'<html><head><meta charset="utf-8">{head}</head><body>{body}</body></html>'
    return FetchResult("https://offline.test/book", "https://offline.test/book", 200,
                       {"content-type": "text/html; charset=utf-8"}, html.encode())


@pytest.mark.parametrize("container", ["li", "tr", "td", "div", "section", "dd"])
@pytest.mark.parametrize("own_content", [False, True])
def test_bibliographic_region_owns_its_file(container, own_content):
    title, author = (BOOK.title, BOOK.author) if own_content else ("South Road", "Bob Different")
    block = (f'<{container}><b>{title}</b><p>Author: {author}</p>'
             f'{FILE}</{container}>')
    result = evaluate_page(document(f'<main>{IDENTITY}{block}</main>'), [BOOK], BOOK.id)
    assert (result["status"] == 1) == own_content
    assert bool(result["content_signals"]) == own_content


@pytest.mark.parametrize("container", ["li", "tr", "td", "div", "section"])
def test_nested_heading_and_byline_cannot_supply_primary_author(container):
    html = (f'<main><h1>{BOOK.title}</h1>{FILE}<{container}><h2>South Road</h2>'
            f'<p class="book-author">{BOOK.author}</p></{container}></main>')
    result = evaluate_page(document(html), [BOOK], BOOK.id)
    assert result["status"] != 1
    assert not result["extracted"]["authors"]


@pytest.mark.parametrize("own_identity", [False, True])
@pytest.mark.parametrize("container", ["li", "td", "div"])
def test_headingless_bibliography_does_not_lend_its_author(container, own_identity):
    title = BOOK.title if own_identity else 'South Road'
    html = (f'<main><h1>{BOOK.title}</h1>{FILE}<{container}><b>{title}</b>'
            f'<p class="book-author">{BOOK.author}</p></{container}></main>')
    result = evaluate_page(document(html), [BOOK], BOOK.id)
    assert (result["status"] == 1) == own_identity


@pytest.mark.parametrize("foreign_scope", [False, True])
def test_labeled_author_row_among_other_book_attributes(foreign_scope):
    rows = ('<ul><li>Author: <a href="/authors/alice">Alice Example</a></li>'
            '<li>Narrator: Carol Reader</li><li>Quality: 56 kb/s</li></ul>')
    if foreign_scope:
        rows = f'<div><b>South Road</b>{rows}</div>'
    result = evaluate_page(document(f'<main><h1>{BOOK.title}</h1>{rows}{FILE}</main>'), [BOOK], BOOK.id)
    assert (result["status"] == 1) == (not foreign_scope)


@pytest.mark.parametrize("own_author", [False, True])
@pytest.mark.parametrize("review", ['class="review"', 'itemprop="review"', 'role="comment"'])
def test_review_author_is_not_the_book_author(own_author, review):
    byline = '<p class="author">Alice Example</p>' if own_author else ''
    html = (f'<main><h1>{BOOK.title}</h1>{byline}{FILE}'
            f'<div {review}><p class="author">Alice Example</p><p>A reader review.</p></div></main>')
    result = evaluate_page(document(html), [BOOK], BOOK.id)
    assert (result["status"] == 1) == own_author
    assert result["extracted"]["authors"] == ([BOOK.author] if own_author else [])


@pytest.mark.parametrize("own_document", [False, True])
@pytest.mark.parametrize("metadata", [True, False])
def test_pdf_recommendation_does_not_own_document_text(own_document, metadata):
    title, author = (BOOK.title, BOOK.author) if own_document else ("South Road", "Bob Different")
    meta = {"/Title": title, "/Author": author} if metadata else {}
    pdf = synthetic_pdf(title, author, meta, note="Recommended: The Northern Wind by Alice Example")
    initial = FetchResult("https://offline.test/book.pdf", "https://offline.test/book.pdf", 200,
                          {"content-type": "application/pdf"}, pdf)
    result = evaluate_page(initial, [BOOK], BOOK.id)
    assert (result["status"] == 1) == own_document
    assert bool(result["content_signals"]) == own_document


@pytest.mark.parametrize("own_content", [False, True])
@pytest.mark.parametrize("wrapper", ["body", "main"])
def test_second_article_is_evaluated_independently(own_content, wrapper):
    html = ('<article><h1>Library news</h1><p>Today a new library opened.</p></article>'
            f'<article><h2>{BOOK.title}</h2><p class="book-author">{BOOK.author}</p>'
            f'{FILE if own_content else ""}</article>')
    if wrapper == "main":
        html = f'<main>{html}</main>'
    result = evaluate_page(document(html), [BOOK], BOOK.id)
    assert (result["status"] == 1) == own_content
    assert result["match"]["state"] == "matched"


@pytest.mark.parametrize("container", ["li", "div", "section", "td"])
@pytest.mark.parametrize("own_content", [False, True])
def test_heading_defines_a_local_card_without_a_special_tag(container, own_content):
    html = (f'<main><h1>Library news</h1><{container}><h2>{BOOK.title}</h2>'
            f'<p class="book-author">{BOOK.author}</p>{FILE if own_content else ""}</{container}>'
            '<div><h2>South Road</h2><p class="book-author">Bob Different</p>'
            f'{FILE}</div></main>')
    result = evaluate_page(document(html), [BOOK], BOOK.id)
    assert (result["status"] == 1) == own_content


@pytest.mark.parametrize("own_content", [False, True])
def test_nested_card_heading_does_not_own_its_outer_sibling_file(own_content):
    html = (f'<main>{IDENTITY}<div><div><h2>South Road</h2>'
            '<p class="book-author">Bob Different</p>' + FILE + '</div>'
            f'{FILE if own_content else ""}</div></main>')
    result = evaluate_page(document(html), [BOOK], BOOK.id)
    assert (result["status"] == 1) == own_content


@pytest.mark.parametrize("own_content", [False, True])
def test_title_wrapper_preserves_its_parent_identity(own_content):
    title = BOOK.title if own_content else 'South Road'
    html = (f'<main><h1>{BOOK.title}</h1><div><header><h2>{title}</h2></header>'
            f'<p class="book-author">{BOOK.author}</p><p>Book description and reading formats.</p>'
            f'{FILE}</div></main>')
    result = evaluate_page(document(html), [BOOK], BOOK.id)
    assert (result["status"] == 1) == own_content


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("same_title", [False, True])
def test_primary_jsonld_conflicts_but_nested_recommendation_does_not(nested, same_title):
    other = {"@type": "Book", "name": BOOK.title if same_title else "South Road",
             "author": {"name": "Bob Different"}}
    primary = {"@type": "Book", "name": BOOK.title, "author": {"name": BOOK.author},
               "isRelatedTo": other}
    data = primary if nested else other
    initial = document(f'<main>{IDENTITY}{FILE}</main>',
                       '<script type="application/ld+json">' + json.dumps(data) + '</script>')
    result = evaluate_page(initial, [BOOK], BOOK.id)
    assert result["status"] == (1 if nested else 2)


@pytest.mark.parametrize("hidden", [False, True])
def test_http_css_visibility_is_not_assumed(hidden):
    css = '.offer { display: none; }' if hidden else '.offer { color: navy; }'
    initial = document(f'<main>{IDENTITY}<div class="offer">{FILE}</div></main>',
                       f'<style>{css}</style>')
    # HTTP cannot establish computed visibility; auto mode must use Chromium.
    result = evaluate_page(initial, [BOOK], BOOK.id)
    assert result["status"] == 2
    assert result["reason_code"] == "visibility_unverified"


@pytest.mark.parametrize("own_content", [False, True])
def test_auto_checks_plain_actions_with_executable_script(own_content, browser_factory):
    probe = browser_factory()
    link = FILE if own_content else '<div><h2>South Road</h2><p>Bob Different</p>' + FILE + '</div>'
    script = '<script>setTimeout(() => { document.querySelector("#actions").innerHTML = ' + json.dumps(link) + '; }, 500);</script>'
    initial = document(f'<main>{IDENTITY}<div id="actions"></div></main>{script}')
    probe.fixture_routes[initial.url] = (200, initial.body.decode())
    result = analyze_url({"url": initial.url, "expected_book_id": BOOK.id}, [BOOK],
                         fetcher=lambda url: initial, browser=probe)
    assert result["method"] == "Chromium"
    if own_content:
        assert result["browser_attempt"]["ready"]
    assert (result["status"] == 1) == own_content


@pytest.mark.parametrize("hidden", [False, True])
def test_real_browser_checks_css_visibility(hidden, browser_factory):
    probe = browser_factory()
    css = '.offer { display: none; }' if hidden else '.offer { color: navy; }'
    initial = document(f'<main>{IDENTITY}<div class="offer">{FILE}</div></main>', f'<style>{css}</style>')
    probe.fixture_routes[initial.url] = (200, initial.body.decode())
    result = analyze_url({"url": initial.url, "expected_book_id": BOOK.id}, [BOOK],
                         fetcher=lambda url: initial, browser=probe)
    assert result["method"] == "Chromium"
    assert result["browser_attempt"]["ready"]
    assert (result["status"] == 1) == (not hidden)


