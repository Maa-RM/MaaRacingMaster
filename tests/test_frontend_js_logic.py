# -*- coding: utf-8 -*-
"""前端纯逻辑（`frontend/js/*.js` 里不碰 DOM 的部分）的 Node 测试入口。

规则本身不碰 DOM，单独用 node --test 覆盖：配置面板时序（旧轮询晚到、保存串行、
卡片代际作废）与状态行取词（阶段名 / 运行项名，不取导航图节点名）。新增一份纯逻辑
测试只需在 `tests/js/` 下放 `*.test.mjs`，本入口自动带上。本地没装 node 时跳过；
CI 上缺 node 直接失败，避免这段门禁静默空过。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
JS_DIR = REPO / "tests" / "js"
NODE = shutil.which("node")
CASES = sorted(p.name for p in JS_DIR.glob("*.test.mjs"))
IS_CI = os.environ.get("CI") == "true"


def test_frontend_logic_cases_exist():
    assert CASES, "tests/js 下没有找到任何 *.test.mjs，纯逻辑测试会静默空过"


@pytest.mark.parametrize("name", CASES)
def test_frontend_logic(name):
    if NODE is None:
        # 本地没装 node 可以跳过；CI 上静默跳过等于没有这段门禁
        if IS_CI:
            pytest.fail("CI 环境必须提供 node：前端纯逻辑测试不能被静默跳过")
        pytest.skip("node 不可用，跳过前端纯逻辑测试")
    proc = subprocess.run(
        [NODE, "--test", str(JS_DIR / name)],
        cwd=REPO, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
