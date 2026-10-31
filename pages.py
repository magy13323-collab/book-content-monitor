"""Bounded HTTP loading and local HTML/PDF feature extraction (no browser)."""

from dataclasses import dataclass, field
from io import BytesIO, RawIOBase
from itertools import islice
import json
import re
import time
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup
from pypdf import PdfReader, PageObject
from pypdf.errors import PdfReadError
import requests

from part1 import normalize_identity, normalize_text, valid_url


@dataclass(frozen=True)
class Limits:
    connect_timeout: int = 5
    read_timeout: int = 12
    total_seconds: int = 40
    max_bytes: int = 5 * 1024 * 1024
    max_redirects: int = 4
    pdf_max_pages: int = 20
    max_text_chars: int = 200_000
    pdf_preview_bytes: int = 512 * 1024


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    http_status: int | None = None
    headers: dict = field(default_factory=dict)
    body: bytes = b""
    error_code: str = ""
    error_detail: str = ""
    redirects: list = field(default_factory=list)
    size_bytes: int = 0
    elapsed_seconds: float = 0
    pdf_stream: object = None  # Internal bounded range source; never exported.
    visibility_checked: bool = False  # Computed styles observed by the existing browser probe.


class PDFPreviewLimit(ValueError):
    pass


class PDFRangeStream(RawIOBase):
    """Sparse, bounded reads: never fetch the whole large PDF just to identify it."""
    def __init__(self, url, session, initial, content_range, limit, owns_session=False):
        super().__init__()
        found = re.fullmatch(r"bytes 0-(\d+)/(\d+)", content_range)
        if not found or len(initial) != int(found[1]) + 1:
            raise ValueError("Некорректный Content-Range PDF.")
        self.url, self.session = url, session
        self.owns_session = owns_session
        self.size, self.pos, self.total, self.limit = int(found[2]), 0, len(initial), limit
        self.block = 65536
        self.cache = {0: initial}
        self.deadline = time.monotonic() + 40
        self.limit_reached = False

    def seek(self, offset, whence=0):
        self.pos = offset if whence == 0 else self.pos + offset if whence == 1 else self.size + offset
        if self.pos < 0:
            raise ValueError("Отрицательная позиция PDF.")
        return self.pos

    def tell(self):
        return self.pos

    def readable(self):
        return True

    def seekable(self):
        return True

    def read(self, count=-1):
        count = self.size - self.pos if count < 0 else min(count, max(0, self.size - self.pos))
        if count > self.limit:
            self.limit_reached = True
            raise PDFPreviewLimit("Парсер запросил больше бюджета предварительного просмотра PDF.")
        parts = []
        while count > 0:
            if time.monotonic() > self.deadline:
                raise PDFPreviewLimit("Истёк бюджет времени Range-чтения PDF.")
            index, offset = divmod(self.pos, self.block)
            if index not in self.cache:
                start = index * self.block
                expected = min(self.block, self.size - start)
                if self.total + expected > self.limit:
                    self.limit_reached = True
                    raise PDFPreviewLimit("Достигнут лимит Range-чтения PDF; полная книга не скачивается.")
                with self.session.get(self.url, headers={"Range": f"bytes={start}-{start + expected - 1}",
                                                        "Accept-Encoding": "identity"}, stream=True,
                                      allow_redirects=False, timeout=(5, 12)) as response:
                    if response.status_code != 206:
                        raise PDFPreviewLimit("Сервер перестал поддерживать ограниченные Range-запросы.")
                    if response.headers.get("Content-Range", "") != f"bytes {start}-{start + expected - 1}/{self.size}":
                        raise PDFPreviewLimit("Несогласованный диапазон PDF.")
                    block = bytearray()
                    for chunk in response.iter_content(16384):
                        block.extend(chunk[:expected - len(block)])
                        if len(block) == expected:
                            break
                    if len(block) != expected:
                        raise PDFPreviewLimit("Неполный диапазон PDF.")
                    self.cache[index] = bytes(block)
                    self.total += len(block)
            chunk = self.cache[index][offset:offset + count]
            if not chunk:
                break
            parts.append(chunk)
            self.pos += len(chunk)
            count -= len(chunk)
        return b"".join(parts)


def fetch_url(url, session=None, limits=Limits()):
    """GET only; redirects handled manually, no retries, TLS verification stays enabled."""
    result = FetchResult(url, url)
    if not valid_url(url):
        result.error_code, result.error_detail = "invalid_url", "Не абсолютный HTTP(S)-адрес."
        return result
    own_session = session is None
    session = session or requests.Session()
    started = time.monotonic()
    try:
        for hop in range(limits.max_redirects + 1):
            if time.monotonic() - started > limits.total_seconds:
                result.error_code = "time_budget"
                break
            pdf_requested = urlsplit(result.final_url).path.casefold().endswith(".pdf")
            request_headers = {"User-Agent": "BookMonitorTest/0.3 (rule-based prototype)",
                               "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.1"}
            if pdf_requested:
                request_headers.update({"Range": "bytes=0-65535", "Accept-Encoding": "identity"})
            response = session.get(
                result.final_url, timeout=(limits.connect_timeout, limits.read_timeout),
                stream=True, allow_redirects=False,
                headers=request_headers,
            )
            try:
                result.http_status = response.status_code
                result.headers = {key.lower(): value for key, value in response.headers.items()}
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = result.headers.get("location", "")
                    if not location:
                        result.error_code = "redirect_without_location"
                        break
                    destination = urljoin(result.final_url, location)
                    if not valid_url(destination):
                        result.error_code = "invalid_redirect"
                        break
                    if hop == limits.max_redirects:
                        result.error_code = "redirect_limit"
                        break
                    result.redirects.append({"status": response.status_code,
                                             "from": result.final_url, "to": destination})
                    result.final_url = destination
                    continue
                if not 200 <= response.status_code < 300:
                    result.error_code = f"http_{response.status_code}"
                    break
                content_type = result.headers.get("content-type", "").casefold()
                is_pdf = "application/pdf" in content_type or (pdf_requested and "html" not in content_type)
                if is_pdf and not pdf_requested:
                    # Discover MIME from headers only, then request the same bounded preview.
                    response.close()
                    if time.monotonic() - started > limits.total_seconds:
                        result.error_code = "time_budget"
                        break
                    request_headers.update({"Range": "bytes=0-65535", "Accept-Encoding": "identity"})
                    response = session.get(result.final_url, timeout=(limits.connect_timeout, limits.read_timeout),
                                           stream=True, allow_redirects=False, headers=request_headers)
                    result.http_status = response.status_code
                    result.headers = {key.lower(): value for key, value in response.headers.items()}
                if is_pdf and response.status_code != 206:
                    result.error_code = "pdf_range_not_supported"
                    result.error_detail = "PDF не загружен целиком: необходим поддерживаемый Range-доступ."
                    break
                length = result.headers.get("content-length", "")
                response_limit = min(limits.max_bytes, 65536) if is_pdf else limits.max_bytes
                if length.isdigit() and int(length) > response_limit:
                    result.error_code = "response_too_large"
                    break
                chunks = []
                for chunk in response.iter_content(chunk_size=16_384):
                    if time.monotonic() - started > limits.total_seconds:
                        result.error_code = "time_budget"
                        break
                    result.size_bytes += len(chunk)
                    if result.size_bytes > response_limit:
                        result.error_code = "response_too_large"
                        break
                    chunks.append(chunk)
                if not result.error_code:
                    result.body = b"".join(chunks)
                    if is_pdf:
                        try:
                            result.pdf_stream = PDFRangeStream(result.final_url, session, result.body,
                                                               result.headers.get("content-range", ""),
                                                               limits.pdf_preview_bytes, owns_session=own_session)
                        except ValueError as error:
                            result.error_code, result.error_detail = "pdf_range_invalid", str(error)
                break
            finally:
                response.close()
    except requests.exceptions.SSLError as error:
        result.error_code, result.error_detail = "tls_error", str(error)[:400]
    except requests.Timeout as error:
        result.error_code, result.error_detail = "timeout", str(error)[:400]
    except requests.RequestException as error:
        result.error_code, result.error_detail = "network_error", str(error)[:400]
    finally:
        result.elapsed_seconds = round(time.monotonic() - started, 3)
        if own_session and result.pdf_stream is None:
            session.close()
    return result


def compact(text):
    return re.sub(r"\s+", " ", text or "").strip()


def text_key(text):
    words = re.findall(r"[^\W_]+|[+#]+", normalize_identity(text))
    # Spaced suffixes such as C ++ and C # retain the same identity as C++ and C#.
    return re.sub(r"\s+([+#])", r"\1", " ".join(words))


VOLUME_NUMBER = re.compile(r"\b(том|книга|часть|ч|volume|vol|book|part)\b[\s._-]+"
                           r"(?:no\.?\s*)?(\d+|[ivxlcdm]+)(?![\w+#]|\s*[+#])", re.I)
VOLUME_LABELS = {"том": "volume", "volume": "volume", "vol": "volume",
                 "книга": "book", "book": "book", "часть": "part", "ч": "part", "part": "part"}


def number_value(number):
    if number.isdigit():
        return int(number)
    total, previous = 0, 0
    for character in reversed(number):
        value = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}[character]
        total += -value if value < previous else value
        previous = max(previous, value)
    return total


def canonical_volume_title(title):
    """Comparison only: never use this key to generate catalogue IDs."""
    def replace(match):
        number = match[2]
        if not number.isdigit() and not re.fullmatch(
            r"m{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})", number
        ):
            return match[0]
        return match[1] + " " + str(number_value(number))

    # NFKC expands № into No; keep its boundary even in 'Том№13'.
    value = "" if title is None else str(title)
    return VOLUME_NUMBER.sub(replace, normalize_identity(value.replace("№", " No ")))


def title_key(title):
    return text_key(canonical_volume_title(title))


