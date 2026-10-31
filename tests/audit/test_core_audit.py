"""Independent offline audit. Expectations were fixed in INVARIANTS.md before execution.

Run explicitly; this file is outside the production test suite. No production files
or fixtures are patched. PDF inputs and browser routes are entirely synthetic.
"""
from io import BytesIO
import json
from pathlib import Path
import sys

import pytest
import requests
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from browser import BrowserProbe
from pages import FetchResult, fetch_url
from part2 import Book, analyze_url, evaluate_page

OUT = Path(__file__).resolve().parent
URL = 'https://offline.audit/books/north'
BOOK = Book('audit-north', 'Alice Example', 'The Northern Wind')
FILE = '<a href="/north.epub">Download EPUB</a>'
OTHER_FILE = '<a href="/south.epub">Download EPUB</a>'
DETAILS = '<p>A public book record with bibliographic details and reading formats.</p>'
BODY = 'The traveler crossed the old bridge and walked north before sunset. '
OBSERVATIONS = []


def html(body, head='', url=URL):
    source = '<html><head><meta charset="utf-8">' + head + '</head><body>' + body + '</body></html>'
    return FetchResult(url, url, 200, {'Content-Type': 'text/html; charset=utf-8'}, source.encode())


def identity(book=BOOK, heading='h1', author=True):
    return f'<{heading}>{book.title}</{heading}>' + (
        f'<p class="book-author">{book.author}</p>' if author else '')


def card(content='', book=BOOK, head=''):
    return html('<main>' + identity(book) + DETAILS + content + '</main>', head)


def schema(book=BOOK, **extra):
    return {'@type': 'Book', 'name': book.title, 'author': {'name': book.author}, **extra}


def ld(data):
    return '<script type="application/ld+json">' + json.dumps(data) + '</script>'


def evaluate(doc, book=BOOK):
    return evaluate_page(doc, [book], book.id)


def observe(request, result, statuses, invariants, *, resource=None, attempted=None, ready=None, formats=None):
    failures = []
    if result['status'] not in statuses:
        failures.append(f"status {result['status']} not in {list(statuses)}")
    if resource is not None and resource not in [signal['url'] for signal in result['content_signals']]:
        failures.append('resource URL differs from document base: ' + resource)
    if attempted is not None and result.get('browser_attempt', {}).get('attempted') != attempted:
        failures.append('browser attempted must be ' + str(attempted))
    if ready is not None and result.get('browser_attempt', {}).get('ready') != ready:
        failures.append('browser ready must be ' + str(ready))
    if formats is not None and result['observed_formats'] != formats:
        failures.append('observed_formats must be ' + repr(formats))
    OBSERVATIONS.append({'case': request.node.name, 'invariants': invariants,
                         'expected_statuses': list(statuses), 'expected_resource': resource,
                         'expected_browser_attempted': attempted, 'expected_formats': formats,
                         'expected_browser_ready': ready,
                         'status': result['status'], 'reason': result['reason_code'],
                         'match': result['match'], 'issues': result['extracted']['issues'],
                         'subjects': result['extracted']['subjects'],
                         'signals': result['content_signals'],
                         'observed_signals': result['extracted']['observed_content_signals'],
                         'browser': result.get('browser_attempt'), 'evidence': result['evidence'],
                         'passed': not failures, 'failures': failures})
    assert not failures, '; '.join(failures) + '; actual reason=' + result['reason_code']


@pytest.fixture(scope='module', autouse=True)
def save_observations():
    yield
    (OUT / 'observations.json').write_text(json.dumps(OBSERVATIONS, ensure_ascii=False, indent=2), encoding='utf-8')


