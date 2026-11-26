# -*- coding: utf-8 -*-
"""关于页「开源许可」入口的静态锁。

入口读的是随包发布的 THIRD_PARTY_LICENSES.md（安装目录内容），按外部内容处理：
C# 侧限长读文件、缺件如实报错，前端整段转义后放进 <pre>，不参与 HTML 解析。

真正的链路是打开应用点一次按钮；本文件的价值是让这条路径在 CI 就被钉住
（入口、宿主动作、转义、读取上限任一被回退即红）。
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHELL = REPO / "apps" / "MaaRacingMaster.Shell"
INDEX = SHELL / "frontend" / "index.html"
APP_JS = SHELL / "frontend" / "app.js"
RPC_BRIDGE = SHELL / "RpcBridge.cs"


def test_about_page_has_license_entry():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="btn-open-licenses"' in html, "关于页缺少开源许可入口"
    assert "开源许可" in html, "入口文案缺失"


def test_license_text_is_escaped_before_modal():
    js = APP_JS.read_text(encoding="utf-8")
    assert "maarm.call('read_licenses')" in js, "入口未接宿主动作"
    assert "'<pre class=\"maarm-license\">' + escHtml(text)" in js, (
        "许可全文未转义就拼进弹窗"
    )


def test_host_reads_bundled_license_with_a_cap():
    cs = RPC_BRIDGE.read_text(encoding="utf-8")
    assert '"read_licenses"' in cs, "宿主未登记开源许可动作"
    assert "THIRD_PARTY_LICENSES.md" in cs, "未读取随包许可文件"
    assert "MaxLicenseBytes" in cs, "缺少读取上限"
