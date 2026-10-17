"""Book bylines and publication authors have distinct roles."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pages import FetchResult
from part2 import Book, evaluate_page


@pytest.mark.parametrize('own', [False, True])
def test_article_author_does_not_override_book_byline(own):
    book = Book('north', 'Alice Example', 'The Northern Wind')
    author = book.author if own else 'Bob Different'
    editor = 'Bob Different' if own else book.author
    html = ('<html><head><meta charset="utf-8"></head><body><main>'
            '<article itemscope itemtype="https://schema.org/BlogPosting">'
            f'<h1>{book.title}</h1><header><span itemprop="author">{editor}</span>'
            '<time>2026-10-16</time></header><div>'
            f'<b>{book.title}</b><p>Author: {author}</p>'
            '<a href="/north.epub">Download EPUB</a></div></article></main></body></html>')
    doc = FetchResult('https://offline.roles/book', 'https://offline.roles/book', 200,
                      {'content-type': 'text/html; charset=utf-8'}, html.encode())
    result = evaluate_page(doc, [book], book.id)
    assert result['status'] == 1 if own else result['status'] != 1
    assert result['extracted']['authors'] == [author]


@pytest.mark.parametrize('region', ['', 'review', 'rating'], ids=['no-review', 'review', 'rating'])
@pytest.mark.parametrize('own', [False, True], ids=['different-author', 'own-author'])
def test_reader_review_author_does_not_replace_book_author(region, own):
    book = Book('north', 'Alice Example', 'The Northern Wind')
    author = book.author if own else 'Bob Different'
    reader = 'Bob Different' if own else book.author
    review = (f'<div class="{region}"><p class="book-author">{reader}</p>'
              f'<p>A reader shares an opinion about {book.title}.</p></div>') if region else ''
    html = ('<html><head><meta charset="utf-8"></head><body><main>'
            f'<h1>{book.title}</h1><p class="book-author">{author}</p>' + review +
            '<a href="/north.epub">Download EPUB</a></main></body></html>')
    doc = FetchResult('https://offline.roles/book', 'https://offline.roles/book', 200,
                      {'content-type': 'text/html; charset=utf-8'}, html.encode())
    result = evaluate_page(doc, [book], book.id)
    assert result['status'] == 1 if own else result['status'] != 1
    assert result['extracted']['authors'] == [author]
    assert result['match']['state'] == ('matched' if own else 'author_conflict')
