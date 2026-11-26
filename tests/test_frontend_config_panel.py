# -*- coding: utf-8 -*-
"""配置面板通用渲染器的静态锁。

锁四件事：
1. 壳里不再留模块专属的静态面板块与摘要函数（插件自述、宿主渲染）；
2. 契约里每种控件类型都有对应的渲染分支（新增类型忘了前端会红）；
3. 渲染器只用 textContent，且带 _rev 守卫（旧轮询响应不得覆盖新状态）；
4. wireSwitchAria 保留显式标题/说明 id（文案更新函数要靠它找人）。

为什么是静态锁：前端没有 DOM 测试链；这些契约可从源码文本稳定判定。时序纯逻辑另由
tests/test_frontend_js_logic.py 跑 node --test 覆盖。
"""
from __future__ import annotations

from pathlib import Path

from maaracing_master.core.config_ui_contract import FIELD_TYPES

REPO = Path(__file__).resolve().parent.parent
FRONTEND = REPO / "apps" / "MaaRacingMaster.Shell" / "frontend"
HTML = FRONTEND / "index.html"
SETTINGS = FRONTEND / "js" / "settings.js"
RUN = FRONTEND / "js" / "run.js"
PANEL = FRONTEND / "js" / "config-panel.js"
PILL = FRONTEND / "js" / "number-pill.js"
STATE = FRONTEND / "js" / "config-panel-state.js"

MODULE_TOKENS = (
    "drive_mode", "record_mode", "macro_group", "macro_mode", "straight_mode",
    "max_daily_loops", "target_session",
)


def _js_function_body(src: str, signature: str) -> str:
    start = src.index(signature)
    brace = src.index("{", start)
    depth = 0
    for index in range(brace, len(src)):
        if src[index] == "{":
            depth += 1
        elif src[index] == "}":
            depth -= 1
            if depth == 0:
                return src[brace:index + 1]
    raise AssertionError(f"找不到 {signature} 的函数体结尾（源码结构变了？）")


def test_static_module_panels_are_gone():
    html = HTML.read_text(encoding="utf-8")
    assert 'id="module-options"' not in html
    assert "opt-record-mode" not in html
    assert "opt-drive-mode" not in html
    assert 'src="js/config-panel-state.js"' in html
    assert 'src="js/config-panel.js"' in html
    assert html.index("js/config-panel.js") < html.index('src="js/run.js"')


def test_every_contract_field_type_has_a_renderer():
    src = PANEL.read_text(encoding="utf-8")
    for field_type in FIELD_TYPES:
        assert f"case '{field_type}':" in src, f"缺少 {field_type} 的渲染分支"


def test_host_files_carry_no_module_field_names_or_summary():
    for src in (SETTINGS.read_text(encoding="utf-8"), RUN.read_text(encoding="utf-8")):
        for token in MODULE_TOKENS:
            assert token not in src, f"宿主仍硬编码模块字段 {token}"
    assert "summary: (cfg)" not in RUN.read_text(encoding="utf-8")
    assert "_summary" in RUN.read_text(encoding="utf-8")


def test_summary_chips_render_without_expanding_card():
    """任务卡摘要 chips 与展开态无关：列表渲染时就取配置。

    chips 的数据源是 get_module_config 的 `_summary`；若只在展开时拉取，收起卡片的
    标题一直空着，点开才补上。
    """
    body = _js_function_body(RUN.read_text(encoding="utf-8"), "function renderTaskList")
    assert "!isBuiltin(mid) && state.expanded[mid]" not in body, \
        "活动模块取配置又被展开态挡住了：收起卡片的摘要 chips 会缺席"
    assert "!state.cfg[mid] || state.expanded[mid]" in body
    assert "ensureConfig(mid)" in body


def test_panel_renders_text_only_and_guards_revisions():
    src = PANEL.read_text(encoding="utf-8")
    assert "innerHTML" not in src
    assert "textContent" in src
    assert "acceptResponse" in src
    assert "_ui_note" in src
    state_src = STATE.read_text(encoding="utf-8")
    for token in ("lastRev", "acceptResponse", "isAlive", "pending"):
        assert token in state_src


def test_panel_renders_option_badges_and_notice_actions():
    src = PANEL.read_text(encoding="utf-8")
    for token in ("seg-badge", "field-notice", "download_release",
                  "open_user_data_folder", "subpath"):
        assert token in src, f"配置面板缺少 {token} 渲染/动作分支"
    assert "innerHTML" not in src


