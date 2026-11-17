"""Wiki 文本规范化与知识块切分（设计 3.5 第 3～4 步）。

这里只放纯函数，便于不依赖数据库的单元测试。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

HEADING_PATTERN = re.compile(r"^(={2,6})\s*(.+?)\s*\1$")
CATEGORIES_PATTERN = re.compile(r"^CATEGORIES:\s*(.+)$", re.IGNORECASE)
SENTENCE_BREAKS = "。！？；\n"


@dataclass(frozen=True)
class WikiChunkDraft:
    heading_path: tuple[str, ...]
    text: str


def normalize_title(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_text(value: str) -> str:
    """统一换行、去掉行尾空格并把连续空行压缩为一段。"""
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_categories(text: str) -> tuple[list[str], str]:
    """从正文末尾提取 WikiExtractor 保留的 CATEGORIES 行。"""
    categories: list[str] = []
    kept: list[str] = []
    for line in text.split("\n"):
        match = CATEGORIES_PATTERN.match(line.strip())
        if match:
            categories.extend(
                item.strip() for item in re.split(r"[,，、]", match.group(1)) if item.strip()
            )
            continue
        kept.append(line)
    return categories, "\n".join(kept)


def first_paragraph(text: str, *, max_chars: int = 500) -> str:
    for paragraph in re.split(r"\n\s*\n", text):
        candidate = paragraph.strip()
        if not candidate or HEADING_PATTERN.match(candidate.split("\n", 1)[0]):
            continue
        return candidate[:max_chars]
    return ""


def split_sections(content: str) -> list[tuple[tuple[str, ...], str]]:
    """按 Wiki 标题层级切出 (heading_path, 段落) 序列。"""
    blocks: list[tuple[tuple[str, ...], str]] = []
    stack: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        paragraph = "\n".join(buffer).strip()
        buffer.clear()
        if paragraph:
            blocks.append((tuple(stack), paragraph))

    for line in content.split("\n"):
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        heading = HEADING_PATTERN.match(stripped)
        if heading:
            flush()
            level = max(1, len(heading.group(1)) - 1)
            title = normalize_title(heading.group(2))
            if title:
                stack = stack[: level - 1] + [title]
            continue
        buffer.append(line)
    flush()
    return blocks


def _split_piece(text: str, *, max_chars: int, overlap: int) -> list[tuple[str, bool]]:
    """把超长段落切成带重叠的窗口，尽量落在句末标点。

    返回 (片段, 是否已与上一片段重叠)。
    """
    if len(text) <= max_chars:
        return [(text, False)]
    pieces: list[tuple[str, bool]] = []
    start = 0
    total = len(text)
    while start < total:
        end = min(start + max_chars, total)
        if end < total:
            floor = start + max_chars // 2
            cut = max(text.rfind(marker, floor, end) for marker in SENTENCE_BREAKS)
            if cut >= floor:
                end = cut + 1
        piece = text[start:end].strip()
        if piece:
            pieces.append((piece, bool(pieces)))
        if end >= total:
            break
        start = max(end - overlap, start + 1)
    return pieces


def chunk_article(
    content: str,
    *,
    min_chars: int = 200,
    max_chars: int = 500,
    overlap_chars: int = 50,
) -> list[WikiChunkDraft]:
    """按标题层级与段落切分，默认每块 200～500 字、重叠 50 字。"""
    if min_chars <= 0 or max_chars <= min_chars:
        raise ValueError("chunk size limits are invalid")
    if not 0 <= overlap_chars < max_chars:
        raise ValueError("overlap must be smaller than max_chars")

    chunks: list[WikiChunkDraft] = []
    pending: list[str] = []
    pending_path: tuple[str, ...] = ()
    pending_len = 0
    carry = ""
    carry_path: tuple[str, ...] = ()

    def flush() -> None:
        nonlocal pending, pending_len, carry, carry_path
        text = "\n\n".join(pending).strip()
        if text:
            chunks.append(WikiChunkDraft(heading_path=pending_path, text=text))
            carry = text[-overlap_chars:] if overlap_chars else ""
            carry_path = pending_path
        pending = []
        pending_len = 0

    for heading_path, paragraph in split_sections(content):
        for piece, continues in _split_piece(paragraph, max_chars=max_chars, overlap=overlap_chars):
            if pending and heading_path != pending_path:
                flush()
            if pending and pending_len + len(piece) > max_chars:
                flush()
            if not pending:
                pending_path = heading_path
                if (
                    carry
                    and carry_path == heading_path
                    and not continues
                    and len(carry) + len(piece) <= max_chars
                ):
                    piece = f"{carry}\n{piece}"
            pending.append(piece)
            pending_len = sum(len(item) for item in pending)
            if pending_len >= min_chars:
                flush()
    if pending:
        flush()
    return chunks
