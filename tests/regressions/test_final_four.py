"""Only the four reported v4 mechanisms; expectations fixed before code changes."""
from pathlib import Path
from types import SimpleNamespace
import json
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from browser import BrowserProbe
from pages import FetchResult
from part2 import Book, analyze_url, evaluate_page

BOOK = Book('north', 'Alice Example', 'The Northern Wind')
URL = 'https://offline.four/book'
FILE = '<a href="/north.epub">Download EPUB</a>'
DETAILS = '<p>Public bibliographic details, publication information and available reading formats.</p>'


def document(body):
    return FetchResult(URL, URL, 200, {'content-type': 'text/html; charset=utf-8'},
                       ('<html><head><meta charset="utf-8"></head><body>' + body + '</body></html>').encode())


def identity(title=BOOK.title, author=BOOK.author, heading='h1'):
    return f'<{heading}>{title}</{heading}><p class="book-author">{author}</p>'


def evaluate(body):
    return evaluate_page(document(body), [BOOK], BOOK.id)


@pytest.mark.parametrize('field', ['book-author', 'author', 'label'])
@pytest.mark.parametrize('participant', [False, True])
@pytest.mark.parametrize('own', [False, True])
def test_A_primary_author_over_secondary_reference(field, participant, own):
    author = BOOK.author if own else 'Bob Different'
    byline = f'<p>Author: {author}</p>' if field == 'label' else f'<p class="{field}">{author}</p>'
    secondary = '<a href="/authors/alice-example">Alice Example</a>'
    if participant:
        secondary = '<div><p>Participant</p>' + secondary + '</div>'
    result = evaluate('<main><h1>' + BOOK.title + '</h1>' + byline + DETAILS + FILE + secondary + '</main>')
    assert result['status'] == 1 if own else result['status'] != 1
    assert result['extracted']['authors'] == [author]


@pytest.mark.parametrize('tag', ['aside', 'div'])
@pytest.mark.parametrize('own', [False, True])
def test_B_independent_card_outside_main(tag, own):
    title, author = (BOOK.title, BOOK.author) if own else ('South Road', 'Bob Different')
    outside = f'<{tag} class="book-card">' + identity(title, author, 'h2') + FILE + f'</{tag}>'
    result = evaluate('<main>' + identity() + DETAILS + '</main>' + outside)
    assert result['status'] == 1 if own else result['status'] != 1
    if own:
        assert all(signal['scope_title'] == BOOK.title for signal in result['content_signals'])


def test_B_bare_external_link_does_not_inherit_main():
    result = evaluate('<main>' + identity() + DETAILS + '</main>' + FILE)
    assert result['status'] != 1


@pytest.mark.parametrize('intro', ['Sponsored offer', 'Publisher bulletin'])
@pytest.mark.parametrize('own', [False, True])
def test_C_neighbouring_bibliographic_identity(intro, own):
    title, author = (BOOK.title, BOOK.author) if own else ('South Road', 'Bob Different')
    context = f'<p>{intro}: {title} by {author}</p>'
    link = FILE if own else '<a href="/south.epub">Download EPUB</a>'
    result = evaluate('<main>' + identity() + DETAILS + context + link + '</main>')
    assert result['status'] == (1 if own else 2)


@pytest.fixture(scope='module')
def chromium():
    pytest.importorskip('playwright.sync_api')
    with BrowserProbe(timeout_ms=4500, fixture_routes={}) as probe:
        if not probe.start():
            pytest.skip(probe.failure)
        yield probe


@pytest.mark.parametrize('mode', ['auto', 'off'])
@pytest.mark.parametrize('foreign', [False, True])
@pytest.mark.parametrize('shell', ['spinner', 'loading'])
def test_D_target_mount_waits_independently(chromium, shell, foreign, mode):
    mount = '<div id="app"><span class="spinner"></span></div>' if shell == 'spinner' else (
        '<div id="root"><div class="loading">Loading</div></div>')
    selector = '#app' if shell == 'spinner' else '#root'
    body = '<main>' + DETAILS + mount
    if foreign:
        body += '<article>' + identity('South Road', 'Bob Different') + DETAILS + '<a href="/south.epub">Download EPUB</a></article>'
    resolved = '<article>' + identity() + DETAILS + FILE + '</article>'
    body += '</main><script>setTimeout(() => { document.querySelector(' + json.dumps(selector)
    body += ').innerHTML = ' + json.dumps(resolved) + '; }, 350);</script>'
    initial = document(body)
    chromium.fixture_routes[URL] = (200, initial.body.decode())
    result = analyze_url({'url': URL, 'expected_book_id': BOOK.id}, [BOOK], fetcher=lambda _: initial,
                         browser=chromium if mode == 'auto' else None)
    assert result['status'] == (1 if mode == 'auto' else 2)
    assert result['browser_attempt']['attempted'] == (mode == 'auto')
    if mode == 'auto':
        assert result['browser_attempt']['ready']
        assert all(signal['url'] == 'https://offline.four/north.epub' for signal in result['content_signals'])


@pytest.mark.parametrize('own', [False, True])
def test_D_static_book_with_ordinary_js_needs_no_browser(own):
    title, author = (BOOK.title, BOOK.author) if own else ('South Road', 'Bob Different')
    initial = document('<main>' + identity(title, author) + DETAILS + FILE + '</main>'
                       '<script>document.documentElement.dataset.theme = "light";</script>')
    browser = SimpleNamespace(check=lambda _: pytest.fail('Unexpected browser attempt'))
    result = analyze_url({'url': URL, 'expected_book_id': BOOK.id}, [BOOK], fetcher=lambda _: initial, browser=browser)
    assert result['status'] == (1 if own else 0)
    assert not result['browser_attempt']['attempted']
