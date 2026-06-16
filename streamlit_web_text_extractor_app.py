#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Streamlit Web Text Extractor v8

Main file for Streamlit Cloud:
    streamlit_web_text_extractor_app_v8.py

Files required:
    streamlit_web_text_extractor_app_v8.py
    web_text_extractor_core_v8.py
    requirements.txt
"""

from __future__ import annotations

import io
import json
import random
import shutil
import tempfile
import time
import traceback
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import streamlit as st

from web_text_extractor_core_v8 import (
    CrawlResult,
    ExtractedPage,
    append_jsonl,
    atomic_write_json,
    build_session,
    chapter_json_path,
    discover_chapter_urls,
    extract_from_html,
    fetch_html,
    load_saved_chapter_files,
    natural_chapter_key,
    normalize_novel_url,
    normalize_url,
    result_to_manifest_json,
    save_chapter_json_atomic,
    saved_global_indices,
    slugify,
    write_result_files,
)


APP_VERSION = "fixed-v8-multithread-range-checkpoint"
DEFAULT_NOVEL_URL = "https://truyenfull.today/trung-sinh-chi-nha-noi/"


st.set_page_config(page_title="Web Text Extractor v8", page_icon="📚", layout="wide")


# =========================
# Session state / workspace
# =========================

def init_state() -> None:
    st.session_state.setdefault("logs", [])
    st.session_state.setdefault("workspace", "")
    st.session_state.setdefault("last_files", {})
    st.session_state.setdefault("last_zip", None)


def log_line(message: str) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    st.session_state.logs.append(f"[{now}] {message}")


def reset_logs() -> None:
    st.session_state.logs = []


def new_workspace() -> Path:
    root = Path(tempfile.gettempdir()) / "web_text_extractor_v8_checkpoints"
    root.mkdir(parents=True, exist_ok=True)
    workspace = root / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    workspace.mkdir(parents=True, exist_ok=True)
    st.session_state.workspace = str(workspace)
    return workspace


def get_workspace() -> Path:
    if not st.session_state.workspace:
        return new_workspace()

    workspace = Path(st.session_state.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def get_paths(workspace: Path) -> dict[str, Path]:
    return {
        "checkpoint": workspace / "checkpoint.json",
        "chapter_urls": workspace / "chapter_urls.txt",
        "chapters_dir": workspace / "chapters",
        "failed_jsonl": workspace / "failed.jsonl",
        "export_dir": workspace / "export",
    }


# =========================
# ZIP / checkpoint restore
# =========================

def zip_folder(folder: Path) -> bytes:
    mem = io.BytesIO()

    with zipfile.ZipFile(mem, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for child in folder.rglob("*"):
            if child.is_file():
                z.write(child, arcname=str(child.relative_to(folder)))

    return mem.getvalue()


def zip_paths(paths: dict[str, Path]) -> bytes:
    mem = io.BytesIO()

    with zipfile.ZipFile(mem, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for _key, path in paths.items():
            if path.is_dir():
                for child in path.rglob("*"):
                    if child.is_file():
                        z.write(child, arcname=str(child.relative_to(path.parent)))
            elif path.is_file():
                z.write(path, arcname=path.name)

    return mem.getvalue()


def restore_checkpoint_zip(uploaded_file) -> Path:
    workspace = new_workspace()
    raw = uploaded_file.read()

    with zipfile.ZipFile(io.BytesIO(raw), "r") as z:
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in Path(name).parts:
                continue
            z.extract(info, workspace)

    # If the zip contains one top folder, lift checkpoint files up.
    if not (workspace / "checkpoint.json").exists():
        candidates = list(workspace.rglob("checkpoint.json"))
        if candidates:
            src_dir = candidates[0].parent
            for child in src_dir.iterdir():
                dst = workspace / child.name
                if child.is_file():
                    shutil.copy2(child, dst)
                elif child.is_dir() and not dst.exists():
                    shutil.copytree(child, dst)

    if not (workspace / "checkpoint.json").exists():
        raise RuntimeError("Uploaded ZIP does not contain checkpoint.json.")

    st.session_state.workspace = str(workspace)
    return workspace


# =========================
# Checkpoint / result loading
# =========================

def read_checkpoint(workspace: Path) -> dict:
    path = get_paths(workspace)["checkpoint"]
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_checkpoint(workspace: Path, data: dict) -> None:
    data["last_updated"] = datetime.now().isoformat(timespec="seconds")
    atomic_write_json(get_paths(workspace)["checkpoint"], data)


def result_from_workspace(workspace: Path) -> CrawlResult:
    paths = get_paths(workspace)
    ckpt = read_checkpoint(workspace)

    if not ckpt:
        raise RuntimeError("No checkpoint.json found in current workspace.")

    chapters = load_saved_chapter_files(paths["chapters_dir"])

    failed: list[dict] = []
    if paths["failed_jsonl"].exists():
        for line in paths["failed_jsonl"].read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                failed.append(json.loads(line))

    return CrawlResult(
        novel_url=ckpt.get("novel_url", ""),
        title=ckpt.get("title", "Exported Book"),
        author=ckpt.get("author", ""),
        index_pages=ckpt.get("index_pages", []),
        chapter_urls=ckpt.get("chapter_urls", []),
        chapters=chapters,
        failed_urls=failed,
    )


# =========================
# Sidebar settings
# =========================

def sidebar_settings() -> dict:
    st.sidebar.header("Crawl settings")

    settings = {
        "book_format": st.sidebar.selectbox(
            "Book output",
            ["both", "txt", "epub"],
            index=0,
            help="Chọn định dạng export cuối cùng. both = tạo cả TXT và EPUB. TXT thường chắc chắn nhất; EPUB dùng để đọc trên Apple Books/Kindle app.",
        ),
        "start_position": int(st.sidebar.number_input(
            "Start chapter position",
            min_value=1,
            max_value=200000,
            value=1,
            step=1,
            help="Vị trí bắt đầu trong danh sách chương sau khi app sort. Ví dụ batch đầu 1, batch sau 501. Đây không phải số luồng.",
        )),
        "end_position": int(st.sidebar.number_input(
            "End chapter position",
            min_value=0,
            max_value=200000,
            value=500,
            step=1,
            help="Vị trí chương cuối của batch. Ví dụ 500 nghĩa là crawl từ Start tới chương thứ 500. Đặt 0 để chạy tới chương cuối truyện.",
        )),
        "max_chapters_this_run": int(st.sidebar.number_input(
            "Max chapters this run",
            min_value=0,
            max_value=200000,
            value=0,
            step=1,
            help="Giới hạn thêm cho lần chạy này. 0 = không giới hạn ngoài Start/End. Dùng khi muốn test nhanh, ví dụ set 5.",
        )),
        "workers": int(st.sidebar.number_input(
            "Workers / threads",
            min_value=1,
            max_value=12,
            value=4,
            step=1,
            help="Số luồng fetch song song. 3–5 là hợp lý. Cao quá có thể bị site chặn 403/429 hoặc timeout.",
        )),
        "request_jitter": float(st.sidebar.number_input(
            "Random jitter per request",
            min_value=0.0,
            max_value=10.0,
            value=0.5,
            step=0.1,
            help="Mỗi request sẽ chờ ngẫu nhiên 0 → jitter giây trước khi fetch để tránh nhiều luồng bắn request cùng lúc. Ví dụ 0.5 là nhẹ.",
        )),
        "index_delay": float(st.sidebar.number_input(
            "Delay between index pages",
            min_value=0.0,
            max_value=20.0,
            value=0.5,
            step=0.5,
            help="Delay khi quét các trang danh sách chương /trang-2/, /trang-3/. Chỉ ảnh hưởng bước discover link chương.",
        )),
        "timeout": int(st.sidebar.number_input(
            "Timeout per request",
            min_value=5,
            max_value=180,
            value=30,
            step=5,
            help="Thời gian tối đa chờ một request. Nếu mạng/site chậm, tăng lên 60.",
        )),
        "retries": int(st.sidebar.number_input(
            "Retries per URL",
            min_value=1,
            max_value=10,
            value=3,
            step=1,
            help="Số lần thử lại nếu request lỗi. Ví dụ 3 nghĩa là lỗi sẽ thử lại tối đa 3 lần trước khi ghi vào failed.jsonl.",
        )),
        "sort_mode": st.sidebar.selectbox(
            "Chapter order",
            ["natural", "discovery"],
            index=0,
            help="natural = sort theo số trong URL chương, thường đúng nhất. discovery = giữ thứ tự link xuất hiện trên site.",
        ),
        "verify_ssl": st.sidebar.checkbox(
            "Verify SSL certificate",
            value=True,
            help="Bật để kiểm tra SSL. Nếu chạy local bị CERTIFICATE_VERIFY_FAILED do proxy/mạng công ty, có thể tắt. Trên Streamlit Cloud thường nên bật.",
        ),
        "ca_bundle": st.sidebar.text_input(
            "Custom CA bundle path",
            value="",
            help="Đường dẫn file .pem CA nội bộ nếu mạng công ty dùng HTTPS inspection. Thường để trống.",
        ).strip(),
        "user_agent": st.sidebar.text_input(
            "Custom User-Agent",
            value="",
            help="Header User-Agent custom. Để trống app sẽ dùng Chrome-like User-Agent mặc định.",
        ).strip(),
        "language": st.sidebar.text_input(
            "EPUB language",
            value="vi",
            help="Mã ngôn ngữ trong EPUB metadata. Với tiếng Việt để vi.",
        ).strip() or "vi",
        "stop_on_error": st.sidebar.checkbox(
            "Stop on first error",
            value=False,
            help="Nếu bật, gặp một chương lỗi sẽ dừng toàn bộ batch. Nếu tắt, chương lỗi ghi vào failed.jsonl rồi chạy tiếp.",
        ),
        "unicode_filename": st.sidebar.checkbox(
            "Keep Vietnamese accents in filenames",
            value=False,
            help="Bật để giữ dấu tiếng Việt trong tên file. Tắt để filename ASCII an toàn hơn trên Windows/ZIP.",
        ),
    }

    st.sidebar.markdown("---")
    st.sidebar.caption(
        "Gợi ý: batch 1 Start=1 End=500, batch 2 Start=501 End=1000, workers=4, jitter=0.5."
    )

    return settings


# =========================
# Discovery and target range
# =========================

def discover_or_load_urls(novel_url: str, workspace: Path, settings: dict):
    paths = get_paths(workspace)
    ckpt = read_checkpoint(workspace)

    clean_novel_url = normalize_novel_url(novel_url)

    if ckpt and ckpt.get("novel_url") == clean_novel_url and ckpt.get("chapter_urls"):
        log_line("Using chapter URLs from existing checkpoint.")
        return (
            ckpt.get("title", "Exported Book"),
            ckpt.get("author", ""),
            ckpt.get("index_pages", []),
            ckpt.get("chapter_urls", []),
        )

    session = build_session(
        user_agent=settings["user_agent"],
        verify_ssl=settings["verify_ssl"],
        ca_bundle=settings["ca_bundle"],
    )

    progress = st.progress(0)
    status = st.empty()

    def progress_callback(stage: str, current: int, total: int, message: str) -> None:
        progress.progress(min(current / max(total, 1), 1.0))
        status.info(f"{stage}: {current}/{total} | {message}")
        log_line(f"{stage}: {current}/{total} | {message}")

    title, author, index_pages, chapter_urls = discover_chapter_urls(
        novel_url=clean_novel_url,
        session=session,
        timeout=settings["timeout"],
        retries=settings["retries"],
        delay=settings["index_delay"],
        max_index_pages=0,
        progress_callback=progress_callback,
    )

    if settings["sort_mode"] == "natural":
        chapter_urls = sorted(chapter_urls, key=natural_chapter_key)

    paths["chapter_urls"].write_text("\n".join(chapter_urls) + "\n", encoding="utf-8")

    ckpt = {
        "app_version": APP_VERSION,
        "novel_url": clean_novel_url,
        "title": title,
        "author": author,
        "index_pages": index_pages,
        "chapter_urls": chapter_urls,
        "total_chapters_found": len(chapter_urls),
        "done_count": len(saved_global_indices(paths["chapters_dir"])),
        "failed_count": 0,
        "status": "discovered",
    }
    write_checkpoint(workspace, ckpt)

    return title, author, index_pages, chapter_urls


def build_target_pairs(chapter_urls: list[str], settings: dict, existing_indices: set[int]) -> list[tuple[int, str]]:
    total = len(chapter_urls)
    start = max(settings["start_position"], 1)

    end = settings["end_position"]
    if end <= 0:
        end = total
    end = min(end, total)

    if start > end:
        return []

    all_pairs = list(enumerate(chapter_urls, start=1))
    target = [(idx, url) for idx, url in all_pairs if start <= idx <= end]

    cap = settings["max_chapters_this_run"]
    if cap and cap > 0:
        target = target[:cap]

    # Important: resume by skipping files that already exist.
    target = [(idx, url) for idx, url in target if idx not in existing_indices]

    return target


# =========================
# Threaded fetch
# =========================

def fetch_one_chapter_worker(global_index: int, url: str, workspace_str: str, settings: dict) -> dict:
    workspace = Path(workspace_str)
    paths = get_paths(workspace)
    chapter_dir = paths["chapters_dir"]

    final_path = chapter_json_path(chapter_dir, global_index)

    if final_path.exists():
        return {
            "status": "skipped",
            "global_index": global_index,
            "url": normalize_url(url),
            "path": str(final_path),
            "chars": 0,
        }

    jitter = float(settings.get("request_jitter", 0.0))
    if jitter > 0:
        time.sleep(random.uniform(0, jitter))

    session = build_session(
        user_agent=settings.get("user_agent", ""),
        verify_ssl=settings.get("verify_ssl", True),
        ca_bundle=settings.get("ca_bundle", ""),
    )

    clean_url = normalize_url(url)
    html = fetch_html(
        session,
        clean_url,
        timeout=int(settings.get("timeout", 30)),
        retries=int(settings.get("retries", 3)),
    )

    page = extract_from_html(html, clean_url)
    path = save_chapter_json_atomic(chapter_dir, global_index, page)

    return {
        "status": "ok",
        "global_index": global_index,
        "url": clean_url,
        "path": str(path),
        "chars": page.content_chars,
        "title": page.chapter_title or page.title,
    }


def run_threaded_crawl(novel_url: str, settings: dict) -> CrawlResult:
    workspace = get_workspace()
    paths = get_paths(workspace)
    paths["chapters_dir"].mkdir(parents=True, exist_ok=True)

    clean_novel_url = normalize_novel_url(novel_url)

    log_line(f"Workspace: {workspace}")
    log_line("Discovering or loading chapter URLs...")

    title, author, index_pages, chapter_urls = discover_or_load_urls(clean_novel_url, workspace, settings)

    existing_indices = saved_global_indices(paths["chapters_dir"])
    target_pairs = build_target_pairs(chapter_urls, settings, existing_indices)

    ckpt = read_checkpoint(workspace)
    ckpt.update({
        "app_version": APP_VERSION,
        "novel_url": clean_novel_url,
        "title": title,
        "author": author,
        "index_pages": index_pages,
        "chapter_urls": chapter_urls,
        "total_chapters_found": len(chapter_urls),
        "range_start": settings["start_position"],
        "range_end": settings["end_position"],
        "workers": settings["workers"],
        "target_remaining_this_run": len(target_pairs),
        "done_count": len(existing_indices),
        "status": "running",
    })
    write_checkpoint(workspace, ckpt)

    st.info(
        f"Total chapters found: {len(chapter_urls)} | Already saved: {len(existing_indices)} | "
        f"Will fetch this run: {len(target_pairs)}"
    )

    if not target_pairs:
        log_line("No target left to fetch. Existing files already cover this range.")
        return result_from_workspace(workspace)

    progress = st.progress(0)
    status = st.empty()

    total = len(target_pairs)
    completed_this_run = 0
    ok_this_run = 0
    skipped_this_run = 0
    fail_this_run = 0

    with ThreadPoolExecutor(max_workers=settings["workers"]) as executor:
        future_map = {
            executor.submit(fetch_one_chapter_worker, idx, url, str(workspace), settings): (idx, url)
            for idx, url in target_pairs
        }

        for future in as_completed(future_map):
            idx, url = future_map[future]
            completed_this_run += 1

            try:
                result = future.result()
                result_status = result["status"]

                if result_status == "ok":
                    ok_this_run += 1
                    msg = f"OK global#{idx} | chars={result.get('chars')} | {result.get('url')}"
                    status.success(f"chapter: {completed_this_run}/{total} | {msg}")
                    log_line(f"chapter: {completed_this_run}/{total} | {msg}")

                elif result_status == "skipped":
                    skipped_this_run += 1
                    msg = f"SKIP existing global#{idx} | {result.get('url')}"
                    status.info(f"chapter: {completed_this_run}/{total} | {msg}")
                    log_line(f"chapter: {completed_this_run}/{total} | {msg}")

                else:
                    msg = f"{result_status} global#{idx} | {result.get('url')}"
                    status.info(f"chapter: {completed_this_run}/{total} | {msg}")
                    log_line(f"chapter: {completed_this_run}/{total} | {msg}")

                saved_count = len(saved_global_indices(paths["chapters_dir"]))
                ckpt = read_checkpoint(workspace)
                ckpt.update({
                    "status": "running",
                    "done_count": saved_count,
                    "ok_this_run": ok_this_run,
                    "skipped_this_run": skipped_this_run,
                    "failed_this_run": fail_this_run,
                    "completed_this_run": completed_this_run,
                    "target_remaining_this_run": total,
                    "last_ok_global_index": idx,
                    "last_ok_url": normalize_url(url),
                })
                write_checkpoint(workspace, ckpt)

            except Exception as exc:
                fail_this_run += 1
                err = {
                    "global_index": idx,
                    "url": normalize_url(url),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                    "time": datetime.now().isoformat(timespec="seconds"),
                }
                append_jsonl(paths["failed_jsonl"], err)

                msg = f"FAIL global#{idx} | {normalize_url(url)} | {type(exc).__name__}: {exc}"
                status.error(f"chapter: {completed_this_run}/{total} | {msg}")
                log_line(f"chapter: {completed_this_run}/{total} | {msg}")

                ckpt = read_checkpoint(workspace)
                ckpt.update({
                    "status": "running",
                    "failed_this_run": fail_this_run,
                    "completed_this_run": completed_this_run,
                    "last_error_global_index": idx,
                    "last_error_url": normalize_url(url),
                    "last_error": f"{type(exc).__name__}: {exc}",
                })
                write_checkpoint(workspace, ckpt)

                if settings["stop_on_error"]:
                    raise

            progress.progress(min(completed_this_run / max(total, 1), 1.0))

    ckpt = read_checkpoint(workspace)
    ckpt.update({
        "status": "completed",
        "done_count": len(saved_global_indices(paths["chapters_dir"])),
        "ok_this_run": ok_this_run,
        "skipped_this_run": skipped_this_run,
        "failed_this_run": fail_this_run,
        "completed_this_run": completed_this_run,
    })
    write_checkpoint(workspace, ckpt)

    return result_from_workspace(workspace)


# =========================
# Export
# =========================

def export_current_workspace(settings: dict) -> None:
    workspace = get_workspace()
    result = result_from_workspace(workspace)

    out_dir = get_paths(workspace)["export_dir"]
    files = write_result_files(
        result=result,
        out_dir=out_dir,
        book_format=settings["book_format"],
        ascii_filename=not settings["unicode_filename"],
        language=settings["language"],
    )

    st.session_state.last_files = files
    st.session_state.last_zip = zip_paths(files)

    st.success(f"Exported from checkpoint: {len(result.chapters)} saved chapters")


# =========================
# UI render
# =========================

def render_checkpoint_status() -> None:
    workspace = get_workspace()
    ckpt = read_checkpoint(workspace)

    if not ckpt:
        return

    paths = get_paths(workspace)
    saved_count = len(saved_global_indices(paths["chapters_dir"]))

    st.markdown("### Current checkpoint")
    st.json({
        "status": ckpt.get("status"),
        "total_chapters_found": ckpt.get("total_chapters_found"),
        "range_start": ckpt.get("range_start"),
        "range_end": ckpt.get("range_end"),
        "workers": ckpt.get("workers"),
        "saved_chapter_files": saved_count,
        "done_count_in_checkpoint": ckpt.get("done_count"),
        "ok_this_run": ckpt.get("ok_this_run"),
        "failed_this_run": ckpt.get("failed_this_run"),
        "last_ok_global_index": ckpt.get("last_ok_global_index"),
        "last_error": ckpt.get("last_error"),
        "last_updated": ckpt.get("last_updated"),
    })


def render_downloads() -> None:
    workspace = get_workspace()
    paths = get_paths(workspace)

    st.subheader("Downloads")

    cols = st.columns(5)

    with cols[0]:
        if paths["checkpoint"].exists():
            st.download_button(
                "Checkpoint ZIP",
                data=zip_folder(workspace),
                file_name="checkpoint_workspace.zip",
                mime="application/zip",
                key="dl_checkpoint_zip_v8",
                help="Tải toàn bộ checkpoint để restore nếu Streamlit reset. Nên tải sau mỗi batch hoặc định kỳ.",
            )

    with cols[1]:
        if paths["checkpoint"].exists():
            st.download_button(
                "checkpoint.json",
                data=paths["checkpoint"].read_bytes(),
                file_name="checkpoint.json",
                mime="application/json",
                key="dl_checkpoint_json_v8",
                help="File trạng thái crawl: range, số chương đã lưu, lỗi gần nhất.",
            )

    with cols[2]:
        if paths["chapter_urls"].exists():
            st.download_button(
                "chapter_urls.txt",
                data=paths["chapter_urls"].read_bytes(),
                file_name="chapter_urls.txt",
                mime="text/plain",
                key="dl_chapter_urls_v8",
                help="Danh sách toàn bộ URL chương đã discover.",
            )

    with cols[3]:
        if paths["failed_jsonl"].exists():
            st.download_button(
                "failed.jsonl",
                data=paths["failed_jsonl"].read_bytes(),
                file_name="failed.jsonl",
                mime="application/jsonl",
                key="dl_failed_v8",
                help="Danh sách chương lỗi, mỗi dòng là một lỗi.",
            )

    with cols[4]:
        if paths["chapters_dir"].exists():
            st.download_button(
                "Raw chapters ZIP",
                data=zip_folder(paths["chapters_dir"]),
                file_name="raw_chapters_json.zip",
                mime="application/zip",
                key="dl_raw_chapters_v8",
                help="ZIP chứa từng chương dạng 000001.json, 000002.json...",
            )

    files = st.session_state.get("last_files") or {}
    if files:
        st.markdown("### Final export")

        c1, c2, c3, c4 = st.columns(4)

        with c1:
            p = files.get("txt")
            if p and Path(p).exists():
                st.download_button(
                    "TXT",
                    data=Path(p).read_bytes(),
                    file_name=Path(p).name,
                    mime="text/plain",
                    key="dl_txt_v8",
                    help="File TXT gom các chương đã lưu trong checkpoint.",
                )

        with c2:
            p = files.get("epub")
            if p and Path(p).exists():
                st.download_button(
                    "EPUB",
                    data=Path(p).read_bytes(),
                    file_name=Path(p).name,
                    mime="application/epub+zip",
                    key="dl_epub_v8",
                    help="File EPUB gom các chương đã lưu trong checkpoint.",
                )

        with c3:
            p = files.get("manifest")
            if p and Path(p).exists():
                st.download_button(
                    "Manifest",
                    data=Path(p).read_bytes(),
                    file_name=Path(p).name,
                    mime="application/json",
                    key="dl_manifest_v8",
                    help="JSON summary của output.",
                )

        with c4:
            if st.session_state.get("last_zip"):
                st.download_button(
                    "Final ZIP",
                    data=st.session_state.last_zip,
                    file_name="web_text_extractor_output.zip",
                    mime="application/zip",
                    key="dl_final_zip_v8",
                    help="ZIP chứa TXT/EPUB/manifest.",
                )


def render_logs() -> None:
    with st.expander("Logs", expanded=True):
        if st.button("Clear logs", key="clear_logs_v8", help="Xóa log hiển thị trong UI, không xóa checkpoint."):
            reset_logs()
            st.rerun()

        st.text_area(
            "Execution log",
            value="\n".join(st.session_state.logs[-1000:]),
            height=320,
            help="Log chạy hiện tại. Nếu Streamlit reset, log UI có thể mất, nhưng checkpoint file vẫn còn nếu workspace chưa mất.",
        )


def tab_full_novel(settings: dict) -> None:
    st.header("Full novel crawler")

    st.info(
        "v8 dùng đa luồng + range + checkpoint. Mỗi chương OK được lưu ngay thành "
        "`chapters/000001.json`, nên export/resume không phụ thuộc RAM."
    )

    novel_url = st.text_input(
        "Novel/index URL",
        value=DEFAULT_NOVEL_URL,
        help="Link tiên đề/index của truyện, không phải link chương. Ví dụ https://truyenfull.today/trung-sinh-chi-nha-noi/",
    )

    uploaded = st.file_uploader(
        "Restore checkpoint ZIP",
        type=["zip"],
        help="Upload checkpoint_workspace.zip đã tải trước đó để resume sau khi Streamlit reset hoặc chuyển máy.",
    )

    if uploaded is not None:
        if st.button("Restore uploaded checkpoint", type="secondary", help="Giải nén checkpoint ZIP vào workspace mới rồi dùng nó để resume."):
            try:
                workspace = restore_checkpoint_zip(uploaded)
                st.success(f"Restored checkpoint to workspace: {workspace}")
                log_line(f"Restored checkpoint: {workspace}")
            except Exception as exc:
                st.error(f"Restore ERROR: {type(exc).__name__}: {exc}")
                st.code(traceback.format_exc(), language="python")

    c1, c2, c3 = st.columns(3)

    with c1:
        start = st.button(
            "Start / Resume selected range",
            type="primary",
            use_container_width=True,
            help="Bắt đầu crawl range đã chọn. Nếu chương đã có file JSON thì app sẽ skip, không chạy lại từ đầu.",
        )

    with c2:
        export_now = st.button(
            "Export current checkpoint",
            use_container_width=True,
            help="Xuất TXT/EPUB từ các chương đã lưu hiện tại, kể cả khi chưa crawl xong full truyện.",
        )

    with c3:
        reset_workspace = st.button(
            "New workspace",
            use_container_width=True,
            help="Tạo workspace mới, dùng khi muốn crawl truyện/range mới từ đầu. Không bấm nếu bạn muốn resume checkpoint hiện tại.",
        )

    if reset_workspace:
        st.session_state.workspace = ""
        st.session_state.last_files = {}
        st.session_state.last_zip = None
        reset_logs()
        st.rerun()

    workspace = get_workspace()
    st.caption(f"Current workspace: `{workspace}`")

    render_checkpoint_status()

    if export_now:
        try:
            export_current_workspace(settings)
        except Exception as exc:
            st.error(f"Export ERROR: {type(exc).__name__}: {exc}")
            st.code(traceback.format_exc(), language="python")

    if start:
        reset_logs()
        try:
            with st.spinner("Crawling selected range with threads..."):
                result = run_threaded_crawl(novel_url, settings)
                export_current_workspace(settings)

            st.success(
                f"Done. Total URLs: {len(result.chapter_urls)} | "
                f"Saved chapters: {len(result.chapters)} | Failed: {len(result.failed_urls)}"
            )
            st.json(json.loads(result_to_manifest_json(result)))

        except Exception as exc:
            st.error(f"ERROR: {type(exc).__name__}: {exc}")
            st.code(traceback.format_exc(), language="python")
            log_line(f"ERROR: {type(exc).__name__}: {exc}")
            log_line(traceback.format_exc())


def tab_single_chapter(settings: dict) -> None:
    st.header("Single chapter/article URL")

    url = st.text_input(
        "URL",
        value="https://truyenfull.today/trung-sinh-chi-nha-noi/chuong-1-1/",
        help="Link một chương hoặc article. Tab này chỉ extract một URL, không crawl full truyện.",
    )

    if st.button("Extract single URL", type="primary", help="Fetch URL này và extract nội dung chính."):
        try:
            session = build_session(
                user_agent=settings["user_agent"],
                verify_ssl=settings["verify_ssl"],
                ca_bundle=settings["ca_bundle"],
            )
            html = fetch_html(session, url, timeout=settings["timeout"], retries=settings["retries"])
            page = extract_from_html(html, url)

            st.success(f"Extracted: {page.title} | chars={page.content_chars} | selector={page.source_selector}")

            data = f"# {page.title}\n\nSource: {page.url}\n\n{page.content}\n".encode("utf-8")
            st.download_button(
                "Download MD",
                data=data,
                file_name=f"{slugify(page.title)}.md",
                mime="text/markdown",
                help="Tải chương/article vừa extract dạng Markdown.",
            )
            st.text_area("Preview", page.content[:5000], height=320)

        except Exception as exc:
            st.error(f"ERROR: {type(exc).__name__}: {exc}")
            st.code(traceback.format_exc(), language="python")


def tab_guide() -> None:
    st.header("Guide")

    st.markdown(
        """
