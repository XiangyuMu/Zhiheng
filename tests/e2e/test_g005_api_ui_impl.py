from __future__ import annotations

from pathlib import Path


def test_g005_ui_uses_plain_user_facing_copy_and_no_demo_data() -> None:
    root = Path("src/zhiheng/api/static")
    html = (root / "knowledge-agent.html").read_text(encoding="utf-8")
    js = (root / "knowledge-agent.js").read_text(encoding="utf-8")

    assert "知识助理" in html
    assert "只使用授权证据" in html
    assert "不执行外部动作" in html
    assert "demo" not in js.lower()
    assert "/v1/answers" in js
    assert "/v1/decisions/analyze" in js
    assert "/v1/knowledge-gaps" in js
    assert "/v1/knowledge/pdf-imports/" in js
    assert "/v1/knowledge/import-tasks" in js
    assert "parsed_page_count" in js
    assert "table_count" in js
    assert "image_count" in js
    assert "打开 PDF 原页" in js
    assert "PDF 将保留原文件" in html
    assert "本次保存文本，不提供原 PDF 页面阅读" not in html
    assert "50 * 1024 * 1024" in js
