#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Streamlit UI for Web Text Extractor.

Run local:
    streamlit run streamlit_web_text_extractor_app.py

Deploy on Streamlit Cloud:
    - Put these files in a GitHub repo:
      streamlit_web_text_extractor_app.py
      web_text_extractor_v3_core.py
      requirements_streamlit_web_text_extractor.txt
    - Set main file = streamlit_web_text_extractor_app.py
"""

from __future__ import annotations

import io
import json
import tempfile
import zipfile
from pathlib import Path
from datetime import datetime

import streamlit as st

from web_text_extractor_v3_core import (
    CrawlResult,
    build_session,
    crawl_novel_to_result,
    crawl_urls_to_result,
    export_single_page_html,
    fetch_html,
    result_to_manifest_json,
    slugify,
    write_result_files,
)


st.set_page_config(
    page_title="Web Text Extractor",
    page_icon="📚",
    layout="wide",
)


def init_state() -> None:
    st.session_state.setdefault("logs", [])
    st.session_state.setdefault("last_result", None)
    st.session_state.setdefault("last_files", {})
    st.session_state.setdefault("last_zip", None)


def log_line(message: str) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    st.session_state.logs.append(f"[{now}] {message}")


def reset_logs() -> None:
    st.session_state.logs = []


def make_progress_callback(progress_bar, status_box):
    def _callback(stage: str, current: int, total: int, message: str) -> None:
        if total <= 0:
            pct = 0
        else:
            pct = min(current / total, 1.0)
        progress_bar.progress(pct)
        status_box.info(f"{stage}: {current}/{total} | {message}")
        log_line(f"{stage}: {current}/{total} | {message}")
    return _callback


def zip_paths(paths: dict[str, Path]) -> bytes:
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for key, path in paths.items():
            if path.is_dir():
                for child in path.rglob("*"):
                    if child.is_file():
                        z.write(child, arcname=str(child.relative_to(path.parent)))
            elif path.is_file():
                z.write(path, arcname=path.name)
    return mem.getvalue()


def render_downloads() -> None:
    files = st.session_state.get("last_files") or {}
    if not files:
        return

    st.subheader("Downloads")

    cols = st.columns(4)

    with cols[0]:
        txt = files.get("txt")
        if txt and Path(txt).exists():
            st.download_button(
                "Download TXT",
                data=Path(txt).read_bytes(),
                file_name=Path(txt).name,
                mime="text/plain",
            )

    with cols[1]:
        epub = files.get("epub")
        if epub and Path(epub).exists():
            st.download_button(
                "Download EPUB",
                data=Path(epub).read_bytes(),
                file_name=Path(epub).name,
                mime="application/epub+zip",
            )

    with cols[2]:
        manifest = files.get("manifest")
        if manifest and Path(manifest).exists():
            st.download_button(
                "Download Manifest JSON",
                data=Path(manifest).read_bytes(),
                file_name=Path(manifest).name,
                mime="application/json",
            )

    with cols[3]:
        zip_bytes = st.session_state.get("last_zip")
        if zip_bytes:
            st.download_button(
                "Download ZIP",
                data=zip_bytes,
                file_name="web_text_extractor_output.zip",
                mime="application/zip",
            )


def render_logs() -> None:
    with st.expander("Logs", expanded=True):
        if st.button("Clear logs"):
            reset_logs()
            st.rerun()
        log_text = "\n".join(st.session_state.logs[-500:])
        st.text_area("Execution log", value=log_text, height=260)


def sidebar_settings():
    st.sidebar.header("Settings")

    book_format = st.sidebar.selectbox(
        "Book output",
        ["both", "txt", "epub"],
        index=0,
        help="both = TXT + EPUB",
    )

    delay = st.sidebar.number_input(
        "Delay between requests (seconds)",
        min_value=0.0,
        max_value=20.0,
        value=1.5,
        step=0.5,
    )

    timeout = st.sidebar.number_input(
        "Timeout per request (seconds)",
        min_value=5,
        max_value=120,
        value=30,
        step=5,
    )

    retries = st.sidebar.number_input(
        "Retries per URL",
        min_value=1,
        max_value=10,
        value=3,
        step=1,
    )

    max_chapters = st.sidebar.number_input(
        "Max chapters, 0 = no limit",
        min_value=0,
        max_value=100000,
        value=5,
        step=1,
        help="Use 5 first for testing. Set 0 for full crawl.",
    )

    max_index_pages = st.sidebar.number_input(
        "Max index pages, 0 = no limit",
        min_value=0,
        max_value=10000,
        value=0,
        step=1,
    )

    sort_mode = st.sidebar.selectbox("Chapter order", ["natural", "discovery"], index=0)

    write_chapters = st.sidebar.checkbox("Also save per-chapter files", value=False)

    chapter_format = st.sidebar.selectbox("Per-chapter format", ["md", "txt", "json"], index=0)

    unicode_filename = st.sidebar.checkbox(
        "Keep Vietnamese accents in filenames",
        value=False,
        help="Default off to avoid mojibake on Windows/ZIP.",
    )

    verify_ssl = st.sidebar.checkbox(
        "Verify SSL certificate",
        value=True,
        help="Turn off only if your network/proxy causes CERTIFICATE_VERIFY_FAILED.",
    )

    ca_bundle = st.sidebar.text_input(
        "Custom CA bundle path",
        value="",
        help="Optional .pem path. Usually leave blank on Streamlit Cloud.",
    )

    user_agent = st.sidebar.text_input("Custom User-Agent", value="")

    language = st.sidebar.text_input("EPUB language", value="vi")

    stop_on_error = st.sidebar.checkbox("Stop on first error", value=False)

    return {
        "book_format": book_format,
        "delay": delay,
        "timeout": int(timeout),
        "retries": int(retries),
        "max_chapters": int(max_chapters),
        "max_index_pages": int(max_index_pages),
        "sort_mode": sort_mode,
        "write_chapters": write_chapters,
        "chapter_format": chapter_format,
        "unicode_filename": unicode_filename,
        "verify_ssl": verify_ssl,
        "ca_bundle": ca_bundle.strip(),
        "user_agent": user_agent.strip(),
        "language": language.strip() or "vi",
        "stop_on_error": stop_on_error,
    }


def save_and_zip_result(result: CrawlResult, settings: dict) -> dict[str, Path]:
    title_slug = slugify(result.title or "book", ascii_filename=not settings["unicode_filename"])
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"web_text_extractor_{title_slug}_"))

    files = write_result_files(
        result=result,
        out_dir=tmp_dir,
        book_format=settings["book_format"],
        write_chapters=settings["write_chapters"],
        chapter_format=settings["chapter_format"],
        ascii_filename=not settings["unicode_filename"],
        language=settings["language"],
    )

    st.session_state.last_result = result
    st.session_state.last_files = files
    st.session_state.last_zip = zip_paths(files)

    return files


def tab_full_novel(settings: dict) -> None:
    st.header("Full novel → TXT / EPUB")

    st.caption(
        "Input link tiên đề/index, ví dụ: https://truyenfull.today/trung-sinh-chi-nha-noi/. "
        "App sẽ đọc #total-page, tự tạo /trang-2/ ... rồi extract từng .chapter-c."
    )

    novel_url = st.text_input(
        "Novel/index URL",
        value="https://truyenfull.today/trung-sinh-chi-nha-noi/",
    )

    col1, col2 = st.columns([1, 2])
    with col1:
        start = st.button("Start crawl", type="primary", use_container_width=True)
    with col2:
        st.warning("Nên test Max chapters = 5 trước. Full 2.000 chương có thể chạy lâu, đặc biệt nếu delay > 1s.")

    if start:
        reset_logs()
        progress = st.progress(0)
        status = st.empty()

        try:
            with st.spinner("Running crawl..."):
                result = crawl_novel_to_result(
                    novel_url=novel_url,
                    verify_ssl=settings["verify_ssl"],
                    ca_bundle=settings["ca_bundle"],
                    user_agent=settings["user_agent"],
                    timeout=settings["timeout"],
                    retries=settings["retries"],
                    delay=settings["delay"],
                    max_index_pages=settings["max_index_pages"],
                    max_chapters=settings["max_chapters"],
                    sort_mode=settings["sort_mode"],
                    stop_on_error=settings["stop_on_error"],
                    progress_callback=make_progress_callback(progress, status),
                )

                files = save_and_zip_result(result, settings)

            progress.progress(1.0)
            st.success(
                f"Done. Index pages: {len(result.index_pages)} | "
                f"Chapter URLs found: {len(result.chapter_urls)} | "
                f"Chapters extracted: {len(result.chapters)} | "
                f"Failed: {len(result.failed_urls)}"
            )

            st.json(json.loads(result_to_manifest_json(result)))

        except Exception as exc:
            st.error(f"ERROR: {exc}")
            log_line(f"ERROR: {exc}")


def tab_single_chapter(settings: dict) -> None:
    st.header("Single chapter/article URL")

    url = st.text_input("URL", value="https://truyenfull.today/trung-sinh-chi-nha-noi/chuong-1-1/")
    output_format = st.selectbox("Output format", ["md", "txt", "json"], index=0)

    if st.button("Extract single URL", type="primary"):
        reset_logs()
        try:
            session = build_session(
                user_agent=settings["user_agent"],
                verify_ssl=settings["verify_ssl"],
                ca_bundle=settings["ca_bundle"],
            )
            html = fetch_html(session, url, timeout=settings["timeout"], retries=settings["retries"])
            page = export_single_page_html(html, source_url=url)

            if output_format == "txt":
                data = f"{page.title}\n{'=' * len(page.title)}\n\n{page.content}\n".encode("utf-8")
                file_name = f"{slugify(page.title)}.txt"
                mime = "text/plain"
            elif output_format == "md":
                data = f"# {page.title}\n\nSource: {page.url}\n\n{page.content}\n".encode("utf-8")
                file_name = f"{slugify(page.title)}.md"
                mime = "text/markdown"
            else:
                data = json.dumps(page.__dict__, ensure_ascii=False, indent=2).encode("utf-8")
                file_name = f"{slugify(page.title)}.json"
                mime = "application/json"

            st.success(f"Extracted: {page.title} | chars={page.content_chars} | selector={page.source_selector}")
            st.download_button("Download", data=data, file_name=file_name, mime=mime)
            st.text_area("Preview", page.content[:5000], height=320)

        except Exception as exc:
            st.error(f"ERROR: {exc}")


def tab_urls_file(settings: dict) -> None:
    st.header("Chapter URLs list → TXT / EPUB")

    st.caption("Upload hoặc paste danh sách chapter URL, mỗi dòng một URL.")

    book_title = st.text_input("Book title", value="Exported Book")
    author = st.text_input("Author", value="")
    pasted = st.text_area("Paste URLs", height=180)
    uploaded = st.file_uploader("Or upload .txt URL list", type=["txt"])

    if st.button("Run URL list", type="primary"):
        reset_logs()

        try:
            urls: list[str] = []
            if uploaded is not None:
                content = uploaded.read().decode("utf-8", errors="ignore")
                urls.extend([line.strip() for line in content.splitlines() if line.strip() and not line.strip().startswith("#")])

            urls.extend([line.strip() for line in pasted.splitlines() if line.strip() and not line.strip().startswith("#")])

            if not urls:
                st.warning("No URLs provided.")
                return

            progress = st.progress(0)
            status = st.empty()

            result = crawl_urls_to_result(
                urls=urls,
                title=book_title,
                author=author,
                verify_ssl=settings["verify_ssl"],
                ca_bundle=settings["ca_bundle"],
                user_agent=settings["user_agent"],
                timeout=settings["timeout"],
                retries=settings["retries"],
                delay=settings["delay"],
                max_chapters=settings["max_chapters"],
                sort_mode=settings["sort_mode"],
                stop_on_error=settings["stop_on_error"],
                progress_callback=make_progress_callback(progress, status),
            )

            save_and_zip_result(result, settings)
            progress.progress(1.0)

            st.success(
                f"Done. Input URLs: {len(urls)} | "
                f"Chapters extracted: {len(result.chapters)} | "
                f"Failed: {len(result.failed_urls)}"
            )

            st.json(json.loads(result_to_manifest_json(result)))

        except Exception as exc:
            st.error(f"ERROR: {exc}")


def tab_upload_html() -> None:
    st.header("Upload local HTML → Extract content")

    st.caption("Dùng khi mạng chặn request. Bạn có thể Save Page HTML từ browser rồi upload vào đây.")

    uploaded = st.file_uploader("Upload HTML/TXT file", type=["html", "htm", "txt"])
    source_url = st.text_input("Optional source URL", value="")
    output_format = st.selectbox("Output", ["md", "txt", "json"], index=0, key="upload_html_output")

    if st.button("Extract uploaded HTML", type="primary"):
        if uploaded is None:
            st.warning("Please upload a file first.")
            return

        raw = uploaded.read()
        html = raw.decode("utf-8", errors="ignore")

        try:
            page = export_single_page_html(html, source_url=source_url)

            if output_format == "txt":
                data = f"{page.title}\n{'=' * len(page.title)}\n\n{page.content}\n".encode("utf-8")
                file_name = f"{slugify(page.title)}.txt"
                mime = "text/plain"
            elif output_format == "md":
                data = f"# {page.title}\n\n{page.content}\n".encode("utf-8")
                file_name = f"{slugify(page.title)}.md"
                mime = "text/markdown"
            else:
                data = json.dumps(page.__dict__, ensure_ascii=False, indent=2).encode("utf-8")
                file_name = f"{slugify(page.title)}.json"
                mime = "application/json"

            st.success(f"Extracted: {page.title} | chars={page.content_chars} | selector={page.source_selector}")
            st.download_button("Download", data=data, file_name=file_name, mime=mime)
            st.text_area("Preview", page.content[:5000], height=320)

        except Exception as exc:
            st.error(f"ERROR: {exc}")

def main() -> None:
    init_state()

    st.title("📚 Web Text Extractor")
    st.markdown(
        """
        Tool extract text/content từ web truyện hoặc article HTML.
        Bản này xử lý case Truyenfull-like: `#total-page`, `/trang-N/`, `#list-chapter`, `.chapter-c`.
        """
    )

    settings = sidebar_settings()

    tabs = st.tabs([
        "Full novel",
        "Single chapter",
        "URLs file",
        "Upload HTML",
        "Downloads & logs",
    ])

    with tabs[0]:
        tab_full_novel(settings)

    with tabs[1]:
        tab_single_chapter(settings)

    with tabs[2]:
        tab_urls_file(settings)

    with tabs[3]:
        tab_upload_html()

    with tabs[4]:
        render_downloads()
        render_logs()


if __name__ == "__main__":
    main()