def _volume_items(title):
    for match in VOLUME_NUMBER.finditer(canonical_volume_title(title)):
        if match[2].isdigit():
            yield VOLUME_LABELS[match[1]], int(match[2])


def volume_markers(title):
    return dict(_volume_items(title))


def volume_conflict(expected, observed):
    first, second = list(_volume_items(expected)), list(_volume_items(observed))
    return any(key == other and value != number for key, value in first for other, number in second)


def title_punctuation_conflict(title, observed):
    """Compare internal punctuation in aligned title words, not presentation wrappers."""
    def parts(value):
        value = canonical_volume_title(value)
        tokens = list(re.finditer(r"[^\W_]+(?:\s*[+#])*|[+#]+", value))
        words = [text_key(token[0]) for token in tokens]
        gaps = [re.sub(r"[\s\"']", "", value[first.end():second.start()])
                for first, second in zip(tokens, tokens[1:])]
        # Leave the existing volume-marker formatting rules to their own matcher.
        for index in range(len(words) - 1):
            if words[index] in {"том", "книга", "часть", "ч", "volume", "vol", "book", "part"} and re.fullmatch(
                r"\d+|[ivxlcdm]+", words[index + 1]):
                if "|" not in gaps[index]:
                    gaps[index] = ""
                if index and "|" not in gaps[index - 1]:
                    gaps[index - 1] = ""
        pipes = ([value[:tokens[0].start()].count("|")] + [gap.count("|") for gap in gaps]
                 + [value[tokens[-1].end():].count("|")]) if tokens else [value.count("|")]
        return words, gaps, pipes

    expected, punctuation, expected_pipes = parts(title)
    words, gaps, pipes = parts(observed)
    candidates = [(gaps[index:index + len(expected) - 1], pipes[index:index + len(expected) + 1])
                  for index in range(len(words) - len(expected) + 1)
                  if expected and words[index:index + len(expected)] == expected]
    for candidate, observed_pipes in candidates:
        if expected_pipes != observed_pipes:
            continue
        first, second = punctuation, candidate
        # Preserve the existing optional subtitle-dash tolerance. If both titles
        # contain a dash, its position still participates in the comparison.
        if not any("-" in gap for gap in first) or not any("-" in gap for gap in second):
            first, second = ([gap.replace("-", "") for gap in first],
                             [gap.replace("-", "") for gap in second])
        if first == second:
            return False
    return bool(candidates)


def authors_match(expected, observed):
    """Compare complete author sets; only semicolons explicitly separate names."""
    def names(values):
        result = set()
        for value in values:
            for author in value.split(";"):
                words = text_key(author).split()
                if not words:
                    return set()  # Do not silently drop a malformed list member.
                if all(len(word) > 1 for word in words):
                    words.sort()  # Full names may be surname-first; initials may not.
                result.add(tuple(words))
        return result

    first, second = names(expected), names(observed)
    return bool(first) and first == second


@dataclass
class Page:
    kind: str = "unknown"
    title: str = ""
    h1: list = field(default_factory=list)
    authors: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    subjects: list = field(default_factory=list)
    signals: list = field(default_factory=list)
    text: str = ""  # Internal only: never exported.
    opening: str = ""  # PDF opening, internal only.
    issues: list = field(default_factory=list)
    blocking_issues: list = field(default_factory=list)
    dynamic_pending: bool = False
    is_collection: bool = False
    pdf_page_count: int = 0
    pdf_pages_read: int = 0
    partial: bool = False
    interaction_hints: list = field(default_factory=list)
    detected_formats: list = field(default_factory=list)
    issue_scopes: dict = field(default_factory=dict)  # Internal provenance; summary schema stays unchanged.

    def issue_applies(self, issue, subject=None):
        scopes = self.issue_scopes.get(issue, [])
        if (not scopes or len(scopes) != self.issues.count(issue) or not subject
            or issue in self.blocking_issues):
            return True
        return any(not (
            scope.get("scope_is_book") and subject.get("scope_is_book")
            and scope.get("scope_id") is not None and subject.get("scope_id") is not None
            and scope["scope_id"] != subject["scope_id"]
            and scope["scope_id"] not in subject.get("scope_ancestors", [])
            and subject["scope_id"] not in scope.get("scope_ancestors", [])
        ) for scope in scopes)

    def summary(self):
        return {
            "kind": self.kind, "title": self.title[:400], "h1": self.h1[:5],
            "authors": self.authors[:], "metadata": self.metadata,
            "subjects": [{"title": item["title"][:400], "authors": item["authors"][:],
                          "source": item["source"], "scope": item["scope"],
                          "author_facts": item.get("author_facts", [])}
                         for item in self.subjects[:30]],
            "text_chars": len(self.text), "text_excerpt": compact(self.text)[:240],
            "issues": self.issues, "blocking_issues": self.blocking_issues,
            "dynamic_pending": self.dynamic_pending, "is_collection": self.is_collection,
            "pdf_page_count": self.pdf_page_count, "pdf_pages_read": self.pdf_pages_read,
            "partial": self.partial,
            "observed_content_signals": self.signals,
            "interaction_hints": self.interaction_hints, "detected_formats": self.detected_formats,
        }


def unique(values):
    return list(dict.fromkeys(compact(value) for value in values if compact(value)))


AUTHOR_LABEL = r"(?:Автор(?:\s+книги)?|Author)\s*:\s*"


def node_authors(node, title="", facts=None):
    panel_authors = []

    def local(element, check_context=False):
        if check_context and "author" in str(element.get("itemprop", "")).split():
            for parent in element.parents:
                kind = str(parent.get("itemtype", "")).rstrip("/").rsplit("/", 1)[-1]
                if kind == "Book":
                    break
                if kind in {"Article", "BlogPosting", "NewsArticle", "Review"}:
                    return False  # Creator of the enclosing publication, not its subject book.
        if not title:
            return True
        state, owner, _ = content_owner(element, node, title, check_context=check_context)
        own_heading = local_heading(node)
        # A separately headed nested card supplies its own author, not its parent's.
        nested = (
            owner is not node and own_heading is not None
            and local_heading(owner) is not None and local_heading(owner) is not own_heading
            and not service_heading(local_heading(owner).get_text()))
        if (state == "bound" and nested and owner.name != "section" and not book_boundary(owner)
            and identity_heading(local_heading(owner).get_text()) == identity_heading(title)):
            panel_authors.append(element.get_text(" ", strip=True))
        return state == "bound" and not nested

    book_authors = [element.get_text(" ", strip=True) for element in node.select(
        '.book-author, .book_author, .book-author-link'
    ) if local(element)]
    authors = []
    author_label = AUTHOR_LABEL
    for label in node.find_all(string=re.compile(r"^\s*" + author_label + "$", re.I)):
        container = label.parent
        if container.name in {"span", "b", "strong", "em"}:
            container = container.parent
        if not local(container):
            continue
        value = compact(container.get_text(" ", strip=True))
        value = re.sub(r"^" + author_label, "", value, flags=re.I)
        if len(value) >= 2:
            authors.append(value)
    for element in node.find_all(string=re.compile(author_label, re.I)):
        if not local(element.parent):
            continue
        line = str(element).strip()
        found = re.search(author_label + r"(.{2,})$", line, re.I)
        if found:
            authors.append(found[1])
    role = "book_author" if book_authors else "labelled_author"
    authors = book_authors or authors
    if not authors:
        role = "local_byline"
        authors = [element.get_text(" ", strip=True) for element in node.select(
            '[itemprop="author"], .author') if local(element, check_context=True)]
    if not authors:
        role = "author_reference"
        authors = [element.get_text(" ", strip=True) for element in node.select(
            'a[href*="/authors/"], a[href*="/book-author/"], a[href*="/xfsearch/avtor/"], '
            'a[href*="/author_"], a[href*="/avtor_"], a[rel="author"]') if local(element, check_context=True)]
    if not authors:
        role = "bibliographic_panel"
        authors = panel_authors
    # Compare the whole set; document loading limits bound the input, not author count.
    authors = unique(re.sub(r"^" + author_label, "", value, flags=re.I) for value in authors)
    if facts is not None:
        facts.extend({"name": author, "role": role, "scope_locator": dom_locator(node)} for author in authors)
    return authors


def identity_heading(title):
    key = title_key(title)
    key = re.sub(r"^(?:(?:читать|скачать|слушать|книгу|аудиокнига|бесплатно|онлайн)\s+)+", "", key)
    return re.sub(r"(?:\s+(?:полностью|онлайн|бесплатно|слушать))+$", "", key)


def title_byline_role(title, expected, authors):
    """Recognize an edge byline, never delete arbitrary names inside a title."""
    def same_title(value):
        return (identity_heading(value) == identity_heading(expected)
                and not title_punctuation_conflict(expected, value))

    # A dash signature still needs an independent title (e.g. primary Book JSON-LD).
    parts = re.split(r"\s+-\s+", normalize_identity(title))
    if len(parts) == 2:
        for name, value in (parts, (parts[1], parts[0])):
            if same_title(value) and authors_match(authors, [name]):
                return "signature"
    # Preserve the established complete, explicitly delimited coauthor-list form.
    for boundary in re.finditer(r"\s+", title):
        left, right = title[:boundary.start()], title[boundary.end():]
        for name, value in ((left, right), (right, left)):
            if ";" in name and same_title(value) and authors_match(authors, [name]):
                return "author_list"
    return ""


def service_heading(title):
    return bool(re.fullmatch(
        r"(?:глава|chapter)\s+[\divxlcdm]+(?:\s.*)?|"
        r"(?:читать|скачать|слушать)(?:\s+(?:книгу|онлайн|бесплатно))*|"
        r"текст(?: книги)?|содержание|оглавление|описание|аннотация|файлы|форматы|"
        r"шрифт|настройки шрифта|размер шрифта|параметры чтения|font|font size|font settings|"
        r"download|read online", text_key(title)))


def book_boundary(node):
    return node.name == "article" or bool(set(node.get("class", [])) & {"book-card", "book-item"}) or str(
        node.get("itemtype", "")).rstrip("/").endswith("/Book")


