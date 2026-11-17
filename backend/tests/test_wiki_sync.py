from __future__ import annotations

import json

import pytest

from app.core.errors import AppError, ForbiddenError
from app.core.security import CurrentUser, Role
from app.domain.wiki import (
    chunk_article,
    extract_categories,
    first_paragraph,
    normalize_text,
    split_sections,
)
from app.services.wiki_parser import iter_package_entries, open_package, parse_wikiextractor_line
from app.services.wiki_reads import WikiReadService


def _line(page_id: str, title: str, text: str, **extra: object) -> str:
    payload = {
        "id": page_id,
        "revid": f"rev-{page_id}",
        "title": title,
        "url": f"https://wiki.example/{page_id}",
        "text": text,
        **extra,
    }
    return json.dumps(payload, ensure_ascii=False)


def test_normalize_text_collapses_line_endings_and_blank_lines() -> None:
    assert normalize_text("  标题\r\n\r\n\r\n正文  \r\n") == "标题\n\n正文"


def test_extract_categories_removes_metadata_line() -> None:
    categories, body = extract_categories("正文\n\nCATEGORIES: 技术, 数据库，架构")

    assert categories == ["技术", "数据库", "架构"]
    assert body.strip() == "正文"


def test_split_sections_tracks_heading_stack() -> None:
    content = "导语\n\n== 背景 ==\n\n背景正文\n\n=== 细节 ===\n\n细节正文"

    blocks = split_sections(content)

    assert [path for path, _ in blocks] == [(), ("背景",), ("背景", "细节")]
    assert [text for _, text in blocks] == ["导语", "背景正文", "细节正文"]


def test_first_paragraph_skips_heading() -> None:
    assert first_paragraph("== 概述 ==\n\n第一段正文\n\n第二段") == "第一段正文"


def test_chunk_article_respects_headings_and_limits() -> None:
    content = "\n\n".join(
        [
            "甲" * 120,
            "== 背景 ==",
            "乙" * 120,
            "== 方案 ==",
            "丙" * 120,
        ]
    )

    chunks = chunk_article(content, min_chars=80, max_chars=150, overlap_chars=10)

    assert [chunk.heading_path for chunk in chunks] == [(), ("背景",), ("方案",)]
    assert all(chunk.text for chunk in chunks)
    assert all(len(chunk.text) <= 150 + 10 for chunk in chunks)


def test_chunk_article_splits_long_paragraph_with_overlap() -> None:
    content = "字" * 250

    chunks = chunk_article(content, min_chars=50, max_chars=100, overlap_chars=20)

    assert len(chunks) >= 3
    assert all(len(chunk.text) <= 100 for chunk in chunks[:2])
    assert chunks[1].text.startswith(chunks[0].text[-20:])


def test_parse_wikiextractor_line_requires_identity() -> None:
    with pytest.raises(AppError) as exc:
        parse_wikiextractor_line(json.dumps({"title": "缺 ID", "text": "正文"}))

    assert exc.value.code == "WIKI_PACKAGE_INVALID"


def test_parse_wikiextractor_line_normalizes_content() -> None:
    record = parse_wikiextractor_line(
        _line("1", "词条", "== 概述 ==\r\n\r\n正文\r\n\r\nCATEGORIES: 分类A, 分类B", lang="zh")
    )

    assert record.content == "== 概述 ==\n\n正文"
    assert record.categories == ["分类A", "分类B"]
    assert record.summary == "正文"
    assert record.redirected is False
    assert len(record.content_hash) == 64


def test_parse_wikiextractor_line_supports_redirect() -> None:
    record = parse_wikiextractor_line(_line("2", "重定向", "", redirect=True))

    assert record.redirected is True
    assert record.content == ""


def test_open_package_and_resume_from_cursor(tmp_path) -> None:
    dump = tmp_path / "dump.jsonl"
    dump.write_text(
        "\n".join(
            [
                _line("1", "词条一", "正文一"),
                "{not json",
                _line("2", "词条二", "正文二"),
            ]
        ),
        encoding="utf-8",
    )

    with open_package(str(tmp_path), max_bytes=1024 * 1024) as view:
        assert view.total_count == 3
        entries = list(iter_package_entries(view))

    assert entries[0].record is not None and entries[0].record.page_id == "1"
    assert entries[1].record is None and entries[1].error
    assert entries[2].record is not None and entries[2].record.page_id == "2"

    with open_package(str(dump), max_bytes=1024 * 1024) as view:
        resumed = list(iter_package_entries(view, start_after=entries[0].cursor))

    assert [entry.cursor for entry in resumed] == [entries[1].cursor, entries[2].cursor]


def test_open_package_rejects_unknown_cursor(tmp_path) -> None:
    dump = tmp_path / "dump.jsonl"
    dump.write_text(_line("1", "词条一", "正文一"), encoding="utf-8")

    with open_package(str(dump), max_bytes=1024 * 1024) as view:
        with pytest.raises(AppError) as exc:
            list(iter_package_entries(view, start_after="missing.jsonl:99"))

    assert exc.value.code == "VALIDATION_ERROR"


def test_open_package_rejects_s3() -> None:
    with pytest.raises(AppError) as exc:
        with open_package("s3://bucket/dump.jsonl", max_bytes=1024):
            pass

    assert exc.value.code == "VALIDATION_ERROR"


def test_viewer_cannot_request_inactive_wiki_content() -> None:
    viewer = CurrentUser(subject="viewer", roles=frozenset({Role.VIEWER}))
    operator = CurrentUser(subject="operator", roles=frozenset({Role.OPERATOR}))

    assert WikiReadService._allow_inactive(viewer, False) is False
    with pytest.raises(ForbiddenError):
        WikiReadService._allow_inactive(viewer, True)
    assert WikiReadService._allow_inactive(operator, True) is True

