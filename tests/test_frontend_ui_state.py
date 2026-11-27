# -*- coding: utf-8 -*-
"""壳前端的「状态复位与错误落点」静态锁。

**为什么是静态锁**：这三个缺陷都要在真实界面上点几步才看得到（切模块、下载失败、
键盘打开成就页），但它们的判据是稳定的代码关系——状态挂在谁身上、重建时有没有复位、
错误写进哪个节点。放在这里让回退在 CI 即红。

**空过防护**：每处抽取都断言抽到锚点串，抽取规则失效时测试报错而不是静默通过。
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FRONTEND = REPO / "apps" / "MaaRacingMaster.Shell" / "frontend"


def _read(path: Path) -> str:
    src = path.read_text(encoding="utf-8")
    assert src, f"{path.name} 为空，抽取规则已失效"
    return src


def test_module_rerender_resets_preview_state():
    """预览放大态挂在 .page 上、句柄指向旧卡片：模块页重建（innerHTML 替换）前必须
    复位，否则新卡没进放大态、页面却仍按全屏排布（左栏与看板被藏掉且没有退出入口）。
    浮动态是宿主侧状态，重建后要按当前模式重放给新卡。"""
    settings = _read(FRONTEND / "js" / "settings.js")
    preview = _read(FRONTEND / "js" / "preview.js")
    assert "resetPreviewState" in preview and "applyPreviewState" in preview, "预览层未提供复位/重放入口"
    assert "preview-fs-active" in preview, "复位实现里没清放大态"

    start = settings.index("function renderModulePages(moduleId)")
    body = settings[start:start + 2200]
    reset_at = body.index("MaaRM.resetPreviewState()")
    inner_at = body.index("dataPage.innerHTML = defaultDataCards(moduleId)")
    assert reset_at < inner_at, "重建前没有先复位预览态"
    assert "MaaRM.applyPreviewState()" in body, "重建后未给新卡重放浮动态"


def test_install_failure_writes_to_text_not_the_row():
    """安装失败的行内落点必须是文字节点：传整行容器会被 setInlineStatus 的 textContent
    清空，「一键安装 / 取消」按钮一起消失且本会话无法恢复。"""
    panel = _read(FRONTEND / "js" / "config-panel.js")
    marker = "MaaRM.reportError('inline', label + '安装操作失败"
    start = panel.index(marker)
    call = panel[start:start + 220]
    assert "nodes.installText" in call, "安装失败仍落在整行容器上（会清掉按钮）"
    assert "nodes.install }" not in call, "安装失败仍把整行当状态元素"


def test_install_entry_hides_with_mode_and_offers_removal():
    """资产入口只在该档位可见；装好后按钮变成删除，删除走白名单 RPC 并先确认。"""
    panel = _read(FRONTEND / "js" / "config-panel.js")
    assert "evalCond(resp.visible_if, entry.cfg)" in panel, (
        "安装入口没有按插件声明的条件显隐（全程直行档仍会显示）")
    assert "removable ? '删除' + label : '一键安装' + label" in panel, (
        "已装好时按钮仍是安装入口")
    assert "install_asset_remove" in panel, "删除动作没有接后端白名单 RPC"
    assert "删除后需要重新安装" in panel, "删除前没有确认提示"
    # 入口排在状态行之前：空状态行不再把入口推到看不见的空白之后
    assert panel.index("nodes.install = install") < panel.index("nodes.status = status")
    css = _read(FRONTEND / "style.css")
    assert ".module-options-status:empty" in css, "空状态行仍占位"


def test_achievement_sheet_takes_and_returns_focus():
    """成就管理是整页覆盖层：打开要把焦点请进去（否则 Tab 先穿过背后的设置页），
    关闭要把焦点还给打开它的那个按钮（否则落在 body）。Tab/Shift+Tab 必须在层内回环，
    否则第二次 Shift+Tab 就会走进被覆盖的设置页。"""
    src = _read(FRONTEND / "js" / "achievement.js")
    start = src.index("function open()")
    body = src[start:start + 900]
    assert "returnFocus = document.activeElement" in body, "打开时未记录焦点来源"
    assert "'ach-back'" in body and ".focus()" in body, "打开后未把焦点移进层内"
    assert "trapSheetTab" in body, "打开后未接管 Tab 焦点环"

    close_start = src.index("function close()")
    close_body = src[close_start:close_start + 800]
    assert "returnFocus.focus()" in close_body, "关闭后未归还焦点"
    assert "removeEventListener('keydown', trapSheetTab)" in close_body, (
        "关闭后未解绑 Tab 焦点环")

    trap = src[src.index("function trapSheetTab(e)"):]
    trap = trap[:trap.index("\n  }\n") + 4]
    assert "e.shiftKey" in trap and "last.focus()" in trap, "Shift+Tab 未回环到末尾"
    assert "first.focus()" in trap, "Tab 未回环到开头"
    assert "sheet.contains(document.activeElement)" in trap, (
        "焦点已跑到层外时未收回层内")
