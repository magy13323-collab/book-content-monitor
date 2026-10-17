"""The checked-in demo uses only synthetic local responses."""
import json

import pytest

from examples.run_demo import run


@pytest.mark.parametrize("mode", ["off", "auto"])
def test_offline_example_reproduces_statuses(tmp_path, mode):
    if mode == "auto":
        pytest.importorskip("playwright.sync_api")
        from browser import BrowserProbe
        with BrowserProbe(fixture_routes={}) as probe:
            if not probe.start():
                pytest.skip(probe.failure)
    run(tmp_path, mode)
    records = [json.loads(line) for line in (tmp_path / "part2_results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["status"] for r in records] == [1, 0, 2, 1, 0, 2, 1 if mode == "auto" else 2]
    assert (tmp_path / "part1_processed.xlsx").is_file()
    assert (tmp_path / "part2_results.xlsx").is_file()
    assert all(r["url"].startswith("https://library.example/") for r in records)
    assert records[-1]["browser_attempt"]["attempted"] == (mode == "auto")