def local_heading(node):
    """Heading of this container, not a heading of an embedded book/section."""
    headings = []
    for heading in node.find_all(re.compile(r"^h[1-6]$")):
        parent = heading.parent
        while parent is not node and parent is not None:
            if parent.name == "section" or book_boundary(parent):
                break
            parent = parent.parent
        if parent is node:
            headings.append(heading)
    if headings:
        return min(headings, key=lambda heading: int(heading.name[1]))
    # A local bibliography can use a formatted title rather than an HTML heading.
    # Attribute labels ("Author:", "Quality:") are not titles of separate books.
    if node.select_one('.book-author, .author, [itemprop="author"], a[href*="/authors/"]') or re.search(
        r"\b(?:автор|author)\s*:", node.get_text(" ", strip=True), re.I):
        for candidate in node.find_all(["b", "strong"], recursive=False):
            value = compact(candidate.get_text(" ", strip=True))
            if value and not value.endswith(":") and not service_heading(value):
                return candidate
    return None


def dom_locator(node):
    if node.get("id"):
        return f"{node.name}#{node['id']}"
    classes = ".".join(node.get("class", [])[:3])
    return node.name + ("." + classes if classes else "")


def region_role(node):
    """Role of this region itself, never inferred from unrelated descendants."""
    marker = " ".join([str(node.get("id", "")), " ".join(node.get("class", [])),
                       str(node.get("itemprop", "")), str(node.get("role", ""))])
    heading = local_heading(node)
    label = heading.get_text(" ", strip=True) if heading else ""
    if node.name in {"p", "li"} or re.fullmatch(r"h[1-6]", node.name or ""):
        label = node.get_text(" ", strip=True)
    if re.search(r"(?:^|[-_\s])(?:user|profile|review\w*|comment\w*|advert\w*|ads|recommend\w*|related|bibliography)(?:$|[-_\s])",
                 marker, re.I) or re.match(
        r"\s*(?:реклам\w*|рекоменду\w*|рекомендации|другие книги|также прочита\w*|"
        r"advert\w*|recommend\w*|suggested reading|reader account|user profile|reviews?|comments?)\b", label, re.I):
        return "secondary"
    if re.search(r"\b(?:состав(?:е)?|оглавлени[еи]|содержание|contents|table of contents|список книг|список произведений)\b", label, re.I):
        return "contents"
    return ""


def fact_role(element, root):
    """Nearest semantic region, shared by authors, resources and collection entries."""
    current = element
    while current is not None:
        role = region_role(current)
        if role:
            return role
        if current is root:
            break
        for sibling in current.previous_siblings:
            if not getattr(sibling, "name", None):
                continue
            # Heading roles persist in a flat list; prose applies to the adjacent action.
            if region_role(sibling) and (re.fullmatch(r"h[1-6]", sibling.name) or sibling.name == "p"):
                return region_role(sibling)
            break
        current = current.parent
    return "book"


def neutral_action_context(text, title):
    key = text_key(text)
    return (not key or resource_label_matches(text, title) or key in {
        "оставить отзыв", "отзывы", "поделиться", "в избранное", "сохранить на полку", "закрыть",
        "автор", "автор книги", "author",
    } or bool(re.fullmatch(r"(?:скачать|download)\s+(?:epub|fb2|pdf|txt)\s+\d+\s*(?:кб|мб|kb|mb)", key)))


def content_owner(element, root, title, check_context=False, authors=()):
    """Respect ancestor containers and sibling heading boundaries, never arbitrary <main> inheritance."""
    expected = identity_heading(title)
    if fact_role(element, root) == "secondary":
        return ("unresolved" if check_context else "other_scope"), element, "Факт относится к пользовательской, рекламной или рекомендательной области."
    if not expected:
        return "unresolved", root, "Нет заголовка области книги."
    owner = root
    current = element
    branch = element
    while current is not None:
        heading = local_heading(current)
        heading_text = compact(heading.get_text(" ", strip=True)) if heading else ""
        if heading is not None and heading.parent is not current and identity_heading(heading_text) != expected:
            heading_branch = heading
            while heading_branch.parent is not current:
                heading_branch = heading_branch.parent
            # A sibling region's heading identifies that region, not this outer action.
            # A wrapper containing only the title still supplies its parent's heading.
            if heading_branch is not branch and (book_boundary(heading_branch) or
                compact(heading_branch.get_text(" ", strip=True)) != heading_text):
                heading_text = ""
        if (identity_heading(heading_text) != expected and any(text_key(heading_text) in {
            text_key(author), " ".join(reversed(text_key(author).split()))} for author in authors)):
            heading_text = ""
        if heading_text and not service_heading(heading_text):
            if title_punctuation_conflict(title, heading_text):
                return "unresolved", current, "Пунктуация заголовка отличается; принадлежность требует подтверждения."
            if identity_heading(heading_text) != expected:
                if normalize_text(identity_heading(heading_text)) == normalize_text(expected):
                    return "unresolved", current, "Заголовок отличается е/ё; принадлежность требует подтверждения."
                return "other_scope", current, "Другой заголовок области: " + heading_text[:150]
            if owner is root:
                owner = current
        elif current is not root and book_boundary(current):
            return "unresolved", current, "Отдельная карточка/article без собственной идентичности."
        elif (check_context and current is not root and current is not element
              and (not heading_text or service_heading(heading_text))):
            fragments = [child.get_text(" ", strip=True) if getattr(child, "name", None) else str(child)
                         for child in current.children if child is not branch
                         and getattr(child, "name", None) not in {"h1", "h2", "h3", "h4", "h5", "h6"}
                         and not (getattr(child, "name", None) and local_heading(child) is not None
                                  and not service_heading(local_heading(child).get_text()))]
            context = compact(" ".join(fragment for fragment in fragments if not neutral_action_context(fragment, title)))
            native_download = element.name == "a" and action_link_kind(element, element.get("href", "")) == "download_link"
            # A site-generated download control may share a description paragraph.
            # Generic links next to prose still need an explicit local book identity.
            ordinary_paragraph = (current.name == "p" and native_download
                                  and re.fullmatch(r"(?:Скачиваний|Downloads)\s*:\s*\d*", element.get("title", ""), re.I)
                                  and not re.search(r":|[«»\"]|\s[-—–]\s|\bby\b", context, re.I))
            bibliography = compact(" ".join(fragments))
            if (current.name == "p" and native_download and current.parent is not root
                and local_heading(current.parent) is None):
                bibliography = compact(current.parent.get_text(" ", strip=True))
            bibliographic_context = (f" {expected} " in f" {title_key(bibliography)} " and any(
                re.search(r"\b(?:автор|author)\s*:\s*" + re.escape(author), bibliography, re.I) for author in authors))
            for child in current.children:
                if child is branch or not getattr(child, "name", None):
                    continue
                headings = [compact(h.get_text(" ", strip=True)) for h in child.find_all(["h1", "h2", "h3"])]
                if (any(identity_heading(value) == expected for value in headings)
                    and any(text_key(value) in {text_key(author), " ".join(reversed(text_key(author).split()))}
                            for value in headings for author in authors)):
                    bibliographic_context = True
            if context and not ordinary_paragraph and not bibliographic_context:
                return "unresolved", current, "Отдельный блок с описанием без собственного заголовка книги."
            if bibliographic_context:
                # A verified local title/byline panel owns its sibling resource. Headings in
                # unrelated outer columns must not replace this nearer identity.
                return "bound", owner if owner is not root else current, "Локальная область с совпадающими названием и автором рядом с признаком контента."
        if current is root:
            return "bound", owner, "Область с заголовком проверяемой книги; вложенные области проверены."
        peer_checked = False
        for sibling in current.previous_siblings:
            if check_context and not peer_checked and getattr(sibling, "name", None):
                peer_checked = True
                # A neighbouring bibliographic identity must corroborate this book,
                # even when the publisher has not labelled the block as advertising.
                separate_card = local_heading(sibling) is not None and sibling.select_one('a[href], audio, [itemprop="text"]')
                peer_authors = [] if separate_card else node_authors(sibling)
                peer_text = title_key(sibling.get_text(" ", strip=True))
                # An inline title/byline is a bibliographic relationship regardless
                # of the publisher's prose prefix or CSS vocabulary.
                byline = re.search(r"\bby\s+(.{2,})$", compact(sibling.get_text(" ", strip=True)), re.I)
                if not separate_card and byline:
                    peer_authors = unique(peer_authors + [byline[1]])
                labelled_byline = re.match(AUTHOR_LABEL, sibling.get_text(" ", strip=True), re.I)
                # A labelled row may name one coauthor; an independent bibliography must name all.
                if peer_authors and (not (authors_match(authors, peer_authors) or (
                    labelled_byline and any(authors_match([author], [peer])
                                            for author in authors for peer in peer_authors)))
                                     or (f" {expected} " not in f" {peer_text} " and not labelled_byline)):
                    return "unresolved", sibling, "Соседний библиографический блок не подтверждает идентичность этой книги."
            if getattr(sibling, "name", None) not in {"h1", "h2", "h3", "h4", "h5", "h6", "header"}:
                continue
            sibling_heading = local_heading(sibling) if sibling.name == "header" else sibling
            if sibling_heading is None:
                continue
            heading_text = compact(sibling_heading.get_text(" ", strip=True))
            if (identity_heading(heading_text) != expected and any(text_key(heading_text) in {
                text_key(author), " ".join(reversed(text_key(author).split()))} for author in authors)):
                continue
            if service_heading(heading_text):
                continue
            if title_punctuation_conflict(title, heading_text):
                return "unresolved", current.parent, "Пунктуация предшествующего заголовка не подтверждает принадлежность."
            if identity_heading(heading_text) != expected:
                if normalize_text(identity_heading(heading_text)) == normalize_text(expected):
                    return "unresolved", current.parent, "Предшествующий заголовок отличается е/ё; принадлежность не подтверждена."
                return "other_scope", current.parent, "Предшествующий заголовок другой области: " + heading_text[:150]
            break
        branch, current = current, current.parent
    return "unresolved", root, "Элемент не принадлежит области книги."


