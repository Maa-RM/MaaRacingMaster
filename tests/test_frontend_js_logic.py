# -*- coding: utf-8 -*-
"""前端纯逻辑（`frontend/js/*.js` 里不碰 DOM 的部分）的 Node 测试入口。

规则本身不碰 DOM，单独用 node --test 覆盖：配置面板时序（旧轮询晚到、保存串行、
卡片代际作废）与状态行取词（阶段名 / 运行项名，不取导航图节点名）。新增一份纯逻辑
测试只需在 `tests/js/` 下放 `*.test.mjs`，本入口自动带上。CI 不装 node 时自动跳过
（本地开发机有 node 即真实执行）。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
JS_DIR = REPO / "tests" / "js"
NODE = shutil.which("node")
CASES = sorted(p.name for p in JS_DIR.glob("*.test.mjs"))


@pytest.mark.skipif(NODE is None, reason="node 不可用，跳过前端纯逻辑测试")
@pytest.mark.parametrize("name", CASES)
def test_frontend_logic(name):
    proc = subprocess.run(
        [NODE, "--test", str(JS_DIR / name)],
        cwd=REPO, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
