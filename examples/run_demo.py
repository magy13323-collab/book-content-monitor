"""Offline example: python examples/run_demo.py [--browser auto].

All library.example responses come from local fixtures. HTTP requests are denied;
Chromium, when requested, fulfills every request through its fixture routes.
Outputs are produced by the unchanged part1/part2 CLI entry points.
"""
import argparse
from pathlib import Path
import sys
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import browser
from pages import FetchResult
import part1
import part2


def run(output_dir, browser_mode="off"):
    output_dir = Path(output_dir)
    fixtures = Path(__file__).parent / "fixtures"
    routes = {"https://library.example/" + p.name: (200, p.read_text(encoding="utf-8"))
              for p in fixtures.glob("*.html")}

    def fetch_fixture(url, *args, **kwargs):
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc != "library.example":
            raise AssertionError("Demo URL is not a local fixture")
        path = fixtures / parts.path.lstrip("/")
        if path.parent != fixtures or not path.is_file():
            raise AssertionError("Unknown demo fixture")
        content_type = "application/pdf" if path.suffix == ".pdf" else "text/html; charset=utf-8"
        return FetchResult(url, url, 200, {"content-type": content_type}, path.read_bytes())

    def deny_http(*args, **kwargs):
        raise AssertionError("External HTTP is forbidden in the offline example")

    original_init = browser.BrowserProbe.__init__

    def fixture_init(self, **options):
        original_init(self, fixture_routes=routes, **options)
    source = str(ROOT / "examples/synthetic_input.xlsx")
    with patch("requests.sessions.Session.request", deny_http):
        with patch.object(sys, "argv", ["part1.py", "--input", source,
                                        "--output", str(output_dir / "part1_processed.xlsx")]):
            part1.main()
        with patch.object(part2, "fetch_url", fetch_fixture), patch.object(browser.BrowserProbe, "__init__", fixture_init):
            with patch.object(sys, "argv", ["part2.py", "--input", source,
                                            "--output-dir", str(output_dir), "--delay", "0",
                                            "--browser", browser_mode, "--browser-timeout", "7"]):
                part2.main()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "examples/output")
    parser.add_argument("--browser", choices=("off", "auto"), default="off")
    args = parser.parse_args()
    run(args.output_dir, args.browser)
