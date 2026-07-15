"""Ops report log parsing."""

from app.routers.ops import parse_reports


def test_parse_blocks_newest_last_and_critical_flag():
    text = (
        "已清殼: ['MAS-096']\n"
        "=== 2026-07-15 14:10 (+0800) 輪值報告(第8輪)===\n"
        "健康:OK\n驗證:6 支\n"
        "=== 2026-07-15 15:20 (+0800) 輪值報告(第9輪)===\n"
        "[CRITICAL→已補救] 某事故\n細節\n"
        "计时器输出附在这里\n"
    )
    blocks = parse_reports(text)
    assert len(blocks) == 3
    assert blocks[0].header == "(未分段紀錄)"
    assert "已清殼" in blocks[0].body
    assert blocks[1].header.endswith("(第8輪)")
    assert blocks[1].critical is False
    assert blocks[2].critical is True
    assert "计时器输出" in blocks[2].body


def test_parse_empty():
    assert parse_reports("") == []
