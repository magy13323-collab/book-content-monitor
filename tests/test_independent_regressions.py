"""Independent no-network regression probes for Book Content Monitor.
Run after placing in project/tests as test_independent_regressions.py:
    python -m pytest -q tests/test_independent_regressions.py

All test data is synthetic. No live network calls.
"""
from io import BytesIO
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from pages import FetchResult
from part2 import Book, evaluate_page

BOOK = Book('target', 'Alice Example', 'The Northern Wind', 'ebook')


def synthetic_pdf(meta_title, meta_author, real_title, real_author):
    writer = PdfWriter()
    writer.add_metadata({'/Title': meta_title, '/Author': meta_author})
    for _ in range(2):
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({
            NameObject('/Type'): NameObject('/Font'),
            NameObject('/Subtype'): NameObject('/Type1'),
            NameObject('/BaseFont'): NameObject('/Helvetica'),
        })
        page[NameObject('/Resources')] = DictionaryObject({
            NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})
        })
        ps = f'BT /F1 12 Tf 40 740 Td ({real_title} - {real_author}) Tj '
        ps += ' '.join(
            '0 -16 Td (An unrelated story about a different traveler at sea.) Tj'
            for _ in range(36)
        )
        stream = DecodedStreamObject()
        stream.set_data((ps + ' ET').encode('ascii'))
        page[NameObject('/Contents')] = writer._add_object(stream)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def test_pdf_metadata_must_not_automatically_override_foreign_body():
    data = synthetic_pdf('The Northern Wind', 'Alice Example', 'South Road', 'Bob Different')
    document = FetchResult('https://example.org/fake.pdf', 'https://example.org/fake.pdf',
                           200, {'Content-Type': 'application/pdf'}, data)
    result = evaluate_page(document, [BOOK], BOOK.id)
    assert result['status'] != 1, ('False positive: PDF metadata name target book, '
                                   'but extracted body identifies another work')


def test_generic_download_inside_unidentified_bonus_section_is_not_proof():
    html = '''<html><main><h1>The Northern Wind</h1>
    <p class="book-author">Alice Example</p>
    <section class="bonus-offer"><p>Bonus item: South Road by Bob Different.</p>
    <a href="/files/south-road.epub">Download EPUB</a></section>
    </main></html>'''
    document = FetchResult('https://example.org/target', 'https://example.org/target',
                           200, {'Content-Type': 'text/html'}, html.encode('utf-8'))
    result = evaluate_page(document, [BOOK], BOOK.id)
    assert result['status'] != 1, ('False positive: generic link belongs to '
                                   'a separate bonus section, not the target book')


def test_plain_file_link_belongs_to_unambiguous_card():
    html = '''<main><h1>The Northern Wind</h1>
    <p class="book-author">Alice Example</p>
    <a href="/files/north-wind.epub">Download EPUB</a>
    </main>'''
    document = FetchResult('https://example.org/target', 'https://example.org/target',
                           200, {'Content-Type': 'text/html'}, html.encode('utf-8'))
    result = evaluate_page(document, [BOOK], BOOK.id)
    assert result['status'] == 1, 'A clearly scoped real book file should remain positive'
