"""Targeted v3 review regressions. Expectations fixed before implementation."""
from pathlib import Path
from types import SimpleNamespace
import json
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from pages import FetchResult
from part2 import Book, analyze_url, evaluate_page
from browser import BrowserProbe
from test_part2 import synthetic_pdf

BOOK = Book('north', 'Alice Example', 'The Northern Wind')
URL = 'https://offline.v3/book'
FILE = '<a href="/north.epub">Download EPUB</a>'
DETAILS = '<p>Public bibliographic details, publication information and reading formats.</p>'


def identity(book=BOOK, author='book-author', heading='h1'):
    return f'<{heading}>{book.title}</{heading}><p class="{author}">{book.author}</p>'


def document(body, head=''):
    return FetchResult(URL, URL, 200, {'content-type': 'text/html; charset=utf-8'},
                       ('<html><head><meta charset="utf-8">' + head + '</head><body>' + body + '</body></html>').encode())


def evaluate(body, head='', book=BOOK):
    return evaluate_page(document(body, head), [book], book.id)


@pytest.mark.parametrize('own', [False, True])
def test_flat_resource_requires_local_identity(own):
    context = (f'{BOOK.title}. Author: {BOOK.author}' if own else 'New release: South Road. Author: Bob Different')
    result = evaluate('<main>' + identity() + DETAILS + '<p>' + context + '</p>' + FILE + '</main>')
    assert result['status'] == 1 if own else result['status'] != 1


@pytest.mark.parametrize('own', [False, True])
def test_generic_author_requires_bibliographic_context(own):
    body = identity(author='author') if own else (
        f'<h1>{BOOK.title}</h1><div class="entry-42"><p class="author">{BOOK.author}</p>'
        '<time datetime="2026-10-01">1 October</time><p>I enjoyed the ending very much.</p></div>')
    result = evaluate('<main>' + body + DETAILS + FILE + '</main>')
    assert result['status'] == 1 if own else result['status'] != 1


@pytest.mark.parametrize('contents', [False, True])
def test_membership_requires_declared_contents(contents):
    heading = 'Состав сборника' if contents else 'Вам может понравиться'
    body = '<main><h1>Alice Example — сборник книг</h1><p class="book-author">Alice Example</p>'
    body += DETAILS + f'<div><h2>{heading}</h2><ul><li>{BOOK.title}</li></ul></div>'
    body += '<a href="/all.zip">Скачать сборник ZIP</a></main>'
    result = evaluate(body)
    assert result['status'] == 1 if contents else result['status'] != 1


@pytest.mark.parametrize('review', [False, True])
def test_pdf_review_is_not_the_work(review):
    body = synthetic_pdf(note='Book review. A critical analysis of the novel and its literary reception.' if review else '')
    result = evaluate_page(FetchResult(URL, URL, 200, {'content-type': 'application/pdf'}, body), [BOOK], BOOK.id)
    assert result['status'] != 1 if review else result['status'] == 1


BUTTONS = [
    '<a href="/north.epub"><svg aria-hidden="true"></svg></a>',
    '<a href="/north.epub" download></a>',
    '<a href="/north.epub" aria-label="Download EPUB"><svg></svg></a>',
    '<a href="/north.epub" title="Download EPUB"><svg></svg></a>',
    '<a href="/north.epub"><img alt="Download EPUB" src="/icon.png"></a>',
    '<a href="/north.epub" download="north.epub"><svg></svg></a>',
]


@pytest.mark.parametrize('button', BUTTONS)
@pytest.mark.parametrize('own', [False, True])
def test_file_buttons_keep_ownership(button, own):
    content = button if own else '<section><h2>South Road</h2><p class="book-author">Bob Different</p>' + button + '</section>'
    result = evaluate('<main>' + identity() + DETAILS + content + '</main>')
    assert result['status'] == 1 if own else result['status'] != 1


@pytest.mark.parametrize('extension', ['mp3', 'm4a', 'ogg'])
@pytest.mark.parametrize('own', [False, True])
def test_direct_audio_resources(extension, own):
    link = f'<a href="/recording.{extension}">Download MP3</a>'
    content = link if own else '<article><h2>South Road</h2><p class="book-author">Bob Different</p>' + link + '</article>'
    result = evaluate('<main>' + identity() + DETAILS + content + '</main>')
    assert result['status'] == 1 if own else result['status'] != 1
    if own:
        assert result['observed_formats'] == ['audio']


@pytest.mark.parametrize('own', [False, True])
def test_sidebar_is_a_region_not_a_deletion_rule(own):
    title = BOOK.title if own else 'South Road'
    author = BOOK.author if own else 'Bob Different'
    panel = f'<aside class="sidebar"><h2>{title}</h2><p class="book-author">{author}</p>{FILE}</aside>'
    result = evaluate('<main>' + identity() + DETAILS + panel + '</main>')
    assert result['status'] == 1 if own else result['status'] != 1


