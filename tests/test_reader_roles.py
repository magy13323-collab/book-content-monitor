"""Reader-sized commentary must not become the text of the reviewed book."""

import pytest
import requests

from part2 import evaluate_page
from test_new_rules import CATALOG, document


PROSE = '<p>Путник вышел на северную дорогу и остановился у старого моста.</p>' * 40
REVIEW = ('<p>Автор исследует память и выбор героя. В этом обзоре мы оцениваем '
          'композицию, стиль и убедительность характеров романа.</p>') * 40


@pytest.fixture(autouse=True)
def no_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Live HTTP forbidden")
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


@pytest.mark.parametrize("attributes", ['class="reader-text"', 'itemprop="text"'], ids=["reader", "text"])
@pytest.mark.parametrize("label", [
    "Рецензия на книгу «Учебный маршрут. Том 1».", "Аннотация.", "Критический обзор книги.",
    "Book review.", "Synopsis.", "Critical analysis of the novel.",
], ids=["review-ru", "annotation-ru", "critical-ru", "review-en", "synopsis-en", "critical-en"])
def test_labeled_commentary_is_not_reader_text(attributes, label):
    result = evaluate_page(document(f'<div {attributes}><p>{label}</p>{REVIEW}</div>'),
                           CATALOG, "north")
    assert result["match"]["state"] == "matched"
    assert (result["status"], result["reason_code"]) == (0, "content_not_confirmed")
    assert not result["content_signals"]
    observed = result["extracted"]["observed_content_signals"]
    assert any(signal["kind"] == "reader_text" and signal["binding"] == "other_scope"
               and signal["semantic_role"] == "secondary" and signal["binding_reason"]
               for signal in observed)


@pytest.mark.parametrize("wrapper", [
    '<section class="annotation">{}</section>',
    '<section itemprop="description">{}</section>',
    '<section itemscope itemtype="https://schema.org/Review">{}</section>',
    '<section><h2>Аннотация</h2>{}</section>',
], ids=["annotation", "description", "schema-review", "heading"])
def test_commentary_region_cannot_lend_its_text_to_the_book(wrapper):
    content = wrapper.format(f'<div itemprop="text">{REVIEW}</div>')
    result = evaluate_page(document(content), CATALOG, "north")
    assert (result["status"], result["reason_code"]) == (0, "content_not_confirmed")
    assert not result["content_signals"]


@pytest.mark.parametrize("attributes", ['class="reader-text"', 'itemprop="text"'], ids=["reader", "text"])
def test_long_work_remains_reader_text(attributes):
    result = evaluate_page(document(f'<div {attributes}>{PROSE}</div>'), CATALOG, "north")
    assert (result["status"], result["reason_code"]) == (1, "placement_signs")
    assert [signal["kind"] for signal in result["content_signals"]] == ["reader_text"]


def test_explicit_review_remains_negative():
    result = evaluate_page(document(f'<div class="review" itemprop="text">{REVIEW}</div>'),
                           CATALOG, "north")
    assert (result["status"], result["reason_code"]) == (0, "content_not_confirmed")


@pytest.mark.parametrize("own_content", [
    '<a href="/north.epub">Скачать EPUB</a>',
    f'<div class="reader-text">{PROSE}</div>',
], ids=["file", "reader"])
def test_commentary_does_not_cancel_independent_book_content(own_content):
    content = f'<div itemprop="text"><p>Аннотация.</p>{REVIEW}</div>{own_content}'
    result = evaluate_page(document(content), CATALOG, "north")
    assert (result["status"], result["reason_code"]) == (1, "placement_signs")
    assert len(result["content_signals"]) == 1


def test_review_words_inside_a_chapter_do_not_cancel_the_work():
    content = f'<div class="reader-text"><h2>Глава 1</h2><p>Рецензия на книгу лежала на столе.</p>{PROSE}</div>'
    result = evaluate_page(document(content), CATALOG, "north")
    assert (result["status"], result["reason_code"]) == (1, "placement_signs")
