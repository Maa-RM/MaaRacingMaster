# -*- coding: utf-8 -*-
"""训练入口回归锁：帮助页必须在不装训练框架的环境下可用。

历史故障：训练脚本在解析参数前从不存在的归档路径导入数据划分工具，
直接运行与按模块启动都在请求帮助页阶段炸掉（模块找不到），模型维护入口整个
不可用（玩家运行期不受影响——生产链路不经过该脚本）。

本文件用子进程按文档所列两种入口各请求一次帮助页。刻意不 import 被测脚本
（那会复现同一故障）也不依赖训练框架：数据划分工具设计为纯标准库，被测脚本
对训练框架的导入都在参数解析之后。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
TRAIN = Path(__file__).resolve().parents[1] / "tools" / "training" / "train.py"
MODULE = "maaracing_master.plugins.infinite_drive.tools.training.train"


def _help(args: list[str]) -> subprocess.CompletedProcess[str]:
    assert TRAIN.is_file(), f"被测脚本不在预期位置: {TRAIN}"
    return subprocess.run([sys.executable, *args], cwd=REPO, capture_output=True,
                          text=True, timeout=120, check=False)


def test_train_help_direct_file_exits_zero():
    """按脚本文档直接运行：--help 打印用法并退出 0。"""
    r = _help([str(TRAIN), "--help"])
    assert r.returncode == 0, f"直接运行入口帮助页失败:\n{r.stderr}"
    assert "--name" in r.stdout, "输出不像帮助页（缺用法参数）"


def test_train_help_module_entry_exits_zero():
    """按模块启动：--help 打印用法并退出 0。"""
    r = _help(["-m", MODULE, "--help"])
    assert r.returncode == 0, f"模块启动入口帮助页失败:\n{r.stderr}"
    assert "--name" in r.stdout, "输出不像帮助页（缺用法参数）"