@pytest.fixture(scope='module')
def chromium():
    pytest.importorskip('playwright.sync_api')
    with BrowserProbe(timeout_ms=3500, fixture_routes={}) as probe:
        if not probe.start():
            pytest.skip(probe.failure)
        yield probe


@pytest.mark.parametrize('enabled', [False, True])
def test_spa_without_initial_book_is_not_a_confident_negative(chromium, enabled):
    resolved = '<main>' + identity() + DETAILS + FILE + '</main>'
    shell = '<main><div id="app"></div><p>Library catalogue and public reading service. Select your next book.</p></main>'
    shell += '<script>setTimeout(() => { document.querySelector("#app").innerHTML = ' + json.dumps(resolved) + '; }, 350);</script>'
    initial = document(shell)
    chromium.fixture_routes[URL] = (200, initial.body.decode())
    result = analyze_url({'url': URL, 'expected_book_id': BOOK.id}, [BOOK], fetcher=lambda _: initial,
                         browser=chromium if enabled else None)
    assert result['status'] == (1 if enabled else 2)
    assert result['browser_attempt']['attempted'] == enabled


@pytest.mark.parametrize('hidden', [False, True])
def test_inline_cascade_honors_important(hidden):
    style = 'display:none!important;display:block' if hidden else 'display:block!important;display:none'
    result = evaluate('<main>' + identity() + DETAILS + '<a href="/north.epub" style="' + style + '">Download EPUB</a></main>')
    assert result['status'] != 1 if hidden else result['status'] == 1


@pytest.mark.parametrize('hidden', [False, True])
def test_css_variables_need_computed_visibility(chromium, hidden):
    style = 'style="--availability:0;opacity:var(--availability)"' if hidden else ''
    initial = document('<main>' + identity() + DETAILS + '<a href="/north.epub" ' + style + '>Download EPUB</a></main>')
    chromium.fixture_routes[URL] = (200, initial.body.decode())
    result = analyze_url({'url': URL, 'expected_book_id': BOOK.id}, [BOOK], fetcher=lambda _: initial, browser=chromium)
    assert result['status'] != 1 if hidden else result['status'] == 1


@pytest.mark.parametrize('conflict', [False, True])
def test_jsonld_is_compared_with_its_book_region(conflict):
    schema = {'@type': 'Book', 'name': 'South Road' if conflict else BOOK.title, 'author': {'name': BOOK.author}}
    head = '<script type="application/ld+json">' + json.dumps(schema) + '</script>'
    body = ('<main>' + identity() + DETAILS + FILE + '</main>') if conflict else (
        '<main><h1>Library announcements</h1><article>' + identity(heading='h2') + DETAILS + FILE + '</article></main>')
    result = evaluate(body, head)
    assert result['status'] == (2 if conflict else 1)


@pytest.mark.parametrize('reader', [False, True])
def test_collection_word_does_not_erase_individual_book(reader):
    book = Book('catalog', 'Alice Example', 'The Catalog of Lost Stars')
    link = '<a href="/read/north">Read online</a>' if reader else FILE
    result = evaluate('<main>' + identity(book) + DETAILS + link + '</main>', book=book)
    assert result['status'] == 1


def test_evidence_strings_are_unique():
    playlist = '<div id="player-outer"><div id="player-inner">1. First chapter<br>2. Second chapter</div></div>'
    result = evaluate('<main>' + identity() + DETAILS + playlist + '</main>', '<title>Аудиокнига The Northern Wind</title>')
    assert result['status'] == 1
    assert len(result['evidence']) == len(set(result['evidence']))


@pytest.mark.parametrize('navigation', ['redirect', 'client', 'reload'])
def test_only_http_redirects_are_exported(navigation):
    body = document('<main>' + identity() + DETAILS + FILE + '</main>').body.decode()
    first_url = URL
    next_url = URL if navigation == 'reload' else URL + '/resolved'
    frame = object()
    request1 = SimpleNamespace(is_navigation_request=lambda: True, redirected_from=None)
    request2 = SimpleNamespace(is_navigation_request=lambda: True, redirected_from=request1 if navigation == 'redirect' else None)

    class Page:
        main_frame = frame
        url = next_url

        def on(self, name, callback):
            self.callback = callback

        def goto(self, *args, **kwargs):
            self.callback(SimpleNamespace(request=request1, frame=frame, status=302 if navigation == 'redirect' else 200,
                                          url=first_url, headers={'content-type': 'text/html'}))
            self.callback(SimpleNamespace(request=request2, frame=frame, status=200,
                                          url=next_url, headers={'content-type': 'text/html'}))

        def evaluate(self, _):
            return body

        def locator(self, _):
            return SimpleNamespace(count=lambda: 0)

    page = Page()
    context = SimpleNamespace(new_page=lambda: page, route=lambda *args: None, close=lambda: None)
    probe = BrowserProbe()
    probe.browser = SimpleNamespace(new_context=lambda **kwargs: context)
    result = probe.check(URL)
    assert not result.error_code
    expected = [{'status': 302, 'from': first_url, 'to': next_url}] if navigation == 'redirect' else []
    assert result.redirects == expected