def resource_label_matches(label, title):
    """Action-only labels inherit the local card; descriptive labels need its actual title."""
    label_key = title_key(label)
    label_key = re.sub(r"\s+\d+(?:\s+\d+)?\s+(?:кб|мб|kb|mb)$", "", label_key)
    if not label_key:
        return False
    expected = identity_heading(title)
    remainder = (" " + label_key + " ").replace(" " + expected + " ", " ").strip()
    return bool(re.fullmatch(
        r"(?:(?:скачать|загрузить|читать|слушать|книга|книгу|бесплатно|онлайн|"
        r"download|read|listen|online|book|epub|fb2|pdf|mobi|azw3|djvu|txt|rtf|docx|torrent|zip|mp3|m4a|ogg|wav)\s*)*",
        remainder)) and not title_punctuation_conflict(title, label)


def link_labels(link):
    """Human-readable names of an action, including accessible icon labels."""
    return unique(value for value in [link.get_text(" ", strip=True), link.get("aria-label", ""),
                                     link.get("title", "")] + [img.get("alt", "") for img in link.select("img[alt]")]
                  if text_key(value))


def action_link_kind(link, href):
    """Purpose must be visible in both label and destination, not just in a button."""
    label = text_key(next(iter(link_labels(link)), ""))
    label = re.sub(r"\s+\d+(?:\s+\d+)?\s+(?:кб|мб|kb|mb)$", "", label)
    path = urlsplit(href).path.casefold()
    query = urlsplit(href).query.casefold()
    if re.fullmatch(r"(?:читать(?: книгу)?(?: онлайн| полностью| бесплатно)*|read(?: online)?)", label):
        if re.search(r"/(?:read|reader|fb2reader|chitat)(?:/|[_.-])", path):
            return "reading_link"
    if re.fullmatch(r"(?:скачать|загрузить|download)(?:\s+(?:книгу|файл|book|бесплатно|epub|fb2|pdf|txt|torrent|mp3))*|epub|fb2|pdf|txt", label):
        if re.search(r"/(?:download|downloads)(?:/|$)", path) or re.search(r"(?:^|&)(?:do|action)=download(?:&|$)", query):
            return "download_link"
    return ""


def _reader_is_commentary(reader, root):
    """Explicit commentary roles outweigh a reader-like container and text length."""
    for region in [reader, *reader.parents]:
        marker = " ".join([str(region.get("id", "")), " ".join(region.get("class", [])),
                           str(region.get("itemprop", "")), str(region.get("itemtype", ""))])
        if re.search(r"(?:^|[-_\s/])(?:reviews?|reviewBody|annotation|description|synopsis|critique)(?:$|[-_\s/])",
                     marker, re.I):
            return True
        if region is root:
            break  # The book's own title is not a label of the nested text's role.
        heading = local_heading(region)
        label = heading.get_text(" ", strip=True) if heading else ""
        if region is reader and not label:
            label = next(reader.stripped_strings, "")
        if re.match(r"\s*(?:рецензия|отзыв|аннотация|критический\s+(?:обзор|разбор|анализ)|"
                    r"(?:book\s+)?review|synopsis|annotation|critical\s+(?:review|analysis))\b", label, re.I):
            return True
    return False


def _file_link_issue(element, owner, document_url, resource_url, title, authors):
    """Contradictory book hints leave a file unresolved; storage IDs are independent."""
    source, target = urlsplit(document_url), urlsplit(resource_url)

    def book_ids(url):
        return set(re.findall(r"/(?:books?|knigi|kniga)[-_/](\d+)(?=/|$|\.html?$)", url.path, re.I)) | {
            value for key in ("book_id", "bookid") for value in parse_qs(url.query).get(key, [])}

    current = element
    while owner.get("data-book-id") and current is not None and current is not owner:
        if current.get("data-book-id") and owner["data-book-id"] != current["data-book-id"]:
            return "Заявленный data-book-id файла противоречит карточке книги."
        current = current.parent
    if source.netloc.casefold() == target.netloc.casefold():
        first, second = book_ids(source), book_ids(target)
        if first and second and first != second:
            return "Адрес файла указывает другой ID книги на том же сайте; принадлежность файла не подтверждена."
    own_words = set(title_key(title).split() + text_key(" ".join(authors)).split()) | {"book", "ebook", "file", "download"}
    title_words = {word for word in identity_heading(title).split()
                   if len(word) > 2 and word not in {"the", "and", "for"}}
    names = [unquote(target.path.rsplit("/", 1)[-1]), str(element.get("download", ""))]
    for name in names:
        stem = name.rsplit(".", 1)[0]
        if re.fullmatch(r"[a-f\d_-]{16,}", stem, re.I):
            continue  # A hash/UUID is an object identifier, not a bibliographic title.
        stem = stem.replace("_", " ")
        if volume_conflict(title, stem):
            return "Номер тома/части/книги в имени файла противоречит карточке: " + name[:120]
        name_key, expected = identity_heading(stem), identity_heading(title)
        if name_key != expected and normalize_text(name_key) == normalize_text(expected):
            return "Имя файла отличается е/ё; принадлежность этой книге не подтверждена: " + name[:120]
        words = {word for word in title_key(stem).split() if word.isalpha() and len(word) > 1}
        # A partial filename may abbreviate the title; a foreign name or an extended
        # complete title cannot establish ownership merely through a shared word.
        if (len(words) >= 2 and not words <= own_words
            and (not words & title_words or title_words <= words)):
            return "Описательное имя файла не подтверждает его принадлежность этой книге: " + name[:120]
    return ""


