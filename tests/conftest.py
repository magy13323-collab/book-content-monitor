"""Offline public tests: deny HTTP unless a test explicitly supplies its own transport."""
import pytest
import requests

@pytest.fixture(autouse=True)
def forbid_unmocked_http(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Unmocked HTTP is forbidden in the public offline test suite")
    monkeypatch.setattr(requests.sessions.Session, "request", denied)
