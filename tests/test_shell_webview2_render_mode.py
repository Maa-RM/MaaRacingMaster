# -*- coding: utf-8 -*-
"""壳界面渲染模式的源码锁（实测改判：GPU 光栅 + 常规资料分区才能跨启动复用着色器缓存）。

锁四件事（主窗与悬浮预览窗两个控制点统一：控制选项是控制器级设置，共享环境不会
自动传播到另一窗口，只锁主窗不构成覆盖）：
1. 两个窗口都用控制选项显式关闭隐私模式——InPrivate 分区只有内存版 GPU 缓存，
   首访编译的着色器程序跨启动不命中，每次启动都要重付冷编译停顿；
2. 控制选项由环境对象自带的工厂创建（不可直接实例化，历史回归：投影无无参构造）；
3. 回落链保留：控制选项失败 → 共享环境 → 默认位置（页面可用性优先于渲染优化）；
4. 浏览器参数不内置 `--disable-gpu-rasterization`——软件光栅没有可复用的着色器
   缓存，GPU 光栅才是「首访编译一次、之后跨启动复用」的路径。

机理与实测见 apps/MaaRacingMaster.Shell/README.md「WebView2 渲染与编译缓存」。
静态锁只防代码回退；跨启动复用行为需真机两次启动实测。
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHELL = REPO / "apps" / "MaaRacingMaster.Shell"
MAIN = SHELL / "MainWindow.xaml.cs"
PEEP = SHELL / "PeepWindow.xaml.cs"
RUNTIME = SHELL / "WebView2Runtime.cs"


def test_regular_profile_before_ensure():
    for name, path in [("主窗", MAIN), ("悬浮窗", PEEP)]:
        src = path.read_text(encoding="utf-8-sig")
        assert "IsInPrivateModeEnabled = false" in src, (
            f"{name}必须显式使用常规资料分区（隐私分区缓存着色器不跨启动复用）")
        assert "IsInPrivateModeEnabled = true" not in src, (
            f"{name}不得再开启隐私模式（会让 GPU 着色器缓存每次启动重编）")
        options_idx = src.index("IsInPrivateModeEnabled = false")
        ensure_idx = src.index("EnsureCoreWebView2Async(env, options)")
        assert options_idx < ensure_idx, f"{name}的分区开关必须先于带选项的初始化调用"


def test_options_created_via_environment_factory():
    """控制选项类不可直接实例化（投影构造只收原生指针）：必须经环境工厂创建。"""
    for name, path in [("主窗", MAIN), ("悬浮窗", PEEP)]:
        src = path.read_text(encoding="utf-8-sig")
        assert "env.CreateCoreWebView2ControllerOptions()" in src, (
            f"{name}的控制选项必须由环境对象的工厂创建")
        assert "new CoreWebView2ControllerOptions" not in src, (
            f"{name}不得直接 new（历史回归：投影无无参构造，编译失败）")


def test_fallback_chain_preserved():
    """控制选项失败不阻断页面：回落链（共享环境 → 默认位置）必须完整保留。"""
    for name, path in [("主窗", MAIN), ("悬浮窗", PEEP)]:
        src = path.read_text(encoding="utf-8-sig")
        assert src.index("EnsureCoreWebView2Async(env, options)") < src.index(
            "EnsureCoreWebView2Async(env);"), f"{name}控制选项失败后必须回落共享环境"
        assert "EnsureCoreWebView2Async(null)" in src, (
            f"{name}显式环境失败后必须回落默认位置")


def test_no_builtin_gpu_rasterization_switch():
    """CPU 光栅没有可落的着色器缓存：内置参数表里不得再出现该开关。"""
    src = RUNTIME.read_text(encoding="utf-8-sig")
    assert "--disable-gpu-rasterization" not in src, (
        "不得内置 --disable-gpu-rasterization（软件光栅无法复用着色器缓存）")
    assert "MAARM_APP_WEBVIEW_ARGS" in src, "诊断参数 env 入口必须保留（追加而非覆盖）"
