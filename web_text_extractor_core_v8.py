#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web_text_extractor_core_v8.py

Core library for Streamlit Web Text Extractor v8.

Main features:
- Truyenfull-like discovery:
  #total-page -> /trang-2/ ... /trang-N/
  #list-chapter / .list-chapter -> chapter URLs
  .chapter-c / #chapter-c -> chapter content
- URL fragment removal: /trang-2/#list-chapter -> /trang-2/
- Manual EPUB generator, no EbookLib dependency
- Atomic per-chapter JSON output support
"""

from __future__ import annotations

import html as html_lib
import json
import random
import re
import time
import unicodedata
import uuid
import zipfile
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import urljoin, urlparse, urldefrag

import requests
import urllib3
from bs4 import BeautifulSoup
from bs4.element import Tag


@dataclass
class ExtractedPage:
    url: str
    title: str
    book_title: str
    chapter_title: str
    content: str
    next_url: str = ""
    source_selector: str = ""
    content_chars: int = 0


@dataclass
class CrawlResult:
    novel_url: str
    title: str
    author: str = ""
    index_pages: list[str] = field(default_factory=list)
    chapter_urls: list[str] = field(default_factory=list)
    chapters: list[ExtractedPage] = field(default_factory=list)
    failed_urls: list[dict] = field(default_factory=list)


ProgressCallback = Optional[Callable[[str, int, int, str], None]]


# =========================
# URL helpers
# =========================

def remove_url_fragment(url: str) -> str:
    clean_url, _fragment = urldefrag(url)
    return clean_url


def normalize_url(url: str) -> str:
    url = remove_url_fragment(url or "").strip()
    if not url:
        return url

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = re.sub(r"/{2,}", "/", parsed.path)

    return parsed._replace(
        scheme=scheme,
        netloc=netloc,
        path=path,
        fragment="",
    ).geturl()


def normalize_novel_url(url: str) -> str:
    url = normalize_url(url)
    parsed = urlparse(url)
    path = parsed.path

    path = re.sub(r"/trang-\d+/?$", "/", path, flags=re.IGNORECASE)
    path = re.sub(r"/chuong-[^/]+/?$", "/", path, flags=re.IGNORECASE)
    path = re.sub(r"/{2,}", "/", path)

    if not path.endswith("/"):
        path += "/"

    return parsed._replace(path=path, query="", fragment="").geturl()


def is_probably_chapter_url(url: str, novel_url: str) -> bool:
    url = normalize_url(url)
    novel_url = normalize_novel_url(novel_url)

    parsed = urlparse(url)
    novel_path = urlparse(novel_url).path.rstrip("/") + "/"

    if parsed.netloc.lower() != urlparse(novel_url).netloc.lower():
        return False

    if not parsed.path.startswith(novel_path):
        return False

    return "/chuong-" in parsed.path.lower()


def is_probably_index_page(url: str, novel_url: str) -> bool:
    url = normalize_url(url)
    novel_url = normalize_novel_url(novel_url)

    parsed = urlparse(url)
    novel_parsed = urlparse(novel_url)
    novel_path = novel_parsed.path.rstrip("/") + "/"

    if parsed.netloc.lower() != novel_parsed.netloc.lower():
        return False

    if parsed.path == novel_path:
        return True

    return bool(
        parsed.path.startswith(novel_path)
        and re.search(r"/trang-\d+/?$", parsed.path, flags=re.IGNORECASE)
    )


def build_truyenfull_index_page_url(novel_url: str, page_no: int) -> str:
    base = normalize_novel_url(novel_url).rstrip("/") + "/"

    if page_no <= 1:
        return normalize_url(base)

    return normalize_url(urljoin(base, f"trang-{page_no}/"))


def natural_chapter_key(url: str) -> tuple:
    path = urlparse(url).path.lower()
    match = re.search(r"/chuong-([^/]+)/?", path)

    if not match:
        return (10**12, url)

    slug = match.group(1)
    nums = re.findall(r"\d+", slug)

    if not nums:
        return (10**12, slug)

    return tuple(int(x) for x in nums)


# =========================
# Text and filename helpers
# =========================

def normalize_whitespace(text: str) -> str:
    text = (text or "").replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def strip_accents(value: str) -> str:
    value = value.replace("đ", "d").replace("Đ", "D")
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def slugify(value: str, max_len: int = 120, ascii_filename: bool = True) -> str:
    value = unicodedata.normalize("NFKC", value or "untitled")

    if ascii_filename:
        value = strip_accents(value)
        value = value.encode("ascii", "ignore").decode("ascii")
        value = re.sub(r"[^A-Za-z0-9\s.-]+", "", value)
    else:
        value = re.sub(r"[^\w\s.-]+", "", value, flags=re.UNICODE)

    value = re.sub(r"[\s_]+", "-", value).strip("-.").lower()
    return (value[:max_len].strip("-.") or "untitled")


def text_from_html_block(block: Tag) -> str:
    for node in list(block.select("script, style, noscript, iframe, form, button, select, input")):
        node.decompose()

    for node in list(block.find_all(True)):
        if not isinstance(node, Tag):
            continue

        node_id = (node.get("id") or "").lower()
        node_class = " ".join(node.get("class") or []).lower()

        if (
            node_id.startswith("ads")
            or "ads" in node_id
            or "ad-" in node_id
            or "ads" in node_class
            or "advert" in node_class
            or "unlock" in node_id
            or "unlock" in node_class
            or "comment" in node_id
            or "comment" in node_class
        ):
            node.decompose()

    for br in block.find_all("br"):
        br.replace_with("\n")

    for tag_name in ["p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "li"]:
        for node in block.find_all(tag_name):
            node.insert_before("\n")
            node.insert_after("\n")

    text = block.get_text("\n", strip=False)

    lines: list[str] = []
    for line in text.splitlines():
        line = normalize_whitespace(line)
        if line:
            lines.append(line)
        else:
            if lines and lines[-1] != "":
                lines.append("")

    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# =========================
# HTTP
# =========================

def build_session(
    user_agent: str = "",
    verify_ssl: bool = True,
    ca_bundle: str = "",
) -> requests.Session:
    session = requests.Session()

    ua = user_agent or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    session.headers.update({
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    })

    if ca_bundle:
        session.verify = ca_bundle
    else:
        session.verify = verify_ssl

    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    return session


def fetch_html(
    session: requests.Session,
    url: str,
    timeout: int = 30,
    retries: int = 3,
    sleep_on_retry: float = 2.0,
) -> str:
    url = normalize_url(url)
    last_error: Optional[Exception] = None

    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()

            if response.apparent_encoding:
                response.encoding = response.apparent_encoding

            return response.text

        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(sleep_on_retry * attempt)

    raise RuntimeError(f"Failed to fetch {url}: {last_error}") from last_error


# =========================
# HTML parsing
# =========================

def pick_first_text(soup: BeautifulSoup, selectors: Iterable[str]) -> str:
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            text = normalize_whitespace(node.get_text(" ", strip=True))
            if text:
                return text
    return ""


def pick_meta_content(soup: BeautifulSoup, attrs_list: Iterable[dict]) -> str:
    for attrs in attrs_list:
        node = soup.find("meta", attrs=attrs)
        if node and node.get("content"):
            return normalize_whitespace(node["content"])
    return ""


def make_soup(html: str) -> BeautifulSoup:
    # html.parser is slower than lxml but avoids some lxml "Document is empty" edge cases.
    return BeautifulSoup(html or "", "html.parser")


def parse_index_metadata(html: str, url: str) -> tuple[str, str]:
    soup = make_soup(html)

    title = (
        pick_meta_content(soup, [{"property": "og:title"}, {"name": "title"}])
        or pick_first_text(soup, ["h3.title[itemprop='name']", "h1", ".title"])
        or (soup.title.get_text(" ", strip=True) if soup.title else "")
        or "Untitled"
    )

    author = ""
    info_text = pick_first_text(soup, [".info", ".info-holder .info"])
    if info_text:
        m = re.search(
            r"Tác giả\s*[:：]?\s*([^\n\r]+?)(?:\s*(Thể loại|Nguồn|Trạng thái|Đánh giá|$))",
            info_text,
            flags=re.I,
        )
        if m:
            author = normalize_whitespace(m.group(1))

    if not author:
        author_url = pick_meta_content(soup, [{"property": "book:author"}])
        if author_url:
            author = urlparse(author_url).path.strip("/").split("/")[-1].replace("-", " ").title()

    return title, author


def parse_chapter_metadata(soup: BeautifulSoup, url: str) -> tuple[str, str, str]:
    book_title = pick_first_text(soup, [".truyen-title", ".breadcrumb h1 a", "h1 a[itemprop='item']"])
    chapter_title = pick_first_text(soup, [".chapter-title", "h2 .chapter-title", "h2"])

    page_title = normalize_whitespace(soup.title.get_text(" ", strip=True)) if soup.title else ""

    if not book_title:
        book_title = pick_meta_content(soup, [{"property": "og:site_name"}]) or ""

    if not chapter_title:
        chapter_title = pick_meta_content(soup, [{"property": "og:title"}]) or ""

    title = " - ".join(x for x in [book_title, chapter_title] if x).strip(" -")
    if not title:
        title = page_title or url

    return title, book_title, chapter_title


CONTENT_SELECTORS = [
    "#chapter-c",
    ".chapter-c",
    "[itemprop='articleBody']",
    "#chapter-content",
    ".chapter-content",
    ".reading-content",
    "article",
    "main",
    "[role='main']",
    ".entry-content",
    ".post-content",
    ".article-content",
    ".post-body",
    ".content",
    "#content",
]


def select_best_content_block(soup: BeautifulSoup) -> tuple[Optional[Tag], str]:
    for selector in CONTENT_SELECTORS:
        node = soup.select_one(selector)
        if isinstance(node, Tag):
            text = normalize_whitespace(node.get_text(" ", strip=True))
            if len(text) >= 50:
                return node, selector

    candidates: list[tuple[int, Tag, str]] = []
    for selector in ["article", "main", "section", "div"]:
        for node in soup.find_all(selector):
            if not isinstance(node, Tag):
                continue

            node_id = (node.get("id") or "").lower()
            node_class = " ".join(node.get("class") or []).lower()

            if any(x in node_id + " " + node_class for x in ["nav", "menu", "footer", "header", "comment", "sidebar", "ads"]):
                continue

            text = normalize_whitespace(node.get_text(" ", strip=True))
            if len(text) >= 200:
                candidates.append((len(text), node, f"fallback:{selector}"))

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1], candidates[0][2]

    return None, ""


def find_next_chapter_url(soup: BeautifulSoup, current_url: str) -> str:
    selectors = [
        "#next_chap",
        "a#next_chap",
        "a[rel='next']",
        ".chapter-nav a[href*='chuong-']",
    ]

    for selector in selectors:
        for a in soup.select(selector):
            if not isinstance(a, Tag):
                continue
            href = a.get("href")
            if not href:
                continue

            text = normalize_whitespace(a.get_text(" ", strip=True)).lower()
            node_id = (a.get("id") or "").lower()
            rel = " ".join(a.get("rel") or []).lower()

            if "next" in node_id or "next" in rel or "tiếp" in text or "sau" in text or ">" in text:
                return normalize_url(urljoin(current_url, href))

    return ""


def extract_from_html(html: str, url: str = "") -> ExtractedPage:
    soup = make_soup(html)
    title, book_title, chapter_title = parse_chapter_metadata(soup, url)

    block, selector = select_best_content_block(soup)
    if block is None:
        raise ValueError(f"Cannot find main content block in {url or '<local file>'}")

    content = text_from_html_block(block)
    next_url = find_next_chapter_url(soup, url) if url else ""

    return ExtractedPage(
        url=normalize_url(url) if url else "",
        title=title,
        book_title=book_title,
        chapter_title=chapter_title,
        content=content,
        next_url=next_url,
        source_selector=selector,
        content_chars=len(content),
    )


# =========================
# Chapter list discovery
# =========================

def find_total_index_pages(html: str) -> int:
    soup = make_soup(html)

    total_page_node = soup.select_one("#total-page")
    if total_page_node:
        raw = (total_page_node.get("value") or "").strip()
        try:
            total_pages = int(raw)
            if total_pages >= 1:
                return total_pages
        except ValueError:
            pass

    nums: list[int] = []
    for a in soup.select(".pagination a, ul.pagination a, .page-nav a, a[href*='trang-']"):
        label = normalize_whitespace(a.get_text(" ", strip=True))
        if label.isdigit():
            nums.append(int(label))

        href = a.get("href") or ""
        m = re.search(r"/trang-(\d+)/?", href, flags=re.I)
        if m:
            nums.append(int(m.group(1)))

    return max(nums) if nums else 1


def find_index_pagination_urls(html: str, page_url: str, novel_url: str) -> list[str]:
    urls: OrderedDict[str, None] = OrderedDict()

    total_pages = find_total_index_pages(html)
    if total_pages > 1:
        for page_no in range(1, total_pages + 1):
            urls[build_truyenfull_index_page_url(novel_url, page_no)] = None
        return list(urls.keys())

    soup = make_soup(html)

    nodes: list[Tag] = []
    for selector in [
        ".pagination a",
        "ul.pagination a",
        ".page-nav a",
        ".chapter-list-pagination a",
        "a[href*='page=']",
        "a[href*='trang-']",
    ]:
        nodes.extend([node for node in soup.select(selector) if isinstance(node, Tag)])

    for a in nodes:
        href = a.get("href")
        if not href or href.lower().startswith(("javascript:", "#", "mailto:")):
            continue

        abs_url = normalize_url(urljoin(page_url, href))
        if is_probably_index_page(abs_url, novel_url):
            urls[abs_url] = None

    if not urls:
        urls[normalize_novel_url(novel_url)] = None

    return list(urls.keys())


def find_chapter_urls_in_index(html: str, page_url: str, novel_url: str) -> list[str]:
    soup = make_soup(html)
    urls: OrderedDict[str, None] = OrderedDict()

    selectors = [
        "#list-chapter a[href]",
        ".list-chapter a[href]",
        ".l-chapters a[href]",
        "a[href*='/chuong-']",
    ]

    for selector in selectors:
        for a in soup.select(selector):
            if not isinstance(a, Tag):
                continue

            href = a.get("href")
            if not href:
                continue

            abs_url = normalize_url(urljoin(page_url, href))
            if is_probably_chapter_url(abs_url, novel_url):
                urls[abs_url] = None

    return list(urls.keys())


def discover_chapter_urls(
    novel_url: str,
    session: requests.Session,
    timeout: int = 30,
    retries: int = 3,
    delay: float = 0.5,
    max_index_pages: int = 0,
    progress_callback: ProgressCallback = None,
) -> tuple[str, str, list[str], list[str]]:
    novel_url = normalize_novel_url(novel_url)

    first_html = fetch_html(session, novel_url, timeout=timeout, retries=retries)
    title, author = parse_index_metadata(first_html, novel_url)

    index_pages = find_index_pagination_urls(first_html, novel_url, novel_url)

    if max_index_pages and max_index_pages > 0:
        index_pages = index_pages[:max_index_pages]

    chapter_urls: OrderedDict[str, None] = OrderedDict()

    for idx, index_url in enumerate(index_pages, start=1):
        clean_index_url = normalize_url(index_url)

        if idx == 1 and clean_index_url == normalize_url(novel_url):
            html = first_html
        else:
            if delay > 0:
                time.sleep(delay)
            html = fetch_html(session, clean_index_url, timeout=timeout, retries=retries)

        for chapter_url in find_chapter_urls_in_index(html, clean_index_url, novel_url):
            chapter_urls[chapter_url] = None

        if progress_callback:
            progress_callback("index", idx, len(index_pages), f"{clean_index_url} | chapters found: {len(chapter_urls)}")

    return title, author, index_pages, list(chapter_urls.keys())


# =========================
# File persistence
# =========================

def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def append_jsonl(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def chapter_json_path(chapter_dir: Path, global_index: int) -> Path:
    return chapter_dir / f"{global_index:06d}.json"


def save_chapter_json_atomic(chapter_dir: Path, global_index: int, page: ExtractedPage) -> Path:
    payload = asdict(page)
    payload["global_index"] = global_index
    path = chapter_json_path(chapter_dir, global_index)
    atomic_write_json(path, payload)
    return path


def load_saved_chapter_files(chapter_dir: Path) -> list[ExtractedPage]:
    if not chapter_dir.exists():
        return []

    rows: list[dict] = []
    for path in sorted(chapter_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            rows.append(data)
        except Exception:
            continue

    rows.sort(key=lambda x: int(x.get("global_index", 10**12)))

    pages: list[ExtractedPage] = []
    for row in rows:
        row = dict(row)
        row.pop("global_index", None)
        pages.append(ExtractedPage(**row))

    return pages


def saved_global_indices(chapter_dir: Path) -> set[int]:
    indices: set[int] = set()
    if not chapter_dir.exists():
        return indices

    for path in chapter_dir.glob("*.json"):
        try:
            indices.add(int(path.stem))
        except ValueError:
            pass

    return indices


# =========================
# Export TXT / EPUB
# =========================

def result_to_manifest_json(result: CrawlResult) -> str:
    payload = {
        "novel_url": result.novel_url,
        "title": result.title,
        "author": result.author,
        "index_pages_count": len(result.index_pages),
        "chapter_urls_count": len(result.chapter_urls),
        "chapters_extracted_count": len(result.chapters),
        "failed_count": len(result.failed_urls),
        "index_pages": result.index_pages,
        "chapter_urls": result.chapter_urls,
        "failed_urls": result.failed_urls,
        "chapters": [
            {
                "url": c.url,
                "title": c.title,
                "book_title": c.book_title,
                "chapter_title": c.chapter_title,
                "source_selector": c.source_selector,
                "content_chars": c.content_chars,
            }
            for c in result.chapters
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def write_book_txt(result: CrawlResult, out_dir: Path, ascii_filename: bool = True) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)

    title_slug = slugify(result.title or "book", ascii_filename=ascii_filename)
    path = out_dir / f"{title_slug}.txt"

    lines: list[str] = []
    lines.append(result.title or "Untitled")
    lines.append("=" * len(lines[0]))

    if result.author:
        lines.append(f"Tác giả: {result.author}")

    if result.novel_url:
        lines.append(f"Source: {result.novel_url}")

    lines.append("")
    lines.append("")

    for i, chapter in enumerate(result.chapters, start=1):
        heading = chapter.chapter_title or chapter.title or f"Chapter {i}"
        lines.append(heading)
        lines.append("-" * len(heading))

        if chapter.url:
            lines.append(f"Source: {chapter.url}")
            lines.append("")

        lines.append(chapter.content.strip())
        lines.append("")
        lines.append("")

    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return path


def _safe_xml_text(value: str) -> str:
    if not value:
        return ""
    return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", value)


def content_to_xhtml_paragraphs(content: str) -> str:
    paragraphs: list[str] = []
    raw_blocks = re.split(r"\n\s*\n", content.strip())

    for block in raw_blocks:
        block = block.strip()
        if not block:
            continue

        escaped = html_lib.escape(_safe_xml_text(block)).replace("\n", "<br/>")
        paragraphs.append(f"<p>{escaped}</p>")

    return "\n".join(paragraphs) or "<p></p>"


def _xhtml_document(title: str, body_html: str, language: str = "vi", with_epub_ns: bool = False) -> str:
    safe_title = html_lib.escape(_safe_xml_text(title))
    safe_lang = html_lib.escape(language or "vi")
    ns = ' xmlns:epub="http://www.idpf.org/2007/ops"' if with_epub_ns else ""

    return f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"{ns} xml:lang="{safe_lang}" lang="{safe_lang}">
<head>
  <meta charset="utf-8"/>
  <title>{safe_title}</title>
  <style>
    body {{ font-family: serif; line-height: 1.55; }}
    h1 {{ font-size: 1.35em; margin-bottom: 1em; }}
    p {{ margin: 0 0 1em 0; }}
  </style>
</head>
<body>
{body_html}
</body>
</html>"""