def content_signals(node, base_url, title="", audio_claim=False, authors=(), resource_base=None):
    """Keep observed signals, but explicitly distinguish bound, foreign and unresolved ownership."""
    signals = []
    unverified_signals = []

    def add(element, signal, unverified=""):
        binding, owner, reason = content_owner(element, node, title, check_context=True, authors=authors)
        role = fact_role(element, node)
        if signal["kind"] == "reader_text" and _reader_is_commentary(element, node):
            binding, role = "other_scope", "secondary"
            reason = "Блок обозначен как рецензия, аннотация или критический обзор, а не текст произведения."
        owner_authors = node_authors(owner, title) if binding == "bound" else []
        if binding == "bound" and not owner_authors:
            for heading in owner.find_all(["h2", "h3"]):
                value = compact(heading.get_text(" ", strip=True))
                previous = heading.find_previous(["h1", "h2", "h3"])
                if (previous and identity_heading(previous.get_text()) == identity_heading(title)
                    and any(text_key(value) in {text_key(author), " ".join(reversed(text_key(author).split()))} for author in authors)):
                    owner_authors.append(value)
        if binding == "bound" and authors and owner_authors and not authors_match(authors, owner_authors):
            if authors_match([a.replace("ё", "е").replace("Ё", "Е") for a in authors],
                             [a.replace("ё", "е").replace("Ё", "Е") for a in owner_authors]):
                binding, reason = "unresolved", "Имя автора области отличается е/ё; идентичность не подтверждена."
            else:
                binding, reason = "other_scope", "Автор области не совпадает с автором основной карточки."
        if binding == "bound" and signal["kind"] in {"file_link", "reading_link", "download_link"}:
            labels = link_labels(element)
            labels = [label for label in labels if not re.fullmatch(r"(?:Скачиваний|Downloads)\s*:\s*\d*", label, re.I)]
            if (not labels and signal["kind"] != "file_link") or not all(resource_label_matches(label, title) for label in labels):
                binding, reason = "unresolved", "Подпись файла не связывает его с заголовком этой книги."
            # Contradicting stable page IDs must not be inherited by another download/reader.
            if signal["kind"] != "file_link":
                source_ids = re.findall(r"(?:^|[-_/])(\d{4,})(?=[-_/\.]|$)", urlsplit(base_url).path)
                target_ids = re.findall(r"(?:^|[-_/])(\d{4,})(?=[-_/\.]|$)", urlsplit(signal["url"]).path)
                if source_ids and target_ids and not set(source_ids) & set(target_ids):
                    binding, reason = "unresolved", "Идентификатор адреса действия отличается от карточки книги."
        if binding == "bound" and signal["kind"] == "file_link":
            issue = _file_link_issue(element, owner, base_url,
                                     urljoin(resource_base or base_url, element["href"]), title, authors)
            if issue:
                binding, reason = "unresolved", issue
        if binding == "bound" and signal["kind"] == "audio_source":
            audio = element if element.name == "audio" else element.parent
            label = audio.get("title") or audio.get("aria-label")
            previous = audio.find_previous_sibling()
            context = compact(previous.get_text(" ", strip=True)) if previous else ""
            contextual_audio = bool(re.search(r"аудиокниг|аудиоверси|audiobook", context, re.I))
            explicit_card = book_boundary(owner)
            named_audio = bool(label and f" {identity_heading(title)} " in f" {title_key(label)} ")
            if (label and not resource_label_matches(label, title)) or not (
                named_audio or explicit_card or audio_claim or contextual_audio
            ):
                binding, reason = "unresolved", "Плеер не связан с книгой подписью, карточкой или контекстом аудиоверсии."
        if signal["kind"] == "embedded_document":
            label = element.get("title") or element.get("aria-label", "")
            if label and not resource_label_matches(label, title) and not re.fullmatch(
                r"(?:book reader|reader|читалка|текст книги|book text|pdf(?: document)?|документ pdf)", label, re.I):
                binding, reason = "other_scope", "Подпись встроенного документа относится к другой области: " + label[:150]
        if unverified:
            if role == "secondary" or _reader_is_commentary(element, node):
                binding = "other_scope"
            elif binding == "bound":
                binding, reason = "unresolved", unverified
        (unverified_signals if unverified else signals).append({**signal, "binding": binding, "scope_title": title if binding == "bound" else "",
                        "document_url": base_url, "raw_resource": element.get("href", element.get("src", element.get("data", ""))),
                        "semantic_role": role,
                        "scope_authors": owner_authors,
                        "scope_locator": dom_locator(owner), "element_locator": dom_locator(element),
                        "binding_reason": reason})

    for link in node.select("a[href]"):
        href = urljoin(resource_base or base_url, link.get("href", ""))
        if not valid_url(href):
            continue
        label = " ".join(link_labels(link))
        filename = str(link.get("download", ""))
        # A fragment does not identify a fetched file. Keep query handling unchanged.
        extension = re.search(r"\.(epub|fb2|pdf|mobi|azw3|djvu|txt|rtf|docx|torrent|zip|mp3|m4a|ogg|wav)(?:$|[?#])",
                              href.split("#", 1)[0], re.I)
        if not extension and filename:
            extension = re.search(r"\.(epub|fb2|pdf|mobi|azw3|djvu|txt|rtf|docx|zip|mp3|m4a|ogg|wav)$", filename, re.I)
        if extension:
            if extension[1].casefold() in {"zip", "torrent"} and not re.search(
                r"книг|сборник|epub|fb2|pdf|mobi", label + " " + filename, re.I
            ):
                continue  # A generic program/archive download is not book-specific evidence.
            add(link, {"kind": "file_link", "format": "audio" if extension[1].casefold() in {"mp3", "m4a", "ogg", "wav"} else "ebook", "url": href[:1500],
                        "detail": f"Ссылка на файл {extension[1].lower()}: {label[:100]}"})
        elif not extension:
            kind = action_link_kind(link, href)
            if kind:
                add(link, {"kind": kind, "format": "audio" if audio_claim else "ebook", "url": href[:1500],
                           "detail": f"Связанная ссылка {'чтения' if kind == 'reading_link' else 'скачивания'}: {label[:100]}"})
    for audio in node.select("audio[src], audio source[src]"):
        href = urljoin(resource_base or base_url, audio.get("src", ""))
        if valid_url(href):
            add(audio, {"kind": "audio_source", "format": "audio", "url": href[:1500],
                        "detail": "Источник аудиоплеера в карточке книги."})
    for embedded in node.select("iframe, object, embed"):
        raw = embedded.get("data" if embedded.name == "object" else "src", "")
        href = urljoin(resource_base or base_url, raw)
        label = embedded.get("title") or embedded.get("aria-label", "")
        pdf = (embedded.get("type", "").split(";", 1)[0].strip().casefold() == "application/pdf"
               or bool(re.search(r"\.pdf$", urlsplit(href).path, re.I)))
        reader_frame = embedded.name == "iframe" and (
            bool(embedded.get("srcdoc", "").strip())
            or bool(re.search(r"/(?:read|reader|fb2reader|chitat)(?:/|[_.-]|$)", urlsplit(href).path, re.I))
            or bool(label and (resource_label_matches(label, title) or re.fullmatch(
                r"(?:book reader|читалка|текст книги|book text)", label, re.I))))
        if not (pdf or reader_frame) or (raw and not valid_url(href)):
            continue
        if not raw and not embedded.get("srcdoc", "").strip():
            continue
        add(embedded, {"kind": "embedded_document", "format": "ebook", "url": (href if raw else base_url)[:1500],
                       "detail": f"Встроенный документ {embedded.name}; содержимое не проверено."},
            unverified="Встроенный документ в области книги не проверен; наличие текста произведения не установлено.")
    readers = node.select(".reader-text, .book-text, .chapter-text, .block_read_text, #reader, "
                          "#book-content-container, [data-reader-content], [itemprop='text']")
    known_readers = {id(reader) for reader in readers}
    # Semantic reading labels/chapter headings establish a candidate, never positive proof.
    readers += [element for element in node.select('[aria-label]') if re.fullmatch(
        r"(?:book text|текст(?: книги)?|read online|читать(?: онлайн)?|excerpt|отрывок)",
        compact(element["aria-label"]), re.I)]
    readers += [heading for heading in node.find_all(re.compile(r"^h[2-6]$"))
                if re.fullmatch(r"(?:глава|chapter)\s+[\divxlcdm]+(?:\s.*)?",
                                text_key(heading.get_text(" ", strip=True)))]
    seen_readers = set()
    for reader in readers:
        if id(reader) in seen_readers:
            continue
        seen_readers.add(id(reader))
        text_node = reader.parent if id(reader) not in known_readers and re.fullmatch(r"h[2-6]", reader.name) else reader
        text = compact(text_node.get_text(" ", strip=True))
        enough = len(text) >= 600 and len(re.findall(r"\w+", text)) >= 80
        if text:
            add(reader, {"kind": "reader_text", "format": "ebook", "url": base_url,
                         "detail": f"Блок чтения: {len(text)} символов; полнота книги не установлена."},
                unverified="" if enough and id(reader) in known_readers else
                "Явная область чтения: объём текста или HTML-структура недостаточны для подтверждения размещения.")
    if audio_claim:
        for player in node.select('[id*="player"], [class*="playlist"], [data-playlist]'):
            lines = unique(player.get_text("\n", strip=True).splitlines())
            tracks = [line for line in lines if re.search(r"\(\d{1,3}\)|^\d{1,3}[.)]|\.(?:mp3|m4a)", line, re.I)]
            if len(tracks) >= 2:
                add(player, {"kind": "audio_playlist", "format": "audio", "url": base_url,
                             "detail": f"Заполненный плеер аудиокниги: не менее {len(tracks)} дорожек."})
    observed = list({(item["kind"], item["url"], item["binding"], item["element_locator"]): item
                     for item in signals + unverified_signals}.values())
    # Bound evidence must survive the observation cap ahead of unrelated regions.
    priority = {"bound": 0, "unresolved": 1, "other_scope": 2}
    return sorted(observed, key=lambda item: priority[item["binding"]])[:30]


def collection_members(main, title, authors, base_url, resource_base=None):
    """A declared TOC and a scoped collection download establish membership, not prose mentions."""
    if not authors:
        found = re.match(r"\s*(.{2,100}?)\s*[-—–:]\s*(?:сборник|собрание|collection)", title, re.I)
        authors = [found[1].strip()] if found else []
    if not authors:
        return []
    entries = []
    for item in main.select("ul li, ol li"):
        if fact_role(item, main) != "contents":
            continue
        value = compact(item.get_text(" ", strip=True))
        if value and len(value) <= 300 and not item.select_one("ul,ol"):
            entries.append((re.sub(r"^\d+[.)]\s*", "", value), dom_locator(item)))
    for line_node in main.find_all(string=True):
        if fact_role(line_node.parent, main) != "contents":
            continue
        line = str(line_node)
        found = re.fullmatch(r"\s*\d{1,3}[.)]\s+(.{2,300})", line)
        if found:
            entries.append((found[1].strip(), "numbered_toc"))
    downloads = []
    for link in main.select("a[href]"):
        href = urljoin(resource_base or base_url, link["href"])
        if fact_role(link, main) == "secondary":
            continue
        label = compact(link.get_text(" ", strip=True))
        if not valid_url(href) or not re.search(r"скачать|download", label, re.I):
            continue
        own_collection_label = bool(re.search(r"сборник|собрание|collection", label, re.I))
        previous = link.find_previous(lambda tag: tag.name in {"h1", "h2", "h3", "h4", "strong", "b"}
                                      and tag.find_parent("a") is None
                                      and bool(compact(tag.get_text(" ", strip=True))))
        header = previous.get_text(" ", strip=True) if previous else ""
        collection_header = bool(re.match(r"\s*(?:скачать|download)", header, re.I) and re.search(
            r"сборник|собрание|collection", header, re.I) and any(text_key(author) in text_key(header) for author in authors))
        if not (own_collection_label or collection_header):
            continue
        if not re.search(r"\.(?:zip|rar|7z|torrent)(?:$|[?#])|/(?:download|downloads)/", href, re.I):
            continue
        # A different headed card between the link and collection root is never a collection action.
        conflicting = False
        for parent in link.parents:
            if parent is main:
                break
            if book_boundary(parent) or parent.name == "section":
                heading = local_heading(parent)
                if heading and not re.search(r"сборник|собрание|collection", heading.get_text(), re.I):
                    conflicting = True
        if conflicting:
            continue
        downloads.append({"kind": "collection_download", "format": "ebook", "url": href[:1500],
                          "document_url": base_url, "raw_resource": link["href"], "semantic_role": "contents",
                          "binding": "bound", "scope_locator": dom_locator(main),
                          "element_locator": dom_locator(link), "collection_title": title[:300],
                          "scope_authors": authors, "detail": "Скачиваемый сборник с явно указанным составом."})
    if not downloads:
        return []
    return [{"title": entry, "authors": authors, "source": "collection_toc", "scope": "collection_entry",
             "signals": [{**signal, "scope_title": entry, "listed_title": entry, "toc_locator": locator,
                          "binding_reason": "Точное название в составе авторского сборника и связанная ссылка его скачивания."}
                         for signal in downloads[:5]], "detected_formats": ["ebook"]}
            for entry, locator in dict.fromkeys(entries)][:150]


