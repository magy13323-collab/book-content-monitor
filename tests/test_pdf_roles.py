"""Document-role regressions using real, entirely synthetic PDF bytes."""

from io import BytesIO

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject
import pytest
import requests

from pages import FetchResult
from part2 import Book, evaluate_page
from test_part2 import synthetic_pdf


TITLE, AUTHOR = "The Northern Wind", "Alice Example"
BOOK = Book("north", AUTHOR, TITLE)
BODY = "The traveler walked north and reached the bridge before sunset."


@pytest.fixture(autouse=True)
def no_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Live HTTP forbidden")
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


def document(lines=(), second_lines=(), metadata=None, body=BODY):
    source = PdfReader(BytesIO(synthetic_pdf()))
    writer = PdfWriter()
    writer.add_metadata(metadata if metadata is not None else {"/Title": TITLE, "/Author": AUTHOR})
    for index, extra in enumerate((lines, second_lines)):
        page = writer.add_page(source.pages[index])
        text = [TITLE, AUTHOR, *extra, *([body] * 25 if body else [])]
        escaped = [line.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)') for line in text]
        stream = DecodedStreamObject()
        stream.set_data(('BT /F1 12 Tf 40 740 Td ' + ' '.join(
            f'({line}) Tj 0 -16 Td' for line in escaped) + ' ET').encode('ascii'))
        page[NameObject('/Contents')] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return FetchResult("https://offline.test/book.pdf", "https://offline.test/book.pdf", 200,
                       {"Content-Type": "application/pdf"}, output.getvalue())


def evaluate(pdf):
    return evaluate_page(pdf, [BOOK], BOOK.id)


def assert_secondary(result, role):
    assert (result["status"], result["reason_code"]) == (2, "pdf_document_role_unresolved")
    assert not result["content_signals"]
    extracted = result["extracted"]
    assert extracted["pdf_pages_read"] == 2
    assert extracted["text_chars"] > 1200
    assert extracted["metadata"]["/Title"] == TITLE
    assert extracted["metadata"]["/Author"] == AUTHOR
    assert extracted["metadata"]["document_role"] == role
    marker = extracted["metadata"]["document_role_evidence"]
    assert marker and any(marker in item for item in result["evidence"])
    assert any(signal["kind"] == "pdf_text" and signal["binding"] == "unresolved"
               and signal["binding_reason"]
               for signal in extracted["observed_content_signals"])


def test_original_work_keeps_placement_signs():
    result = evaluate(document(["Chapter 1"]))
    assert (result["status"], result["reason_code"]) == (1, "placement_signs")
    assert [signal["kind"] for signal in result["content_signals"]] == ["pdf_text"]


@pytest.mark.parametrize(("label", "role"), [
    ("Book review. A critical analysis of the novel.", "review"),
    ("Critical review of The Northern Wind", "review"),
    ("A critical study of The Northern Wind", "review"),
    ("Study guide for The Northern Wind", "study_guide"),
    ("Reading guide: The Northern Wind", "study_guide"),
    ("This is a critical review of The Northern Wind.", "review"),
    ("This study guide discusses the novel and its themes.", "study_guide"),
    ("A study-guide for The Northern Wind", "study_guide"),
], ids=["review", "critical-review", "critical-study", "study-guide", "reading-guide",
        "self-description", "this-guide", "hyphenated-guide"])
def test_explicit_secondary_document_is_not_the_work(label, role):
    assert_secondary(evaluate(document([label], body="This analysis discusses the novel and its themes.")), role)


def test_critical_review_after_eight_lines_and_opening_excerpt_is_detected():
    notes = ["Publication note: edition information and bibliographic details for this document."] * 13
    pdf = document([*notes, "Critical review of The Northern Wind"])
    extracted = PdfReader(BytesIO(pdf.body)).pages[0].extract_text()
    assert extracted.index("Critical review") > 1000
    assert len(extracted.split("Critical review")[0].splitlines()) > 8
    assert_secondary(evaluate(pdf), "review")


def test_role_on_second_page_is_detected():
    assert_secondary(evaluate(document(second_lines=["Study guide for The Northern Wind"])), "study_guide")


@pytest.mark.parametrize("mention", [
    "He remembered a review of the old book and returned to his journey.",
    "Book review. The newspaper lay on his desk beside the unfinished letter.",
    "Study guide",
    "Themes and analysis",
])
def test_review_or_guide_mentions_inside_a_chapter_do_not_cancel_the_work(mention):
    result = evaluate(document(["Chapter 1", mention], second_lines=["Discussion questions"]))
    assert (result["status"], result["reason_code"]) == (1, "placement_signs")


def test_subject_can_identify_a_guide_despite_a_chapter_heading():
    metadata = {"/Title": TITLE, "/Author": AUTHOR, "/Subject": "Study guide for The Northern Wind"}
    assert_secondary(evaluate(document(["Chapter 1"], metadata=metadata)), "study_guide")


def test_document_level_statement_after_a_chapter_is_not_a_prose_mention():
    assert_secondary(evaluate(document(["Chapter 1", "This document is a study guide for The Northern Wind."])),
                     "study_guide")


def test_companion_structure_without_an_explicit_role_remains_uncertain():
    assert_secondary(evaluate(document(["Themes and analysis", "Discussion questions"],
                                       body="Consider the author's style and discuss the main character's choices.")),
                     "unresolved")


def test_one_ambiguous_heading_does_not_establish_a_document_role():
    result = evaluate(document(["Discussion questions"]))
    assert result["status"] == 1


def test_mismatching_metadata_is_still_uncertain():
    result = evaluate(document(metadata={"/Title": "South Road", "/Author": "Bob Different"}))
    assert (result["status"], result["reason_code"]) == (2, "pdf_identity_unresolved")
    assert not result["content_signals"]


def test_insufficient_extracted_text_does_not_confirm_a_work():
    result = evaluate(document(body=""))
    assert (result["status"], result["reason_code"]) == (2, "pdf_no_text")


def test_missing_metadata_does_not_restore_a_rejected_pdf_signal():
    result = evaluate(document(["Study guide for The Northern Wind"], metadata={}))
    assert (result["status"], result["reason_code"]) == (2, "pdf_document_role_unresolved")
    assert not result["content_signals"]