@pytest.fixture(autouse=True)
def no_external_http(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('External HTTP is forbidden in this audit')
    monkeypatch.setattr(requests.sessions.Session, 'request', denied)


@pytest.fixture(scope='module')
def chromium():
    pytest.importorskip('playwright.sync_api')
    probe = BrowserProbe(timeout_ms=7000, fixture_routes={})
    if not probe.start():
        pytest.skip(probe.failure)
    yield probe
    probe.close()


def browser_result(probe, initial, book=BOOK, *, http=None):
    probe.fixture_routes[initial.url] = (200, initial.body.decode())
    return analyze_url({'url': initial.url, 'expected_book_id': book.id}, [book],
                       fetcher=lambda url: http or initial, browser=probe)


def pdf_bytes(openings, metadata):
    writer = PdfWriter()
    writer.add_metadata(metadata)
    for lines in openings:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                 NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({
            NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        lines = list(lines) + [BODY] * 25
        instructions = 'BT /F1 12 Tf 40 740 Td ' + ' '.join(
            f'({line}) Tj 0 -16 Td' for line in lines) + ' ET'
        stream.set_data(instructions.encode('ascii'))
        page[NameObject('/Contents')] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def pdf_document(openings, metadata, url=URL + '.pdf'):
    return FetchResult(url, url, 200, {'Content-Type': 'application/pdf'}, pdf_bytes(openings, metadata))


@pytest.mark.parametrize('own', [True, False])
def test_01_direct_file_identity(request, own):
    link = FILE if own else '<a href="/other.epub">Download South Road EPUB</a>'
    observe(request, evaluate(card(link)), (1,) if own else (0, 2), ['I1', 'I2'])


@pytest.mark.parametrize('layout', ['p', 'li', 'table', 'neighbor'])
def test_02_advertisements_without_sections(request, layout):
    advert = 'Реклама другой книги: South Road by Bob Different.'
    if layout == 'table':
        content = f'<table><tr><td>{advert}</td><td>{OTHER_FILE}</td></tr></table>'
    elif layout == 'neighbor':
        content = f'<p>{advert}</p>{OTHER_FILE}'
    else:
        content = f'<{layout}>{advert}{OTHER_FILE}</{layout}>'
    observe(request, evaluate(card(content)), (0, 2), ['I2', 'I3'])


@pytest.mark.parametrize('own', [True, False])
def test_03_aside_is_not_an_ownership_rule(request, own):
    book = BOOK if own else Book('other', 'Bob Different', 'South Road')
    content = '<aside>' + identity(book, 'h2') + (FILE if own else OTHER_FILE) + '</aside>'
    observe(request, evaluate(card(content)), (1,) if own else (0, 2), ['I3', 'I8'])


@pytest.mark.parametrize('role', ['user-meta', 'review', 'profile'])
@pytest.mark.parametrize('own_author', [False, True])
def test_04_person_role_does_not_identify_book(request, role, own_author):
    body = identity(author=own_author) + DETAILS + FILE
    body += f'<div class="{role}"><p class="author">Alice Example</p><p>Reader account and review.</p></div>'
    observe(request, evaluate(html('<main>' + body + '</main>')), (1,) if own_author else (0, 2), ['I2', 'I3'])


@pytest.mark.parametrize('own_file', [True, False])
def test_05_multiple_independent_articles(request, own_file):
    body = '<article><h1>Library news</h1>' + DETAILS + '</article><article>'
    body += identity(heading='h2') + (FILE if own_file else '') + '</article><article>'
    body += identity(Book('other', 'Bob Different', 'South Road'), 'h2') + OTHER_FILE + '</article>'
    observe(request, evaluate(html(body)), (1,) if own_file else (0, 2), ['I1', 'I2', 'I8'])


@pytest.mark.parametrize('target_is_inner', [True, False])
def test_06_nested_local_identity(request, target_is_inner):
    other = Book('other', 'Bob Different', 'South Road')
    outer, inner = (other, BOOK) if target_is_inner else (BOOK, other)
    body = '<article>' + identity(outer) + DETAILS + '<div class="book-card">'
    body += identity(inner, 'h2') + (FILE if target_is_inner else OTHER_FILE) + '</div></article>'
    observe(request, evaluate(html(body)), (1,) if target_is_inner else (0, 2), ['I1', 'I2'])


@pytest.mark.parametrize('toc', [True, False])
def test_07_collection_contents_are_not_recommendations(request, toc):
    heading = 'Состав сборника' if toc else 'Рекомендуем также прочитать'
    body = '<main><h1>Alice Example — сборник книг</h1><p class="book-author">Alice Example</p>'
    body += DETAILS + f'<section><h2>{heading}</h2><ul><li>{BOOK.title}</li></ul></section>'
    body += '<a href="/books.zip">Скачать сборник ZIP</a></main>'
    observe(request, evaluate(html(body)), (1,) if toc else (0, 2), ['I1', 'I3'])


@pytest.mark.parametrize('style', ['', 'display:none', 'opacity:0', 'visibility:collapse'])
def test_08_inline_visibility(request, style):
    link = f'<a style="{style}" href="/north.epub">Download EPUB</a>'
    observe(request, evaluate(card(link)), (1,) if not style else (0, 2), ['I1', 'I6'])


@pytest.mark.parametrize('hidden', [False, True])
def test_09_computed_visibility_with_real_chromium(request, chromium, hidden):
    css = '.resource {display:none}' if hidden else '.resource {color:navy}'
    initial = card('<div class="resource">' + FILE + '</div>', head='<style>' + css + '</style>')
    observe(request, browser_result(chromium, initial), (0, 2) if hidden else (1,), ['I6', 'I8'], attempted=True, ready=True)


@pytest.mark.parametrize('relative', [False, True])
def test_10_base_href_preserves_resource_identity(request, relative):
    expected = 'https://files.audit/library/north.epub'
    href = 'north.epub' if relative else expected
    initial = card(f'<a href="{href}">Download EPUB</a>', head='<base href="https://files.audit/library/">')
    observe(request, evaluate(initial), (1,), ['I1', 'I8'], resource=expected)


@pytest.mark.parametrize('long_card', [False, True])
def test_11_short_structured_identity_is_sufficient(request, long_card):
    book = Book('short', 'Alice Example', 'North')
    initial = html('<main><h1>North</h1>' + (DETAILS if long_card else '') + FILE + '</main>', ld(schema(book)))
    observe(request, evaluate(initial, book), (1,), ['I1', 'I5', 'I8'])


@pytest.mark.parametrize('number', ['3', '03', 'III'])
def test_12_equivalent_volume_notation(request, number):
    book = Book('volume', 'Alice Example', 'Рубеж. Том 3')
    body = identity(Book('observed', book.author, 'Рубеж. Том ' + number)) + DETAILS + FILE
    observe(request, evaluate(html('<main>' + body + '</main>'), book), (1,), ['I1', 'I8'])


def test_13_different_volume_cannot_confirm(request):
    book = Book('volume', 'Alice Example', 'Рубеж. Том 3')
    observed = Book('other', book.author, 'Рубеж. Том 4')
    observe(request, evaluate(card(FILE, observed), book), (0, 2), ['I1', 'I8'])


@pytest.mark.parametrize('audio', [False, True])
def test_14_work_identity_and_format_follow_documented_policy(request, audio):
    content = '<p>Аудиоверсия книги.</p><audio src="/north.mp3"></audio>' if audio else FILE
    observe(request, evaluate(card(content)), (1,), ['I1', 'I8'], formats=['audio' if audio else 'ebook'])


@pytest.mark.parametrize('reader', [False, True])
def test_15_story_collection_is_an_individual_work(request, reader):
    book = Book('stories', 'Alice Example', 'Сборник рассказов')
    content = '<div class="reader-text">' + BODY * 30 + '</div>' if reader else FILE
    observe(request, evaluate(card(content, book), book), (1,), ['I1', 'I5', 'I8'])


@pytest.mark.parametrize('real_book', [False, True])
def test_16_antibot_words_do_not_override_book_evidence(request, real_book):
    book = Book('phrase', 'Alice Example', 'Access Denied')
    initial = card(FILE, book, '<title>Access Denied</title>') if real_book else html(
        '<h1>Access Denied</h1><p>Verify you are human.</p><input name="captcha">', '<title>Access Denied</title>')
    observe(request, evaluate(initial, book), (1,) if real_book else (2,), ['I5', 'I6'])


@pytest.mark.parametrize('placement', ['primary_conflict', 'nested', 'graph'])
def test_17_jsonld_role_and_graph_edges(request, placement):
    other = schema(Book('other', 'Bob Different', 'South Road'), **{'@id': URL + '#recommendation'})
    if placement == 'primary_conflict':
        data = other
    elif placement == 'nested':
        data = schema(isRelatedTo=other)
    else:
        data = {'@graph': [
            {'@type': 'WebPage', '@id': URL, 'mainEntity': {'@id': URL + '#main'},
             'relatedLink': {'@id': URL + '#recommendation'}},
            schema(**{'@id': URL + '#main', 'mainEntityOfPage': {'@id': URL}}), other]}
    observe(request, evaluate(card(FILE, head=ld(data))), (2,) if placement == 'primary_conflict' else (1,), ['I2', 'I5'])


@pytest.mark.parametrize('own', [False, True])
def test_18_pdf_primary_identity_vs_mention(request, own):
    title, author = (BOOK.title, BOOK.author) if own else ('South Road', 'Bob Different')
    opening = [title, author, 'Recommended: The Northern Wind by Alice Example']
    doc = pdf_document([opening, ['Chapter Two']], {'/Title': title, '/Author': author})
    observe(request, evaluate(doc), (1,) if own else (0, 2), ['I1', 'I4'])


@pytest.mark.parametrize('recommendation_page', [False, True])
def test_19_pdf_metadata_conflict_is_not_reconciled_by_advert_page(request, recommendation_page):
    second = [BOOK.title, BOOK.author, 'Suggested reading from the publisher, not the main work.'] if recommendation_page else ['Chapter Two']
    doc = pdf_document([['South Road', 'Bob Different'], second], {'/Title': BOOK.title, '/Author': BOOK.author})
    observe(request, evaluate(doc), (2,), ['I4', 'I5', 'I6'])


@pytest.mark.parametrize('extension', ['.pdf', ''])
def test_20_pdf_content_type_and_range_do_not_depend_on_suffix(request, extension):
    payload = pdf_bytes([[BOOK.title, BOOK.author], ['Chapter Two']], {'/Title': BOOK.title, '/Author': BOOK.author})

    class Response:
        def __init__(self, ranged):
            self.status_code = 206 if ranged else 200
            self.headers = {'Content-Type': 'application/pdf', 'Content-Length': str(len(payload))}
            if ranged:
                self.headers['Content-Range'] = f'bytes 0-{len(payload)-1}/{len(payload)}'

        def iter_content(self, chunk_size):
            for start in range(0, len(payload), chunk_size):
                yield payload[start:start+chunk_size]

        def close(self):
            pass

    class Session:
        def get(self, url, **kwargs):
            return Response('Range' in kwargs['headers'])

    observe(request, evaluate(fetch_url(URL + extension, session=Session())), (1,), ['I4', 'I8'])


@pytest.mark.parametrize('own', [False, True])
def test_21_pdf_without_metadata_uses_primary_opening(request, own):
    opening = [BOOK.title, BOOK.author] if own else ['South Road', 'Bob Different', 'Recommended: The Northern Wind by Alice Example']
    observe(request, evaluate(pdf_document([opening, ['Chapter Two']], {})), (1,) if own else (0, 2), ['I1', 'I4'])


def dynamic_page(foreign_file=False, url=URL):
    body = '<main><article>' + identity() + DETAILS + '<div id="actions"></div></article>'
    if foreign_file:
        body += '<article>' + identity(Book('other', 'Bob Different', 'South Road'), 'h2') + OTHER_FILE + '</article>'
    body += '</main><script>setTimeout(() => { document.querySelector("#actions").innerHTML = '
    body += json.dumps(FILE) + '; }, 350);</script>'
    return html(body, url=url)


def test_22_real_js_in_plain_actions(request, chromium):
    observe(request, browser_result(chromium, dynamic_page()), (1,), ['I1', 'I7'], attempted=True)


@pytest.mark.parametrize('already_in_browser', [False, True])
def test_23_foreign_static_file_does_not_finish_target_js(request, chromium, already_in_browser):
    initial = dynamic_page(foreign_file=True)
    blocked = FetchResult(URL, URL, 403, error_code='http_403') if already_in_browser else None
    observe(request, browser_result(chromium, initial, http=blocked), (1,), ['I2', 'I7'], attempted=True)


@pytest.mark.parametrize('script', [False, True])
def test_24_ordinary_js_uncertainty_is_not_called_a_bug(request, chromium, script):
    content = '<script>document.documentElement.dataset.theme = "light";</script>' if script else ''
    observe(request, browser_result(chromium, card(content)), (0, 2) if script else (0,), ['I6', 'I7'])


def test_25_browser_context_error_does_not_abort_next_url(request, chromium, monkeypatch):
    original = chromium.browser

    class FailOnce:
        calls = 0

        def new_context(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError('Synthetic context failure')
            return original.new_context(**kwargs)

    monkeypatch.setattr(chromium, 'browser', FailOnce())
    for index, expected in enumerate((2, 1)):
        doc = card(FILE)
        doc.url = doc.final_url = URL + '/batch-' + str(index)
        blocked = FetchResult(doc.url, doc.url, 403, error_code='http_403')
        observe(request, browser_result(chromium, doc, http=blocked), (expected,), ['I6', 'I7'], attempted=True)


def test_26_html_mime_can_require_js_even_at_pdf_suffix(request):
    initial = dynamic_page(url=URL + '.pdf')

    class Browser:
        version = 'synthetic-audit'

        def check(self, url):
            doc = card(FILE)
            doc.url = doc.final_url = url
            return doc

    result = analyze_url({'url': initial.url, 'expected_book_id': BOOK.id}, [BOOK],
                         fetcher=lambda url: initial, browser=Browser())
    observe(request, result, (1,), ['I4', 'I7', 'I8'], attempted=True)


@pytest.mark.parametrize('book_metadata', [False, True])
def test_27_metadata_author_role_is_not_interchangeable(request, book_metadata):
    head = '<meta property="book:author" content="Alice Example">' if book_metadata else (
        '<meta property="og:type" content="article"><meta name="author" content="Alice Example">')
    body = identity(author=False) + DETAILS + FILE
    if not book_metadata:
        body += '<div class="posted-by">Article writer: Alice Example. Book author is not stated.</div>'
    observe(request, evaluate(html('<main>' + body + '</main>', head)), (1,) if book_metadata else (0, 2), ['I2', 'I3', 'I5'])


@pytest.mark.parametrize('login_widget', [False, True])
def test_28_independent_login_widget_does_not_cancel_public_book(request, login_widget):
    content = FILE
    if login_widget:
        content += '<section><h2>User account</h2><form><input type="password"><p>Optional member login.</p></form></section>'
    observe(request, evaluate(card(content)), (1,), ['I1', 'I5'])
