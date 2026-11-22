# -*- coding: utf-8 -*-
"""插件测试基础设施回归锁：conftest 推导的仓库根必须指向真实仓库根。

历史瑕疵：层级多算了一层（数到仓库的上一级），因项目包总能从工作目录或
环境解析到而被掩盖，插入的错误路径一直是死条目——直到某天换一种启动方式
（不经过项目包可解析的入口）才会以「找不到项目代码」的形式炸出来，且报错
不指向病根。本用例直接锁这个推导结果。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_CONFTEST = Path(__file__).resolve().parent / "conftest.py"


def test_conftest_repo_root_points_at_repo():
    spec = importlib.util.spec_from_file_location(
        "_infinite_drive_conftest_probe", _CONFTEST)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    expected = Path(__file__).resolve().parents[4]
    assert probe._REPO == expected, f"conftest 推导的仓库根指偏了：{probe._REPO}"
    assert (probe._REPO / "pyproject.toml").is_file(), "推导结果不是仓库根"