def json_books(soup, document_url=""):
    books = []
    primary_ids = set()
    linked_ids = set()
    nodes = {}

    def has_type(value, name):
        types = value.get("@type", [])
        types = [types] if isinstance(types, str) else types
        return isinstance(types, list) and any(kind in types for kind in (
            name, "https://schema.org/" + name, "http://schema.org/" + name))

    def reference_key(value):
        return urljoin(document_url, value) if isinstance(value, str) and value and not re.search(r"\s", value) else ""

    def index_nodes(value):
        if isinstance(value, list):
            for item in value:
                index_nodes(item)
        elif isinstance(value, dict):
            key = reference_key(value.get("@id"))
            if key and len(value) > 1:  # Bare references are not definitions.
                if not secondary and has_type(value, "Book"):
                    linked_ids.add(key)
                definition = {**value, "@id": key}
                definitions = nodes.setdefault(key, [])
                if definition not in definitions:
                    definitions.append(definition)
            for item in value.values():
                if isinstance(item, (dict, list)):
                    index_nodes(item)

    def author_name(value):
        if not isinstance(value, dict):
            return str(value), False
        if "@id" not in value:
            return str(value.get("name", "")), False
        definitions = nodes.get(reference_key(value["@id"]), [])
        names = unique(node["name"] for node in definitions if isinstance(node.get("name"), str))
        if not names or not all(authors_match([names[0]], [name]) for name in names[1:]) or any(
            ("name" in node and not isinstance(node["name"], str)) or has_type(node, "Book") or ("@type" in node and not (
            has_type(node, "Person") or has_type(node, "Organization")
        )) for node in definitions):
            return "", True
        return names[0], False

    def primary_references(value, page_only=False):
        if isinstance(value, list):
            for item in value:
                primary_references(item, page_only)
        elif isinstance(value, dict):
            entity = value.get("mainEntity", {}) if not page_only or (
                has_type(value, "WebPage") and on_page(value)) else {}
            entities = entity if isinstance(entity, list) else [entity]
            primary_ids.update(item["@id"] for item in entities if isinstance(item, dict) and item.get("@id"))
            owner = value.get("mainEntityOfPage")
            owner = owner.get("@id") if isinstance(owner, dict) else owner
            if owner and urljoin(document_url, str(owner)) == document_url and value.get("@id"):
                primary_ids.add(value["@id"])
            for item in value.values():
                if isinstance(item, (dict, list)):
                    primary_references(item, page_only)

    def visit(value, primary=True, page_link=False):
        if isinstance(value, list):
            for item in value:
                visit(item, primary, page_link)
        elif isinstance(value, dict):
            if has_type(value, "Book") and value.get("name"):
                authors = value.get("author", [])
                authors = authors if isinstance(authors, list) else [authors]
                resolved = [author_name(item) for item in authors]
                books.append({"title": str(value["name"]), "authors": unique(name for name, _ in resolved),
                              "_author_unresolved": any(unknown for _, unknown in resolved),
                              "primary_metadata": primary and (
                        not primary_ids or value.get("@id") in primary_ids) and (
                        not secondary or reference_key(value.get("@id")) in linked_ids
                        or page_link or on_page(value) or (
                            bool(document_url) and page_owner(value) == document_url)), "_scope_id": owner_id})
            for key, item in value.items():
                if isinstance(item, (dict, list)):
                    visit(item, primary and key in {"@graph", "mainEntity"}, page_link or (
                        key == "mainEntity" and has_type(value, "WebPage") and on_page(value)))

    def on_page(value):
        return bool(document_url) and any(reference_key(value.get(key)) == document_url for key in ("@id", "url"))

    def page_owner(value):
        owner = value.get("mainEntityOfPage")
        owner = owner.get("@id") if isinstance(owner, dict) else owner
        return reference_key(owner)

    documents = []
    # The HTTP/browser byte budget bounds this input; script order must not hide
    # identity conflicts or author definitions. Public summaries remain capped.
    for script in soup.select('script[type="application/ld+json"]'):
        owner = next((parent for parent in script.parents if book_boundary(parent)), None)
        owner_id = id(owner) if owner is not None else None
        region = next((parent for parent in script.parents
                       if parent.name not in {"html", "body", "main"} and region_role(parent) == "secondary"), None)
        # A separate review region owns its metadata. A review nested in a book
        # does not by itself disprove that book's ownership of the script.
        secondary = region is not None and not (
            owner is not None and any(parent is owner for parent in region.parents))
        try:
            data = json.loads(script.string or script.get_text())
            index_nodes(data)
            documents.append((data, owner_id, secondary))
        except (ValueError, TypeError, RecursionError):
            continue
    for data, _, secondary in documents:
        try:
            primary_ids.clear()
            primary_references(data, page_only=secondary)
            linked_ids.update(reference_key(key) for key in primary_ids)
        except (ValueError, TypeError, RecursionError):
            continue
    for data, owner_id, secondary in documents:
        try:
            primary_ids.clear()
            primary_references(data)
            visit(data)
        except (ValueError, TypeError, RecursionError):
            continue
    return books


def inline_styles(node):
    """Apply declaration order and !important within the inline origin."""
    declarations, priorities = {}, {}
    for key, value in re.findall(r"([\w-]+)\s*:\s*([^;]+)", node.get("style", "").casefold()):
        important = bool(re.search(r"!\s*important\s*$", value))
        if important or not priorities.get(key):
            declarations[key] = re.sub(r"!\s*important\s*$", "", value).strip()
            priorities[key] = important
    return declarations


