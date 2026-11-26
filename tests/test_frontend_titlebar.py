"""标题栏窗口控制按钮：图标形状必须来自系统标题栏字形，而不是手画。

三道锁：
1. 结构：三个按钮各挂一个 10×10 视框的 `win-icon` 矢量，不许再出现字形文本（`win-glyph`）；
2. 出处：`apps/MaaRacingMaster.Shell/tools/extract_caption_glyphs.py --check` 逐条比对——index.html
   里的 path 必须和 `SegoeIcons.ttf`（回退 `segmdl2.ttf`）里 U+E921/E922/E923/E8BB 的轮廓一致，
   手改任何一个坐标都会红（改了就没圆角、也未必落在整像素上，正是「看着糙」的来源）；
3. 度量：图标 10×10、按钮 46×52，直角边落在整像素，圆角/斜线吃灰度抗锯齿。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO / "apps" / "MaaRacingMaster.Shell" / "frontend" / "index.html"
STYLE_CSS = REPO / "apps" / "MaaRacingMaster.Shell" / "frontend" / "style.css"
TOOL = REPO / "apps" / "MaaRacingMaster.Shell" / "tools" / "extract_caption_glyphs.py"
FONTS = (Path(r"C:\Windows\Fonts\SegoeIcons.ttf"), Path(r"C:\Windows\Fonts\segmdl2.ttf"))


def _controls(html: str) -> str:
    m = re.search(r'<div class="win-controls" id="win-controls">(.*?)\n    </div>', html, re.S)
    assert m, "标题栏里找不到 win-controls"
    return m.group(1)


def test_caption_buttons_use_generated_vectors():
    html = INDEX_HTML.read_text(encoding="utf-8")
    controls = _controls(html)
    assert "win-glyph" not in html, "图标不该再走字形文本（ClearType 会把 1px 笔画拆成彩边）"
    for btn_id in ("btn-win-min", "btn-win-max", "btn-win-close"):
        assert f'id="{btn_id}"' in controls, f"缺按钮 {btn_id}"
    boxes = re.findall(r'<svg class="win-icon[^"]*" viewBox="([^"]+)"', controls)
    assert boxes and all(b == "0 0 10 10" for b in boxes), f"图标视框不是 10×10：{boxes}"
    # 最大化按钮两态：常态 icon-max、最大化后 icon-restore
    assert controls.count('class="win-icon icon-max"') == 1
    assert controls.count('class="win-icon icon-restore"') == 1


@pytest.mark.skipif(not any(f.exists() for f in FONTS), reason="本机没有 Segoe 图标字体，无法比对轮廓")
def test_caption_paths_come_from_the_system_font():
    proc = subprocess.run(
        [sys.executable, str(TOOL), "--check", str(INDEX_HTML)],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert proc.returncode == 0, f"index.html 的图标路径和系统字形对不上：{proc.stderr or proc.stdout}"
    assert "ok" in proc.stdout


def test_caption_metrics_are_native():
    css = STYLE_CSS.read_text(encoding="utf-8")
    icon = css.split(".win-icon {", 1)[1].split("}", 1)[0]
    assert "width: 10px" in icon and "height: 10px" in icon
    assert "fill: currentColor" in icon and "stroke: none" in icon, "轮廓是填充路径，不该再描边"
    btn = css.split(".win-btn {", 1)[1].split("}", 1)[0]
    assert "width: 46px" in btn and "height: 100%" in btn, "按钮尺寸变了，字形与原生 caption 的等比就断了"


def test_maximized_state_still_swaps_the_two_icons():
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".win-btn--maximized .icon-max { display: none; }" in css
    assert ".win-btn--maximized .icon-restore { display: block; }" in css
    assert ".win-btn:not(.win-btn--maximized) .icon-restore { display: none; }" in css
