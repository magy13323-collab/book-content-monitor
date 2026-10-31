"""Incomplete PDF previews cannot prove absence; all HTTP ranges are synthetic."""

from io import BytesIO
import json
import re

import openpyxl
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject
import pytest
import requests

from pages import FetchResult, Limits, extract_page, fetch_url
from part2 import Book, analyze_url, classify, evaluate_page, match_book, write_results
from test_part2 import FakeResponse, synthetic_pdf
from test_short_pdf import pdf as short_pdf


OWN = Book("north", "Alice Example", "The Northern Wind")
OTHER = Book("south", "Bob Different", "South Road")
URL = "https://offline.test/book.pdf"


@pytest.fixture(autouse=True)
def no_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Live HTTP forbidden")
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


def pdf(pages=21, own=False, target_at_end=False, metadata=None, note="", padding=False):
    first = OWN if own else OTHER
    initial = PdfReader(BytesIO(synthetic_pdf(first.title, first.author, note=note)))
    target = PdfReader(BytesIO(synthetic_pdf()))
    writer = PdfWriter()
    writer.add_metadata(metadata if metadata is not None else {"/Title": first.title, "/Author": first.author})
    for index in range(pages):
        reader = target if target_at_end and index == pages - 1 else initial
        writer.add_page(reader.pages[0])
    if padding:
        # Separate the trailer from the opening objects to require real sparse reads.
        stream = DecodedStreamObject()
        stream.set_data(b"x" * 300000)
        writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def local(body):
    return FetchResult(URL, URL, 200, {"Content-Type": "application/pdf"}, body)


class RangeResponse(FakeResponse):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class RangeSession:
    def __init__(self, body, reject_followup=False):
        self.body = body
        self.calls = []
        self.reject_followup = reject_followup

    def get(self, url, **kwargs):
        assert url == URL
        assert kwargs["allow_redirects"] is False
        found = re.fullmatch(r"bytes=(\d+)-(\d+)", kwargs["headers"]["Range"])
        assert found
        start, end = int(found[1]), min(int(found[2]), len(self.body) - 1)
        self.calls.append((start, end))
        if self.reject_followup and len(self.calls) > 1:
            return RangeResponse(200, b"range unavailable")
        data = self.body[start:end + 1]
        return RangeResponse(206, data, {"Content-Type": "application/pdf", "Content-Length": str(len(data)),
                                        "Content-Range": f"bytes {start}-{end}/{len(self.body)}"})


def network(body, limits=Limits(), reject_followup=False):
    session = RangeSession(body, reject_followup)
    fetched = fetch_url(URL, session=session, limits=limits)
    assert not fetched.error_code
    assert fetched.pdf_stream is not None
    return fetched, session


@pytest.mark.parametrize("mode,read", [("local", 20), ("range", 3)])
def test_unread_target_on_page_21_prevents_a_negative(mode, read):
    body = pdf(target_at_end=True, padding=mode == "range")
    assert OWN.title in PdfReader(BytesIO(body)).pages[20].extract_text()
    initial = local(body) if mode == "local" else network(body)[0]
    result = evaluate_page(initial, [OWN], OWN.id)
    observed = result["extracted"]
    assert observed["pdf_page_count"] == 21
    assert observed["pdf_pages_read"] == read
    assert observed["partial"] is True
    assert OWN.title not in observed["text_excerpt"]
    assert observed["metadata"]["/Title"] == OTHER.title
    assert (result["status"], result["reason_code"]) == (2, "partial_document")
    assert any(f"{read}/21" in item for item in result["evidence"])
    assert any(OTHER.title in item for item in result["evidence"])
    assert any(OTHER.author in item for item in result["evidence"])
    if mode == "range":
        assert int(observed["metadata"]["preview_bytes"]) < len(body)
        assert observed["metadata"]["preview_limit_reached"] == "false"
        assert "preview_error" not in observed["metadata"]


@pytest.mark.parametrize("mode", ["local", "range"])
def test_fully_read_other_book_remains_negative(mode):
    body = pdf(pages=2, padding=mode == "range")
    initial = local(body) if mode == "local" else network(body)[0]
    result = evaluate_page(initial, [OWN], OWN.id)
    assert result["extracted"]["partial"] is False
    assert result["extracted"]["pdf_pages_read"] == 2
    assert (result["status"], result["reason_code"]) == (0, "different_pdf_book")


@pytest.mark.parametrize("mode,read", [("local", 20), ("range", 3)])
def test_partial_own_book_with_positive_evidence_still_confirms(mode, read):
    body = pdf(own=True, padding=mode == "range")
    initial = local(body) if mode == "local" else network(body)[0]
    result = evaluate_page(initial, [OWN], OWN.id)
    assert result["extracted"]["partial"] is True
    assert result["extracted"]["pdf_pages_read"] == read
    assert (result["status"], result["reason_code"]) == (1, "placement_signs")
    assert result["content_signals"][0]["kind"] == "pdf_text"