def extract_html(document, limits):
    soup = BeautifulSoup(document.body, "html.parser")
    page = Page(kind="html")
    page.title = compact(soup.title.get_text(" ", strip=True)) if soup.title else ""
    for meta in soup.select("meta[name], meta[property]")[:100]:
        name = str(meta.get("property") or meta.get("name")).casefold()
        if name in {"author", "book:author", "og:title", "og:type", "og:site_name", "description", "og:description"}:
            value = str(meta.get("content", ""))
            page.metadata[name] = value if name in {"author", "book:author"} else value[:1000]
    structured = json_books(soup, document.final_url)
    base = soup.find("base", href=True)
    resource_base = urljoin(document.final_url, base["href"]) if base else document.final_url
    if not valid_url(resource_base):
        resource_base = document.final_url
    requires_js = any(re.search(r"javascript", node.get_text(), re.I)
                      and re.search(r"включ|enable|required|требуется", node.get_text(), re.I)
                      for node in soup.select("noscript"))
    has_scripts = soup.select_one("script:not([type='application/ld+json'])") is not None
    executable_scripts = any(
        str(script.get("type", "")).casefold() in {"", "module", "text/javascript", "application/javascript"}
        and not re.search(r"(?:analytics|googletagmanager|metrika)", script.get("src", ""), re.I)
        for script in soup.select("script"))
    has_styles = soup.select_one('style, link[rel="stylesheet"]') is not None
    has_styles = has_styles or any(
        re.search(r"var\(|calc\(|env\(|\b(?:inherit|revert)\b", value)
        for node in soup.select('[style]') for key, value in inline_styles(node).items()
        if key in {"display", "visibility", "opacity", "content-visibility"})
    visible = compact(soup.get_text(" ", strip=True))
    requires_js = requires_js or bool(
        has_scripts and re.search(r"javascript", visible, re.I)
        and re.search(r"включ|enable|динамическ|(?:содержим|content).{0,80}(?:загру|load)", visible, re.I))
    if re.search(r"service.*unavailable|bad gateway|gateway time.?out|ошибка\s*50[234]", page.title, re.I):
        page.issues.append("service_error")
    if (re.search(r"загрузка|подождите|секундочку|loading|please wait", page.title, re.I)
        and len(visible) < 2000 and re.search(r"javascript|автоматически|подождите|секундочку|loading|please wait", visible, re.I)):
        page.issues.append("loading_shell")
    challenge = (r"just a moment|access denied|captcha|verify.*human|checking your browser|"
                 r"доступ ограничен|проверка браузера|подтвердите.{0,40}человек")
    if re.search(challenge, page.title, re.I) or (
        len(visible) < 2000 and re.search(challenge, visible, re.I)
    ):
        page.issues.append("access_challenge")
    if soup.select_one('input[name="captcha"], .g-recaptcha, .h-captcha'):
        if "access_challenge" not in page.issues:
            page.issues.append("access_challenge")
        page.blocking_issues.append("access_challenge")
    if soup.select_one('input[type="password"]') and (
        len(visible) < 500 or re.search(r"sign in|log in|авторизац|вход", page.title, re.I)
    ):
        page.issues.append("login_required")
    for element in list(soup.select("script, style, noscript, nav, footer, template")):
        element.decompose()
    if soup.head:
        soup.head.decompose()  # A title in <head> is not the main text of an otherwise empty page.
    noise = re.compile(r"(?:^|[-_\s])(related|recommend\w*|similar|advert\w*|ads|banner|comments?|reviews?|bibliography)(?:$|[-_\s])", re.I)
    for element in list(soup.find_all(True)):
        if element.attrs is None:
            continue
        marker = " ".join([str(element.get("id", "")), " ".join(element.get("class", [])),
                           str(element.get("itemprop", "")), str(element.get("role", ""))])
        hidden = element.has_attr("hidden") or element.get("aria-hidden") == "true"
        declarations = inline_styles(element)
        hidden = hidden or declarations.get("display") == "none" or declarations.get("visibility") in {"hidden", "collapse"}
        hidden = hidden or declarations.get("content-visibility") == "hidden" or bool(re.fullmatch(
            r"[+-]?(?:0+(?:\.0*)?|\.0+)%?", declarations.get("opacity", "-1")))
        removable_noise = (noise.search(marker) and element.name not in {"html", "body", "main"}
                           and element.find("main") is None)
        if hidden or removable_noise:
            element.decompose()
    document_root = soup.body or soup
    main = soup.find("main") or document_root
    page.h1 = unique(element.get_text(" ", strip=True)[:400] for element in main.find_all("h1"))[:5]
    primary_title = page.h1[0] if page.h1 else page.metadata.get("og:title", page.title)
    # Only the HTML <title> fallback may carry an independently identified site suffix.
    if not page.h1 and "og:title" not in page.metadata:
        book_title, separator, site_title = primary_title.rpartition("|")
        site_name = page.metadata.get("og:site_name", "")
        if separator and book_title.strip() and site_name and normalize_identity(site_title) == normalize_identity(site_name):
            primary_title = compact(book_title)
    primary_node = main
    heading = main.find("h1")
    if heading:
        for ancestor in heading.parents:
            if ancestor is main:
                break
            if book_boundary(ancestor):
                primary_node = ancestor
                break
    author_facts = []
    page.authors = node_authors(primary_node, primary_title, facts=author_facts) or unique([page.metadata.get("book:author", ""),
        page.metadata.get("author", "") if page.metadata.get("og:type", "").casefold() != "article" else ""])
    if not page.authors:
        for byline in primary_node.find_all(["h2", "h3"]):
            previous = byline.find_previous(["h1", "h2", "h3"])
            value = compact(byline.get_text(" ", strip=True))
            if (previous and identity_heading(previous.get_text()) == identity_heading(primary_title)
                and 2 <= len(text_key(value).split()) <= 4
                and identity_heading(value) != identity_heading(primary_title)
                and f" {text_key(value)} " in f" {text_key(page.title)} "):
                page.authors.append(value)
    if page.authors and not author_facts:
        author_facts = [{"name": author, "role": "page_metadata_or_heading",
                         "scope_locator": dom_locator(primary_node)} for author in page.authors]
    page.text = main.get_text("\n", strip=True)[:limits.max_text_chars]
    page.partial = len(main.get_text(" ", strip=True)) > limits.max_text_chars or _http_partial(document)
    page.is_collection = bool(re.search(r"сборник|собрание|подборка|каталог|все книги|collection|anthology",
                                        primary_title, re.I))
    audio_claim = bool(re.search(r"аудиокниг|audiobook", page.title, re.I)) and not page.is_collection
    page.signals = content_signals(main, document.final_url, primary_title, audio_claim=audio_claim,
                                   authors=page.authors, resource_base=resource_base)
    scoped_signals = page.signals if primary_node is main else content_signals(
        primary_node, document.final_url, primary_title, audio_claim=audio_claim, authors=page.authors,
        resource_base=resource_base)
    primary_signals = [signal for signal in scoped_signals if signal["binding"] == "bound"]
    unresolved_signals = [signal for signal in scoped_signals if signal["binding"] == "unresolved"]
    if page.is_collection and page.authors and any(
        signal["kind"] in {"reader_text", "reading_link", "download_link", "audio_source", "audio_playlist"} or (signal["kind"] == "file_link" and re.search(
            r"\.(?:epub|fb2|pdf|mobi|azw3|djvu|txt)(?:$|[?#])", signal["url"], re.I)) for signal in primary_signals
    ):
        page.is_collection = False  # A directly bound book file also supports a book named "Сборник рассказов".
    for link in main.select("a[href]"):
        label = compact(link.get_text(" ", strip=True))
        if re.search(r"читать|скачать|слушать|download|read online", label, re.I):
            page.interaction_hints.append({"label": label[:100],
                                           "url": urljoin(resource_base, link["href"])[:1500]})
    page.interaction_hints = page.interaction_hints[:15]
    if audio_claim:
        page.detected_formats.append("audio")
    page.detected_formats = unique(page.detected_formats + [signal["format"] for signal in primary_signals])
    page.subjects.append({"title": primary_title, "authors": page.authors, "source": "primary",
                          "author_facts": author_facts,
                          "scope_id": id(primary_node),
                          "scope_is_book": book_boundary(primary_node),
                          "scope_ancestors": [id(parent) for parent in primary_node.parents],
                          "scope_locator": dom_locator(primary_node),
                          "scope": "collection" if page.is_collection else "main",
                          "signals": [] if page.is_collection else primary_signals,
                          "unresolved_signals": [] if page.is_collection else unresolved_signals,
                          "detected_formats": [] if page.is_collection else page.detected_formats})
    if page.is_collection:
        page.subjects.extend(collection_members(main, primary_title, page.authors, document.final_url, resource_base))
    cards = list(document_root.select("article, .book-card, .book-item, [itemtype$='/Book']"))
    cards += [heading.parent for heading in document_root.find_all(re.compile(r"^h[1-6]$"))
              if heading.parent is not main and heading.parent is not document_root]
    seen = set()
    for node in cards:
        if id(node) in seen:
            continue
        if len(seen) == 150:
            page.partial = True
            break
        seen.add(id(node))
        heading = local_heading(node) or node.select_one("[itemprop='name']")
        if heading:
            title = compact(heading.get_text(" ", strip=True))[:400]
            author_facts = []
            authors = node_authors(node, title, facts=author_facts)
            signals = content_signals(node, document.final_url, title, authors=authors, resource_base=resource_base)
            if node is not main and not any(parent is main for parent in node.parents):
                page.signals.extend(signals)  # Preserve observations of independent cards outside main.
            bound = [signal for signal in signals if signal["binding"] == "bound"]
            page.subjects.append({"title": title,
                                  "author_facts": author_facts,
                                  "authors": authors, "source": "book_card", "scope": "card",
                                  "scope_id": id(node),
                                  "scope_is_book": book_boundary(node),
                                  "scope_ancestors": [id(parent) for parent in node.parents],
                                  "scope_locator": dom_locator(node),
                                  "signals": bound,
                                  "unresolved_signals": [signal for signal in signals if signal["binding"] == "unresolved"],
                                  "detected_formats": unique(signal["format"] for signal in bound)})
    dom_scopes = {}
    for subject in page.subjects:
        if subject["source"] in {"primary", "book_card"}:
            dom_scopes.setdefault(subject["scope_id"], subject)
    for book in structured:
        author_unresolved = book.pop("_author_unresolved", False)
        scope = page.subjects[0]
        owner_id = book.pop("_scope_id", None)
        if owner_id is not None:
            scope_known = owner_id in dom_scopes
            scope = dom_scopes.get(owner_id, scope)
        else:
            candidates = [subject for subject in dom_scopes.values()
                          if identity_heading(subject["title"]) == identity_heading(book["title"])
                          and not title_punctuation_conflict(book["title"], subject["title"])]
            authored = [subject for subject in candidates if authors_match(book["authors"], subject["authors"])]
            candidates = authored if authored else candidates
            scope_known = len(candidates) == 1 or (not candidates and sum(
                bool(subject["scope_is_book"]) for subject in dom_scopes.values()) <= 1)
            if len(candidates) == 1:
                scope = candidates[0]
        scope_title, scope_authors = scope["title"], scope["authors"]
        first, second = identity_heading(scope_title), identity_heading(book["title"])
        same_title = bool(first and first == second and not title_punctuation_conflict(book["title"], scope_title))
        if not same_title and scope_authors and book["authors"] and authors_match(scope_authors, book["authors"]):
            same_title = bool(title_byline_role(scope_title, book["title"], scope_authors))
        issues = []
        scoped_book = scope_known and scope.get("scope_is_book")
        if book["primary_metadata"] and (not page.is_collection or scoped_book) and scope_title:
            if first and second and not same_title:
                issues.append("metadata_title_conflict")
        same_author = not author_unresolved and (
            not book["authors"] or not scope_authors or authors_match(book["authors"], scope_authors))
        if book["primary_metadata"] and same_title and not same_author:
            issues.append("author_missing" if author_unresolved else "metadata_author_conflict")
        for issue in issues:
            page.issues.append(issue)
            page.issue_scopes.setdefault(issue, []).append({
                "scope_id": scope["scope_id"] if scope_known else None,
                "scope_is_book": bool(scoped_book), "scope_ancestors": scope.get("scope_ancestors", []),
                "title": scope_title if scope_known else "не установлена",
                "scope_locator": scope.get("scope_locator", "") if scope_known else "",
                "metadata_title": book["title"], "metadata_authors": book["authors"]})
        use_primary_scope = (book["primary_metadata"] and scope_known and (not page.is_collection or scoped_book)
                             and same_title and same_author)
        page.subjects.append({**book, "source": "jsonld_book",
                              "scope": "metadata" if book["primary_metadata"] else "recommendation",
                              "scope_id": scope.get("scope_id") if book["primary_metadata"] and scope_known else None,
                              "scope_is_book": bool(scoped_book),
                              "scope_ancestors": scope.get("scope_ancestors", []),
                              "scope_locator": scope.get("scope_locator", "") if scope_known else "",
                              "signals": scope["signals"] if use_primary_scope else [],
                              "unresolved_signals": scope.get("unresolved_signals", []) if use_primary_scope else [],
                              "detected_formats": scope.get("detected_formats", []) if use_primary_scope else []})
    if audio_claim and not any(signal["format"] == "audio" for signal in primary_signals) and main.select_one('[id*="player"]'):
        page.issues.append("content_requires_js")
    if (not page.is_collection and not primary_signals and re.search(r"читать|read online", page.title, re.I)
        and (page.authors or main.select_one('.block_read_text, .reader-text, #reader'))):
        page.issues.append("content_requires_js")
    if (not primary_signals and page.authors
        and (requires_js or (has_scripts and main.select_one(
            '.block_read_text, .reader-text, .book-text, #reader, #book-content-container, [data-reader-content]')))):
        page.issues.append("content_requires_js")
    for subject in page.subjects:
        subject["requires_js"] = bool(executable_scripts and subject["authors"] and not subject["signals"])
    page.dynamic_pending = bool(executable_scripts and not any(subject["authors"] for subject in page.subjects)
                                and (requires_js or main.select_one('div:empty, section:empty, main:empty, [role="application"]')))
    # Observe the mount's state, independently of ready neighbouring books.
    # Empty decorative children and a loading status are still an unfinished app.
    for mount in document_root.select('#app, #root, [data-reactroot], [role="application"], [aria-busy="true"]'):
        text = compact(mount.get_text(" ", strip=True))
        loading = not text or bool(re.fullmatch(r"(?:loading|please wait|загрузка|подождите)[\s.…!]*", text, re.I))
        pending = mount.get("aria-busy") == "true" or (loading and local_heading(mount) is None)
        page.dynamic_pending = page.dynamic_pending or bool(executable_scripts and pending)
    if page.dynamic_pending or any(subject["requires_js"] for subject in page.subjects):
        page.issues.append("content_requires_js")
    if has_styles and not document.visibility_checked and any(subject["signals"] for subject in page.subjects):
        page.issues.append("visibility_unverified")
    if len(compact(page.text)) < 40:
        page.issues.append("insufficient_html")
    return page