def test_stepper_renderer_rolls_and_estimates():
    """数字胶囊：弹簧滚动、spinbutton 语义、预计用时乘算、落定才写后端、原地编辑。

    胶囊本体 2026-11 起独立成 js/number-pill.js（宿主所有数字字段共用），
    所以「怎么滚」在胶囊文件里锁，「怎么接字段」在渲染器里锁。
    """
    pill = PILL.read_text(encoding="utf-8")
    for token in ("maarm-stepper", "prefers-reduced-motion", "spinbutton",
                  "aria-valuenow", "requestAnimationFrame", "flush",
                  "stepper-edit", "stepper-window--editing", "HOLD_DELAY_MS"):
        assert token in pill, f"数字胶囊缺少 {token}"
    src = PANEL.read_text(encoding="utf-8")
    for token in ("unit_seconds", "formatEstimateSeconds", "numberPill"):
        assert token in src, f"配置面板缺少 {token}"
    css = (FRONTEND / "style.css").read_text(encoding="utf-8")
    assert "stepper-col--hidden" in css
    assert "stepper-window--plain" in css
    assert "@media (prefers-reduced-motion: reduce)" in css


def test_numeric_fields_share_one_pill():
    """数字字段只有一个实现：插件 number / stepper 与内置步骤的秒数都走数字胶囊。

    改造前内置步骤是裸 input[type=number]（后缀在框外、不能按住调），插件面板另有
    「常驻输入」与「滚轮」两套胶囊——三处外观与写入时机各不相同。这里锁住合并的结果：
    裸 number 输入与旧的两个构建函数都不得回潮。
    """
    run = RUN.read_text(encoding="utf-8")
    assert 'type="number"' not in run, "内置步骤又用回了裸 number 输入"
    assert "numberPill.create" in run, "内置步骤没走数字胶囊"
    assert 'data-num="' in run, "内置步骤的数字字段缺少胶囊挂载位"
    panel = PANEL.read_text(encoding="utf-8")
    assert "buildNumber" not in panel and "buildStepper" not in panel, \
        "插件面板又分裂出第二套数字控件"
    assert "numberPill.create" in panel
    html = HTML.read_text(encoding="utf-8")
    assert 'src="js/number-pill.js"' in html
    assert html.index("js/number-pill.js") < html.index("js/config-panel.js"), \
        "胶囊必须早于使用方加载（两个使用方都在加载期取用）"
    assert html.index("js/number-pill.js") < html.index('src="js/run.js')
    css = (FRONTEND / "style.css").read_text(encoding="utf-8")
    assert ".maarm-field-control .field-suffix" not in css, "框外后缀位已被胶囊内后缀取代"
    assert "maarm-stepper--plain" not in css


def test_number_pill_dimensions_come_from_tokens():
    """胶囊的尺寸只有一个真源：CSS token 给像素换算，JS 不再存副本。

    改造前 30px 格高与 0.62em 列宽在 style.css 与 config-panel.js 各有一份，
    改一处另一处就错（列位移与编辑框宽度会立刻不对齐）。现在 JS 只写「第几格」
    与「几位」，px / em 全由 --maarm-num-cell-h / --maarm-num-col-w 换算。
    """
    css = (FRONTEND / "style.css").read_text(encoding="utf-8")
    for token in ("--maarm-control-h: 36px", "--maarm-num-cell-h: 30px",
                  "--maarm-num-col-w: 0.62em"):
        assert token in css, f"缺少控件刻度 token {token}"
    assert "calc(var(--slot, 0) * -1 * var(--maarm-num-cell-h))" in css, \
        "列位移没有交给 token 换算（JS 会重新存一份 30px）"
    pill = PILL.read_text(encoding="utf-8")
    assert "var(--maarm-num-col-w)" in pill, "编辑框宽度没引用列宽 token"
    assert ".62em" not in pill, "列宽在 JS 里存了第二份"
    assert "30px" not in pill, "格高在 JS 里存了第二份"
    assert "translate3d" not in pill, "列位移又写回了 JS（像素应交给 CSS token）"


def test_panel_layout_is_host_owned():
    """行/组布局归宿主：插件只声明字段语义，面板不提供可折叠分组。

    布局一旦散进模块声明，后人加字段就会各自调间距；这里锁住渲染器必须走
    统一的 .cfg-group/.cfg-row 结构，任务设置标题与折叠组不得回潮。
    """
    panel = PANEL.read_text(encoding="utf-8")
    for token in ("cfg-group", "cfg-group-title", "cfg-row",
                  "cfg-row-label", "cfg-row-control", "cfg-row-desc"):
        assert token in panel, f"配置面板缺少宿主行结构 {token}"
    css = (FRONTEND / "style.css").read_text(encoding="utf-8")
    for token in (".cfg-row {", ".cfg-group + .cfg-group", ".cfg-row-desc"):
        assert token in css, f"样式缺少宿主行布局 {token}"
    run = RUN.read_text(encoding="utf-8")
    assert "任务设置" not in run, "任务设置标题已并入分组，不要再加回来"
    assert "cfg-row cfg-row--switch" in run, "内置步骤开关未走宿主行结构"


def test_switch_aria_keeps_explicit_title_and_desc_ids():
    body = _js_function_body(SETTINGS.read_text(encoding="utf-8"), "function wireSwitchAria")
    assert "if (!title.id)" in body
    assert "if (!desc.id)" in body