@pytest.mark.parametrize("mode", ["local", "range"])
def test_partial_metadata_conflict_keeps_its_specific_reason(mode):
    body = pdf(metadata={"/Title": OWN.title, "/Author": OWN.author}, padding=mode == "range")
    initial = local(body) if mode == "local" else network(body)[0]
    result = evaluate_page(initial, [OWN], OWN.id)
    assert result["extracted"]["partial"] is True
    assert (result["status"], result["reason_code"]) == (2, "pdf_identity_unresolved")
    assert not result["content_signals"]
    assert any(OTHER.title in item for item in result["evidence"])


@pytest.mark.parametrize("mode", ["local", "range"])
@pytest.mark.parametrize("note", ["Book review of The Northern Wind", "Study guide for The Northern Wind"])
def test_partial_secondary_pdf_keeps_role_protection(mode, note):
    body = pdf(own=True, note=note, padding=mode == "range")
    initial = local(body) if mode == "local" else network(body)[0]
    result = evaluate_page(initial, [OWN], OWN.id)
    assert result["extracted"]["partial"] is True
    assert (result["status"], result["reason_code"]) == (2, "pdf_document_role_unresolved")
    assert not result["content_signals"]
    assert any(note in item for item in result["evidence"])


@pytest.mark.parametrize("limit", [Limits(pdf_max_pages=1), Limits(max_text_chars=2000)])
def test_page_or_text_limit_cannot_support_a_negative(limit):
    initial = local(pdf(pages=2))
    observed = extract_page(initial, limit)
    assert observed.partial is True
    match = match_book(observed, [OWN], OWN.id)
    status, reason, _ = classify(initial, observed, match, [OWN])
    assert (status, reason) == (2, "partial_document")


def test_short_own_pdf_keeps_uncertainty_without_claiming_incomplete_read():
    result = evaluate_page(short_pdf(), [OWN], OWN.id)
    assert result["extracted"]["partial"] is False
    assert (result["status"], result["reason_code"]) == (2, "content_not_confirmed")


@pytest.mark.parametrize("failure", ["byte-budget", "range-rejected"])
def test_failed_sparse_read_keeps_technical_reason(failure):
    body = pdf(target_at_end=True, padding=True)
    limit = Limits(pdf_preview_bytes=65536) if failure == "byte-budget" else Limits()
    initial, session = network(body, limits=limit, reject_followup=failure == "range-rejected")
    result = evaluate_page(initial, [OWN], OWN.id)
    assert result["extracted"]["partial"] is True
    assert (result["status"], result["reason_code"]) == (2, "pdf_preview_incomplete")
    metadata = result["extracted"]["metadata"]
    assert "PDFPreviewLimit" in metadata["preview_error"]
    assert metadata["preview_limit_reached"] == ("true" if failure == "byte-budget" else "false")
    assert int(metadata["preview_bytes"]) < len(body)
    assert session.calls[0] == (0, 65535)


@pytest.mark.parametrize("mode", ["local", "range"])
@pytest.mark.parametrize("own", [False, True], ids=["unread-target", "positive-preview"])
def test_export_keeps_partial_evidence_and_decisions(tmp_path, mode, own):
    body = pdf(own=own, target_at_end=not own, padding=mode == "range")
    target = {"url": URL, "expected_book_id": OWN.id, "source_row": 7,
              "expected_title": OWN.title, "expected_author": OWN.author}
    fetcher = lambda url: local(body) if mode == "local" else network(body)[0]
    result = analyze_url(target, [OWN], fetcher=fetcher)
    status, reason = (1, "placement_signs") if own else (2, "partial_document")
    write_results([result], tmp_path / "export")
    exported = json.loads((tmp_path / "export/part2_results.jsonl").read_text(encoding="utf-8"))
    assert (exported["status"], exported["reason_code"]) == (status, reason)
    assert exported["extracted"]["partial"] is True
    assert exported["expected_book_id"] == OWN.id
    assert exported["source_row"] == 7 and exported["evidence"]
    workbook = openpyxl.load_workbook(tmp_path / "export/part2_results.xlsx", read_only=True)
    headers, row = list(workbook.active.values)
    values = dict(zip(headers, row))
    assert (values["status"], values["reason_code"]) == (status, reason)
    assert values["partial"] is True and values["pdf_page_count"] == 21
    assert values["pdf_pages_read"] == (20 if mode == "local" else 3)
    assert values["evidence"]
    workbook.close()
