"""Wiki Dump 解析适配器（设计 3.5 第 2～3 步）。

首版支持 WikiExtractor JSONL 格式：每行一条词条，
必须含 `id`（页 ID）、`revid`、`title`、`url`、`text`；
可选 `lang`、`timestamp`、`redirect`。

数据包来源支持本地目录、本地文件、`file://` 与 `http(s)://`；
`s3://` 在首版明确报错，避免引入未经验证的云存储依赖。
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

import httpx

from app.core.errors import AppError
from app.domain.wiki import extract_categories, first_paragraph, normalize_text

MAX_ITEM_BYTES = 1_048_576


@dataclass(frozen=True)
class WikiArticleRecord:
    page_id: str
    revision_id: str
    title: str
    language: str
    content: str
    categories: list[str]
    source_url: str
    source_updated_at: datetime | None
    redirected: bool
    content_hash: str
    summary: str


@dataclass(frozen=True)
class WikiPackageEntry:
    cursor: str
    record: WikiArticleRecord | None
    error: str | None = None


@dataclass(frozen=True)
class WikiPackageView:
    files: list[Path]
    total_count: int | None


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _normalize_language(value: str | None, *, fallback: str = "zh") -> str:
    raw = (value or "").strip().lower()
    return raw if raw else fallback


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def parse_wikiextractor_line(line: str) -> WikiArticleRecord:
    if len(line.encode("utf-8")) > MAX_ITEM_BYTES:
        raise AppError("WIKI_PACKAGE_INVALID", "单条词条超过 1MB，可能不是预期格式", 422)
    try:
        item = json.loads(line)
    except json.JSONDecodeError as exc:
        raise AppError("WIKI_PACKAGE_INVALID", "WikiExtractor 行不是合法 JSON", 422) from exc
    if not isinstance(item, dict):
        raise AppError("WIKI_PACKAGE_INVALID", "WikiExtractor 行必须为 JSON 对象", 422)
    page_id = str(item.get("id") or "")
    revision_id = str(item.get("revid") or "")
    title = str(item.get("title") or "")
    if not page_id or not title:
        raise AppError("WIKI_PACKAGE_INVALID", "词条缺少 id 或 title", 422)
    redirected = bool(item.get("redirect"))
    if redirected:
        return WikiArticleRecord(
            page_id=page_id,
            revision_id=revision_id,
            title=title,
            language=_normalize_language(item.get("lang")),
            content="",
            categories=[],
            source_url=str(item.get("url") or ""),
            source_updated_at=_parse_timestamp(item.get("timestamp")),
            redirected=True,
            content_hash=_digest(""),
            summary="",
        )
    content = normalize_text(str(item.get("text") or ""))
    if not content:
        raise AppError("WIKI_PACKAGE_INVALID", f"词条 {title} 正文为空", 422)
    categories, body = extract_categories(content)
    body = normalize_text(body)
    if not body:
        raise AppError("WIKI_PACKAGE_INVALID", f"词条 {title} 正文为空", 422)
    return WikiArticleRecord(
        page_id=page_id,
        revision_id=revision_id,
        title=title,
        language=_normalize_language(item.get("lang")),
        content=body,
        categories=categories,
        source_url=str(item.get("url") or ""),
        source_updated_at=_parse_timestamp(item.get("timestamp")),
        redirected=False,
        content_hash=_digest(body),
        summary=first_paragraph(body),
    )


def _count_lines(path: Path) -> int:
    count = 0
    with path.open("rb") as handle:
        for line in handle:
            if line.strip():
                count += 1
    return count


@contextmanager
def open_package(uri: str, *, max_bytes: int) -> Iterator[WikiPackageView]:
    """打开数据包并返回可逐行读取的文件列表；HTTP 包会先落盘临时文件。"""
    cleanup: list[Path] = []
    try:
        parsed = urlsplit(uri)
        if parsed.scheme == "s3":
            raise AppError(
                "VALIDATION_ERROR",
                "首版暂不支持 s3:// 数据包，请提供本地路径、file:// 或 HTTP(S) URL",
                422,
            )
        if parsed.scheme in {"http", "https"}:
            files = [_download_package(uri, max_bytes=max_bytes, cleanup=cleanup)]
        else:
            path = _local_path(uri, parsed)
            files = _expand_local(path)
        total = sum(_count_lines(path) for path in files)
        yield WikiPackageView(files=files, total_count=total)
    finally:
        for path in cleanup:
            path.unlink(missing_ok=True)


def _local_path(uri: str, parsed) -> Path:
    # Windows 盘符会被 urlsplit 识别成 scheme，例如 C:\dump\a.jsonl。
    drive_letter = len(parsed.scheme) == 1 and uri[1:3] in {":\\", ":/"}
    if parsed.scheme == "file":
        path = Path(url2pathname(unquote(parsed.path)))
    elif parsed.scheme and not drive_letter:
        raise AppError("VALIDATION_ERROR", f"不支持的数据包协议：{parsed.scheme}", 422)
    else:
        path = Path(uri)
    if not path.exists():
        raise AppError("VALIDATION_ERROR", "数据包路径不存在", 422)
    if not path.is_file() and not path.is_dir():
        raise AppError("VALIDATION_ERROR", "数据包路径必须是文件或目录", 422)
    return path


def _expand_local(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    files = sorted(
        item
        for item in path.iterdir()
        if item.is_file() and item.suffix in {".jsonl", ".json", ".txt"}
    )
    if not files:
        raise AppError("VALIDATION_ERROR", "数据包目录中未找到 .jsonl/.json/.txt 文件", 422)
    return files


def _download_package(uri: str, *, max_bytes: int, cleanup: list[Path]) -> Path:
    try:
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            with client.stream("GET", uri) as response:
                if response.status_code >= 400:
                    raise AppError(
                        "WIKI_PACKAGE_UNAVAILABLE",
                        f"下载数据包失败：HTTP {response.status_code}",
                        502,
                    )
                length = response.headers.get("content-length")
                if length is not None and length.isdigit() and int(length) > max_bytes:
                    raise AppError("VALIDATION_ERROR", "数据包超过允许的最大字节数", 422)
                handle = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl")
                target = Path(handle.name)
                cleanup.append(target)
                written = 0
                for chunk in response.iter_bytes():
                    written += len(chunk)
                    if written > max_bytes:
                        raise AppError("VALIDATION_ERROR", "数据包超过允许的最大字节数", 422)
                    handle.write(chunk)
                handle.close()
    except httpx.HTTPError as exc:
        raise AppError("WIKI_PACKAGE_UNAVAILABLE", "下载数据包失败", 502) from exc
    return target


def iter_package_entries(
    view: WikiPackageView, *, start_after: str | None = None
) -> Iterator[WikiPackageEntry]:
    """按文件顺序产出词条；`start_after` 为上次成功处理的游标。"""
    matched = start_after is None
    for path in view.files:
        label = path.name
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                cursor = f"{label}:{line_number}"
                if not matched:
                    if cursor == start_after:
                        matched = True
                    continue
                try:
                    record = parse_wikiextractor_line(line)
                except AppError as exc:
                    yield WikiPackageEntry(cursor=cursor, record=None, error=exc.message)
                else:
                    yield WikiPackageEntry(cursor=cursor, record=record)
    if not matched:
        raise AppError(
            "VALIDATION_ERROR", "同步游标与数据包不匹配，请使用相同 package_version 重试", 422
        )
