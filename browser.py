"""Optional standard Chromium rendering. No profiles, CAPTCHA solving or book downloads."""

from pathlib import Path
import re
import sys
import time

from pages import FetchResult, Limits, extract_page


class BrowserProbe:
    def __init__(self, timeout_ms=25000, channel="auto", executable=None, fixture_routes=None):
        self.timeout_ms = timeout_ms
        self.channel = channel
        self.executable = executable
        self.fixture_routes = fixture_routes  # Tests fulfill every request; never an external fetch.
        self.runtime = None
        self.browser = None
        self.failure = ""
        self.version = ""

    def start(self):
        if self.browser:
            return True
        if self.failure:
            return False
        try:
            from playwright.sync_api import sync_playwright
            self.runtime = sync_playwright().start()
            options = {"headless": True}
            if self.executable:
                options["executable_path"] = str(self.executable)
            elif self.channel != "auto":
                options["channel"] = self.channel
            elif Path(self.runtime.chromium.executable_path).is_file():
                options["channel"] = "chromium"
            elif sys.platform == "win32":
                options["channel"] = "msedge"
            self.browser = self.runtime.chromium.launch(**options)
            self.version = self.browser.version
            return True
        except ImportError:
            self.failure = "Не установлен необязательный пакет playwright."
        except Exception as error:
            # Native launch errors often contain machine-specific paths; do not export those.
            self.failure = f"Не удалось запустить локальный Chromium/Edge ({type(error).__name__})."
        self.close()
        return False

    def check(self, url, ready=None, html_document=False):
        if not self.start():
            return FetchResult(url, url, error_code="browser_unavailable", error_detail=self.failure)
        result = FetchResult(url, url)
        started = time.monotonic()
        deadline = started + self.timeout_ms / 1000
        context = None
        document_count = 0
        policy_error = ""
        last_page = None

        def route_request(route):
            nonlocal document_count, policy_error
            request = route.request
            if request.is_navigation_request() and request.frame == page.main_frame:
                document_count += 1
                if document_count > Limits().max_redirects + 1:
                    policy_error = "browser_redirect_limit"
                    route.abort()
                    return
            is_file = re.search(r"\.(pdf|epub|fb2|mobi|azw3|djvu|zip|torrent|mp3|m4a|ogg|mp4|wav)(?:$|[?#])",
                                request.url, re.I)
            is_download = re.search(r"/(?:download|downloads)(?:/|\?)|[?&](?:do|action)=download", request.url, re.I)
            known_html = (html_document and request.url == url and request.is_navigation_request()
                          and request.frame == page.main_frame)
            if request.resource_type == "media" or ((is_file or is_download) and not known_html):
                route.abort()
            elif self.fixture_routes is not None:
                fixture = self.fixture_routes.get(request.url)
                if fixture is None:
                    route.abort()
                else:
                    status, html = fixture
                    route.fulfill(status=status, content_type="text/html; charset=utf-8", body=html)
            else:
                route.continue_()

        def response_received(response):
            if response.request.is_navigation_request() and response.frame == page.main_frame:
                if result.http_status in {301, 302, 303, 307, 308} and getattr(response.request, "redirected_from", None) is not None:
                    result.redirects.append({"status": result.http_status,
                                             "from": result.final_url, "to": response.url})
                result.http_status = response.status
                result.final_url = response.url
                result.headers = {"content-type": response.headers.get("content-type", "text/html")}

        try:
            context = self.browser.new_context(accept_downloads=False, service_workers="block")
            page = context.new_page()
            context.route("**/*", route_request)
            page.on("response", response_received)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=min(self.timeout_ms, 12000))
            except Exception:
                pass  # A navigation timeout may still leave useful DOM; bounded observation continues.
            while time.monotonic() < deadline:
                if policy_error:
                    result.error_code = policy_error
                    break
                try:
                    # Keep the live DOM untouched. Mark hidden nodes in a detached snapshot,
                    # using Chromium's computed CSS rather than guessing class names in HTTP.
                    html = page.evaluate("""() => {
                        const clone = document.documentElement.cloneNode(true);
                        const live = [...document.querySelectorAll('*')];
                        const copies = [clone, ...clone.querySelectorAll('*')];
                        live.forEach((node, i) => {
                            const style = getComputedStyle(node);
                            if (style.display === 'none' || style.visibility === 'hidden' ||
                                style.visibility === 'collapse' || style.contentVisibility === 'hidden' ||
                                style.opacity === '0') copies[i].setAttribute('hidden', '');
                        });
                        return clone.outerHTML;
                    }""").encode("utf-8")
                    result.final_url = page.url
                    if len(html) > Limits().max_bytes:
                        result.error_code = "browser_response_too_large"
                        break
                    result.body = html
                    result.size_bytes = len(html)
                    result.headers = {"content-type": "text/html; charset=utf-8"}
                    result.visibility_checked = True
                    parsed = extract_page(result)
                    last_page = parsed
                    # An actual CAPTCHA form is terminal. Normal site JS may finish a loading shell.
                    captcha = page.locator('input[name="captcha"], .g-recaptcha, .h-captcha').count()
                    if captcha and ("access_challenge" in parsed.issues or len(parsed.text) < 500):
                        result.error_code = "browser_captcha"
                        break
                    confirmed = any(subject["authors"] and subject["signals"] for subject in parsed.subjects)
                    if "login_required" in parsed.issues and not confirmed:
                        result.error_code = "browser_login_required"
                        break
                    pending = set(parsed.issues)
                    if confirmed:
                        pending -= {"insufficient_html", "access_challenge", "login_required"}
                    if ready is not None:
                        pending.discard("content_requires_js")
                    if parsed.kind == "html" and not pending & {
                        "loading_shell", "insufficient_html", "access_challenge", "content_requires_js",
                        "extraction_error", "not_loaded",
                        "service_error",
                    }:
                        identity = parsed.authors or any(subject.get("authors") for subject in parsed.subjects)
                        if (identity or parsed.signals) and (ready is None or ready(parsed)):
                            return result
                except Exception:
                    pass  # DOM may be between two automatic navigations.
                page.wait_for_timeout(200)
            result.error_code = result.error_code or "browser_not_ready"
            result.error_detail = "Содержательный HTML не получен в пределах таймаута; обход защиты не выполнялся."
            if last_page is not None:
                if "service_error" in last_page.issues:
                    result.error_detail = f"После ожидания сайт возвращает служебную ошибку HTTP {result.http_status}."
                elif "loading_shell" in last_page.issues:
                    result.error_detail = "После ожидания страница осталась загрузочной оболочкой."
                elif last_page.signals:
                    result.error_detail = "HTML получен, но автоматическая привязка признаков к произведению не подтверждена."
            return result
        except Exception as error:
            result.error_code = "browser_error"
            result.error_detail = f"Ошибка браузерной проверки: {type(error).__name__}."
            return result
        finally:
            result.elapsed_seconds = round(time.monotonic() - started, 3)
            if context is not None:
                try:
                    context.close()
                except Exception as error:
                    result.error_code = result.error_code or "browser_error"
                    result.error_detail += f" Ошибка закрытия браузерного контекста: {type(error).__name__}."

    def close(self):
        if self.browser:
            self.browser.close()
            self.browser = None
        if self.runtime:
            self.runtime.stop()
            self.runtime = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