def write_book_epub(result: CrawlResult, out_dir: Path, ascii_filename: bool = True, language: str = "vi") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)

    title = _safe_xml_text(result.title or "Untitled")
    author = _safe_xml_text(result.author or "Unknown")
    title_slug = slugify(title, ascii_filename=ascii_filename)
    path = out_dir / f"{title_slug}.epub"

    uid = f"urn:uuid:{uuid.uuid4()}"
    lang = language or "vi"
    chapters = result.chapters or []

    xhtml_files: list[tuple[str, str, str, str]] = []

    intro_title = "Thong tin"
    intro_body = f"<h1>{html_lib.escape(title)}</h1>\n"
    if author:
        intro_body += f"<p><strong>Tac gia:</strong> {html_lib.escape(author)}</p>\n"
    if result.novel_url:
        intro_body += f"<p><strong>Source:</strong> {html_lib.escape(_safe_xml_text(result.novel_url))}</p>\n"
    intro_body += f"<p><strong>So chuong extract:</strong> {len(chapters)}</p>\n"
    xhtml_files.append(("intro", "intro.xhtml", intro_title, _xhtml_document(intro_title, intro_body, lang)))

    for i, chapter in enumerate(chapters, start=1):
        chapter_title = _safe_xml_text(chapter.chapter_title or chapter.title or f"Chapter {i}")
        body = f"<h1>{html_lib.escape(chapter_title)}</h1>\n"
        body += content_to_xhtml_paragraphs(chapter.content)
        href = f"chap_{i:04d}.xhtml"
        xhtml_files.append((f"chap_{i:04d}", href, chapter_title, _xhtml_document(chapter_title, body, lang)))

    nav_items = [
        f'<li><a href="{html_lib.escape(href)}">{html_lib.escape(display_title)}</a></li>'
        for _item_id, href, display_title, _content in xhtml_files
    ]

    nav_body = (
        '<nav epub:type="toc" id="toc">\n'
        '<h1>Muc luc</h1>\n'
        '<ol>\n'
        + "\n".join(nav_items)
        + "\n</ol>\n</nav>"
    )
    nav_xhtml = _xhtml_document("Table of Contents", nav_body, lang, with_epub_ns=True)

    manifest_items = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
    ]
    spine_items = []

    for item_id, href, _display_title, _content in xhtml_files:
        manifest_items.append(
            f'<item id="{item_id}" href="{html_lib.escape(href)}" media-type="application/xhtml+xml"/>'
        )
        spine_items.append(f'<itemref idref="{item_id}"/>')

    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">{html_lib.escape(uid)}</dc:identifier>
    <dc:title>{html_lib.escape(title)}</dc:title>
    <dc:language>{html_lib.escape(lang)}</dc:language>
    <dc:creator>{html_lib.escape(author)}</dc:creator>
    <meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>
  </metadata>
  <manifest>
    {chr(10).join(manifest_items)}
  </manifest>
  <spine>
    {chr(10).join(spine_items)}
  </spine>
</package>"""

    container_xml = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""

    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container_xml, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/nav.xhtml", nav_xhtml, compress_type=zipfile.ZIP_DEFLATED)

        for _item_id, href, _display_title, content in xhtml_files:
            z.writestr(f"OEBPS/{href}", content, compress_type=zipfile.ZIP_DEFLATED)

    return path


def write_result_files(
    result: CrawlResult,
    out_dir: Path,
    book_format: str = "both",
    ascii_filename: bool = True,
    language: str = "vi",
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    chapter_urls_path = out_dir / "chapter_urls.txt"
    chapter_urls_path.write_text("\n".join(result.chapter_urls) + "\n", encoding="utf-8")
    paths["chapter_urls"] = chapter_urls_path

    manifest_path = out_dir / "book_manifest.json"
    manifest_path.write_text(result_to_manifest_json(result), encoding="utf-8")
    paths["manifest"] = manifest_path

    if book_format in ("txt", "both"):
        paths["txt"] = write_book_txt(result, out_dir, ascii_filename=ascii_filename)

    if book_format in ("epub", "both"):
        paths["epub"] = write_book_epub(result, out_dir, ascii_filename=ascii_filename, language=language)

    return paths
