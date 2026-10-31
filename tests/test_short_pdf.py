"""Short PDF text is insufficient for a confident negative, not proof of placement."""

from io import BytesIO

from pypdf import PdfReader, PdfWriter
import pytest
import requests

from pages import FetchResult
from part2 import Book, evaluate_page
from test_pdf_roles import AUTHOR, BODY, TITLE, document, evaluate


@pytest.fixture(autouse=True)
def no_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Live HTTP forbidden")
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


def pdf(first_chars=666, pages=1, label="Chapter 1", metadata=None, blank=False):
    writer = PdfWriter()
    writer.add_metadata(metadata if metadata is not None else {"/Title": TITLE, "/Author": AUTHOR})
    if blank:
        writer.add_blank_page(width=612, height=792)
    else:
        base = PdfReader(BytesIO(document([label], body="").body)).pages[0].extract_text()
        prose = ((BODY + " ") * 100)[:first_chars - len(base) - 1]
        source = PdfReader(BytesIO(document([label, prose], body="").body))
        writer.add_page(source.pages[0])
    if pages == 2:
        writer.add_blank_page(width=612, height=792)
    output = BytesIO()
    writer.write(output)
    return FetchResult("https://offline.test/short.pdf", "https://offline.test/short.pdf", 200,
                       {"Content-Type": "application/pdf"}, output.getvalue())


@pytest.mark.parametrize(("first_chars", "pages", "status"), [
    (666, 1, 2), (1544, 1, 2), (666, 2, 2), (1544, 2, 1), (4000, 1, 2), (4000, 2, 1),
])
def test_matched_work_below_positive_threshold_is_uncertain(first_chars, pages, status):
    result = evaluate(pdf(first_chars, pages))
    assert result["extracted"]["text_chars"] == first_chars + pages - 1
    assert result["extracted"]["pdf_pages_read"] == pages
    assert result["match"]["state"] == "matched"
    assert result["status"] == status
    assert result["reason_code"] == ("placement_signs" if status == 1 else "content_not_confirmed")
    assert bool(result["content_signals"]) == (status == 1)


@pytest.mark.parametrize("label", [
    "Book review. A critical analysis of the novel.",
    "Critical review of The Northern Wind", "Study guide for The Northern Wind",
], ids=["review", "critical-review", "study-guide"])
@pytest.mark.parametrize(("first_chars", "pages"), [(666, 1), (1544, 1), (666, 2), (1544, 2)])
def test_short_and_long_secondary_documents_stay_uncertain(label, first_chars, pages):
    result = evaluate(pdf(first_chars, pages, label=label))
    assert (result["status"], result["reason_code"]) == (2, "pdf_document_role_unresolved")
    assert not result["content_signals"]
    assert result["extracted"]["metadata"]["document_role_evidence"] == label


@pytest.mark.parametrize(("first_chars", "pages"), [(666, 1), (1544, 1), (666, 2)])
def test_annotation_does_not_become_placement(first_chars, pages):
    result = evaluate(pdf(first_chars, pages, label="Synopsis: The Northern Wind"))
    assert (result["status"], result["reason_code"]) == (2, "content_not_confirmed")
    assert not result["content_signals"]


@pytest.mark.parametrize("pages", [1, 2])
def test_pdf_without_text_stays_uncertain(pages):
    result = evaluate(pdf(pages=pages, blank=True))
    assert (result["status"], result["reason_code"]) == (2, "pdf_no_text")
    assert not result["content_signals"]


@pytest.mark.parametrize(("first_chars", "pages"), [(666, 1), (1544, 2)])
def test_conflicting_metadata_stays_uncertain(first_chars, pages):
    result = evaluate(pdf(first_chars, pages, metadata={"/Title": "South Road", "/Author": "Bob Different"}))
    assert (result["status"], result["reason_code"]) == (2, "pdf_identity_unresolved")
    assert not result["content_signals"]


def test_confirmed_different_pdf_keeps_a_negative_result():
    result = evaluate_page(pdf(), [Book("other", "Bob Different", "South Road")], "other")
    assert (result["status"], result["reason_code"]) == (0, "different_pdf_book")


@pytest.mark.parametrize(("first_chars", "pages", "status"), [(666, 1, 2), (1544, 2, 1)])
def test_identity_from_opening_obeys_the_same_threshold_rule(first_chars, pages, status):
    result = evaluate(pdf(first_chars, pages, metadata={}))
    assert result["match"]["state"] == "matched"
    assert result["status"] == status
