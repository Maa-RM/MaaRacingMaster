# -*- coding: utf-8 -*-
"""无限驾驶插件测试的本地配置。

插件自包含：回归锁随插件目录走（形态同归档插件 tests/）。本目录在默认收集面内
（`pyproject.toml` 的 `testpaths` 与 CI 均含本路径）；单独运行：

    .venv/Scripts/python.exe -m pytest maaracing_master/plugins/infinite_drive/tests
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True

_REPO = Path(__file__).resolve().parents[4]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))