### Range là gì?

`Start chapter position` và `End chapter position` là **vùng chương của batch hiện tại**, tính theo danh sách chương đã sort.

Ví dụ truyện có 2153 chương:

- Batch 1: Start `1`, End `500`
- Batch 2: Start `501`, End `1000`
- Batch 3: Start `1001`, End `1500`
- Batch 4: Start `1501`, End `2153`

### Workers là gì?

`Workers / threads` là số luồng fetch song song trong range đó.

Ví dụ Start `501`, End `1000`, Workers `4`:

- Toàn bộ queue là chương 501 → 1000
- 4 worker chia nhau lấy chương
- Worker nào xong trước sẽ lấy chương tiếp theo
- App lưu từng chương thành `chapters/000501.json`, `chapters/000502.json`...

### Resume hoạt động như nào?

App kiểm tra file trong folder `chapters/`.

Nếu `chapters/000687.json` đã tồn tại, lần chạy sau sẽ skip chương 687.  
Nếu app reset hẳn và mất workspace, upload `checkpoint_workspace.zip` để restore rồi bấm Start/Resume.

### Khi nào nên tải checkpoint ZIP?

Nên tải sau mỗi batch hoặc sau vài trăm chương. Streamlit Cloud có thể reset process, nên checkpoint ZIP là cách giữ dữ liệu ngoài server tạm.
        """
    )


def main() -> None:
    init_state()

    st.title("📚 Web Text Extractor")
    st.caption(f"Version: {APP_VERSION}")

    settings = sidebar_settings()

    tabs = st.tabs(["Full novel", "Single chapter", "Downloads & logs", "Guide"])

    with tabs[0]:
        tab_full_novel(settings)

    with tabs[1]:
        tab_single_chapter(settings)

    with tabs[2]:
        render_downloads()
        render_logs()

    with tabs[3]:
        tab_guide()


if __name__ == "__main__":
    main()
