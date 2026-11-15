# -*- coding: utf-8 -*-
"""壳界面隐私模式的源码锁（评审裁决：浏览数据不落盘，持久目录不再累积几十 MB 缓存）。

锁三件事：
1. 界面初始化用控制选项开启隐私模式（浏览数据不写盘）；
2. 隐私模式由环境对象自带的工厂创建（控制选项类不可直接实例化，历史回归：
   直接 new 编译失败）；
3. 回落链保留：隐私模式失败 → 共享环境 → 默认位置（页面可用性优先于隐私开关）。

真实落盘行为需提权启动实测（隐私模式下目录不增长），静态锁只防代码回退。
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAIN = REPO / "apps" / "MaaRacingMaster.Shell" / "MainWindow.xaml.cs"


def test_private_mode_enabled_before_ensure():
    src = MAIN.read_text(encoding="utf-8-sig")
    assert "IsInPrivateModeEnabled = true" in src, "界面必须显式开启隐私模式"
    options_idx = src.index("IsInPrivateModeEnabled = true")
    ensure_idx = src.index("EnsureCoreWebView2Async(env, options)")
    assert options_idx < ensure_idx, "隐私开关必须先于带选项的初始化调用"


def test_options_created_via_environment_factory():
    """控制选项类不可直接实例化（投影构造只收原生指针）：必须经环境工厂创建。"""
    src = MAIN.read_text(encoding="utf-8-sig")
    assert "env.CreateCoreWebView2ControllerOptions()" in src, (
        "控制选项必须由环境对象的工厂创建")
    assert "new CoreWebView2ControllerOptions" not in src, (
        "不得直接 new（历史回归：投影无无参构造，编译失败）")


def test_fallback_chain_preserved():
    """隐私模式失败不阻断页面：回落链（共享环境 → 默认位置）必须完整保留。"""
    src = MAIN.read_text(encoding="utf-8-sig")
    assert src.index("EnsureCoreWebView2Async(env, options)") < src.index(
        "EnsureCoreWebView2Async(env);"), "隐私模式失败后必须回落共享环境"
    assert "EnsureCoreWebView2Async(null)" in src, "显式环境失败后必须回落默认位置"
