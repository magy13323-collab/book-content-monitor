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
