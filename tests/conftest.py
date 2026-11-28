# -*- coding: utf-8 -*-
"""pytest 全局配置。

把项目根加入 sys.path，使 `tools.pipeline...` 等仓库内导入在 CI（`pytest` 不带
`python -m`）下也可导入——cwd 非 project root 时不自动在 sys.path。

插件代码的测试一律走绝对包路径导入（maaracing_master.plugins.<id>.…）：
包 __init__ 与插件 __init__ 均为轻依赖（实测不拉 maa/opencv），且这是与生产
代码一致的唯一导入口径——不再为个别测试保留 sys.path 平铺导入的旁门。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 测试进程不走 maaracing_master 包入口，无法继承 __init__.py 的禁用；
# 此处同样关闭字节码写入，避免 tests/ 与直导目录散落 __pycache__。
sys.dont_write_bytecode = True

_PROJ = Path(__file__).resolve().parent.parent

# 理由见文件头 docstring（仓库内导入在 CI 下也可导入）。
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))


@pytest.fixture(autouse=True)
def _launcher_check_off(monkeypatch):
    """默认关闭启动器版本检查闸门（MAARM_APP_LAUNCHER_CHECK，运行时覆盖开关）。

    装了游戏的开发机跑单测时，开着闸门会真的拉起官方启动器（弹 UAC、联网）。
    测试一律从关闭态出发；要测闸门的用例自行 setenv 打开，并打桩
    game_process 的启动器交互函数。
    """
    monkeypatch.setenv("MAARM_APP_LAUNCHER_CHECK", "0")


@pytest.fixture
def pure_maa(monkeypatch):
    """用 Python 桩替代导航图的 Maa 工厂，避免创建原生句柄。"""
    from maaracing_master.core import nav_graph as ng

    class _NoInit:
        def __init__(self, *args, **kwargs):
            pass

    for name in ("CustomController", "CustomAction", "CustomRecognition", "PipelineLogger"):
        monkeypatch.setattr(ng, name, _NoInit)
    monkeypatch.setattr(ng, "Resource", lambda: MagicMock())
    monkeypatch.setattr(ng, "Tasker", lambda: MagicMock())