def pdf_primary_opening(opening):
    """Choose the document opening before testing a catalogue identity."""
    starts = [start for start in opening.split("\f") if compact(start)]
    return next((start for start in starts if len(start.splitlines()) >= 2), starts[0] if starts else "")


def pdf_identity_matches(opening, title, authors=()):
    """Corroborate the primary title/byline, never a more convenient later advertisement."""
    expected_title = title_key(title)
    names = {variant for author in authors for variant in (
        text_key(author), " ".join(reversed(text_key(author).split())))}
    for start in [pdf_primary_opening(opening)]:
        raw_lines = [line for line in start.splitlines() if compact(line)][:6]
        lines = [re.sub(r"^(?:title|author|by|название|автор)\s+", "", text_key(line))
                 for line in raw_lines]
        # A short series heading can precede the byline and a wrapped title.
        offsets = [0, 1] if lines and len(lines[0].split()) <= 10 and not re.search(
            r"recommend|bibliograph|also read|рекоменд|библиограф|другие книги|содержание|contents", lines[0]) else [0]
        for offset in offsets:
            for count in range(offset + 1, len(lines) + 1):
                prefix = " ".join(lines[offset:count])
                candidates = set() if names else {prefix}
                # Match the author literally before normalizing only the title's number.
                for name in names:
                    for suffix in (" " + name, " by " + name):
                        if prefix.endswith(suffix):
                            candidates.add(prefix[:-len(suffix)])
                    if prefix.startswith(name + " "):
                        candidates.add(prefix[len(name) + 1:])
                if (any(title_key(candidate) == expected_title for candidate in candidates)
                    and not title_punctuation_conflict(title, "\n".join(raw_lines[offset:count]))):
                    return True
    return False


def _pdf_document_role(text, subject, identity_lines):
    """Use document labels and front matter, not isolated words inside a chapter."""
    roles = {
        "review": r"book\s+review|review\s+of|critical\s+(?:review|analysis|study)|рецензия|"
                  r"критический\s+(?:разбор|обзор)|критическое\s+исследование",
        "study_guide": r"study[-\s]+guide|reading[-\s]+guide|учебное\s+пособие|учебное\s+руководство",
        "summary": r"(?:plot[-\s]+)?summary\s+of\s+(?:the\s+)?(?:novel|book|work)(?=\s*(?:$|[:;.]))|"
                   r"plot[-\s]+summary(?=\s*(?:$|[:;.]))|"
                   r"(?:краткое\s+(?:содержание|изложение)|пересказ)\s+(?:романа|книги|произведения)(?=\s*(?:$|[:;.]))",
    }
    companion_headings = {
        "summary and analysis": "analysis", "themes and analysis": "analysis", "character analysis": "analysis",
        "discussion questions": "questions", "questions for discussion": "questions", "study questions": "questions",
    }
    front_matter = True
    hints = {}
    for index, line in enumerate([subject, *text.splitlines()]):
        key = text_key(line)
        if not key or key in identity_lines:
            continue
        metadata = index == 0
        if not metadata and re.match(r"^(?:chapter|глава)\s+(?:\d+|[ivxlcdm]+|one|first|первая)\b", key):
            front_matter = False
        statement = re.match(
            r"\s*(?:this (?:document|pdf|file) (?:is|provides|contains|presents)|document type\s*:)\s+(.+)",
            line, re.I)
        if metadata or front_matter or statement:
            label = statement[1] if statement else line
            if not statement:
                label = re.sub(r"^\s*this\s+(?:is\s+)?", "", label, flags=re.I)
            for role, pattern in roles.items():
                if re.match(r"\s*(?:an?\s+)?(?:" + pattern + r")\b", label, re.I):
                    return role, compact(line)[:240]
        if front_matter and not metadata and key in companion_headings:
            hints[companion_headings[key]] = compact(line)
    if len(hints) >= 2:
        return "unresolved", "; ".join(hints.values())[:240]
    return "", ""


def extract_pdf(document, limits):
    page = Page(kind="pdf")
    texts = []
    try:
        reader = PdfReader(document.pdf_stream or BytesIO(document.body), strict=bool(document.pdf_stream))
        if reader.is_encrypted and not reader.decrypt(""):
            page.issues.append("encrypted_pdf")
            return page
        page.metadata = {str(key): str(value) if str(key) == "/Author" else str(value)[:1000]
                         for key, value in (reader.metadata or {}).items()}
        page.title = page.metadata.get("/Title", "")
        page.authors = [page.metadata["/Author"]] if page.metadata.get("/Author") else []
        if document.pdf_stream:
            root = reader.trailer["/Root"]["/Pages"]
            page.pdf_page_count = int(root.get("/Count", 0))

            def first_pages(reference, inherited=None):
                obj = reference.get_object()
                attrs = dict(inherited or {})
                for name in ("/Resources", "/MediaBox", "/CropBox", "/Rotate"):
                    if name in obj:
                        attrs[name] = obj.raw_get(name)
                if obj.get("/Type") == "/Page":
                    child = PageObject(reader, reference)
                    for key, value in attrs.items():
                        if key not in child:
                            child[key] = value
                    yield child
                else:
                    for child in obj.get("/Kids", []):
                        yield from first_pages(child, attrs)

            pages = islice(first_pages(root), min(3, limits.pdf_max_pages))
        else:
            page.pdf_page_count = len(reader.pages)
            pages = reader.pages[:limits.pdf_max_pages]
        for pdf_page in pages:
            text = pdf_page.extract_text() or ""
            texts.append(text)
            page.pdf_pages_read += 1
            if sum(map(len, texts)) >= limits.max_text_chars:
                break
    except (PDFPreviewLimit, requests.RequestException, PdfReadError, ValueError) as error:
        if not document.pdf_stream:
            raise
        page.partial = True
        page.issues.append("pdf_preview_incomplete")
        page.metadata["preview_error"] = f"{type(error).__name__}: {str(error)[:200]}"
    finally:
        if document.pdf_stream:
            document.size_bytes = document.pdf_stream.total
            page.metadata["preview_bytes"] = str(document.pdf_stream.total)
            page.metadata["preview_total_bytes"] = str(document.pdf_stream.size)
            page.metadata["preview_limit_reached"] = str(document.pdf_stream.limit_reached).lower()
            if document.pdf_stream.owns_session:
                document.pdf_stream.session.close()
    page.text = "\n".join(texts)[:limits.max_text_chars]
    # Preserve page boundaries: only each page's title/byline prefix identifies it.
    page.opening = "\f".join("\n".join(line for line in text.splitlines() if compact(line))[:1000]
                             for text in texts[:3])[:4000]
    page.partial = page.partial or page.pdf_pages_read < page.pdf_page_count or sum(map(len, texts)) > limits.max_text_chars
    if len(compact(page.text)) < 100:
        page.issues.append("pdf_no_text")
    if len(compact(page.text)) >= 1200 and page.pdf_pages_read >= 2:
        page.signals = [{"kind": "pdf_text", "format": "ebook", "url": document.final_url,
                         "detail": f"Из PDF извлечено {len(page.text)} символов, "
                                   f"страниц {page.pdf_pages_read}/{page.pdf_page_count}; полнота книги не установлена."}]
    page.subjects = [{"title": page.title, "authors": page.authors, "source": "pdf_metadata",
                      "scope": "pdf", "signals": page.signals}]
    # Metadata identifies a file only when the opening text corroborates it.
    # Missing corroboration may mean stale metadata or another work, not proof of placement.
    corroborated = pdf_identity_matches(page.opening, page.title, page.authors) if page.title else (
        not page.authors or any(text_key(author) in {
            text_key(line) for line in pdf_primary_opening(page.opening).splitlines()[:2]
        } for author in page.authors))
    if not corroborated:
        page.issues.append("pdf_identity_unresolved")
        page.signals.clear()
    identity_lines = {text_key(page.title), *(text_key(author) for author in page.authors)}
    role, role_evidence = _pdf_document_role("\n".join(texts[:3])[:limits.max_text_chars],
                                             page.metadata.get("/Subject", ""), identity_lines)
    if role:
        page.metadata["document_role"] = role
        page.metadata["document_role_evidence"] = role_evidence
        page.issues.append("pdf_document_role_unresolved")
        for signal in page.signals:
            signal.update(binding="unresolved", binding_reason="Роль PDF не подтверждает текст произведения: " + role_evidence)
        page.subjects[0]["signals"] = []
        page.subjects[0]["unresolved_signals"] = page.signals
    return page


def _http_partial(document):
    """A 206 HTML fragment is complete only with a consistent full byte range."""
    if document.http_status != 206:
        return False
    headers = {key.lower(): value for key, value in document.headers.items()}
    found = re.fullmatch(r"bytes\s+0-(\d+)/(\d+)", headers.get("content-range", "").strip(), re.I)
    # Missing, malformed and unknown totals do not prove completeness.
    complete = bool(found and int(found[1]) + 1 == int(found[2]) > 0)
    if complete and headers.get("content-encoding", "identity").casefold() in {"", "identity"}:
        complete = len(document.body) == int(found[2])
    return not complete


def extract_page(document, limits=Limits()):
    if document.error_code or not document.body:
        return Page(issues=["not_loaded"], partial=_http_partial(document))
    content_type = next((value for key, value in document.headers.items()
                         if key.lower() == "content-type"), "").casefold()
    try:
        if document.body.lstrip().startswith(b"%PDF-") or "application/pdf" in content_type:
            return extract_pdf(document, limits)
        if "html" in content_type or (not content_type and document.body.lstrip().startswith(b"<")):
            return extract_html(document, limits)
        return Page(issues=["unsupported_content_type"])
    except Exception as error:
        # A malformed document must not abort the batch or become a negative result.
        return Page(issues=["extraction_error"], metadata={"error": f"{type(error).__name__}: {str(error)[:300]}"})
