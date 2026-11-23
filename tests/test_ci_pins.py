# -*- coding: utf-8 -*-
"""CI 依赖与发布运行时锁文件的版本一致性守卫。

Linux CI 装的 onnxruntime（CPU）与 opencv-python-headless，和发布包锁文件里的
onnxruntime-directml / opencv-python 是**不同发行包**，pip 的 -c 只约束同名包，
所以 requirements-ci.txt 必须各钉一行。本测试把这份不可避免的重复变成可机检的等式：
锁文件升版而 CI 没跟上，直接红。
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CI = REPO / "requirements-ci.txt"
LOCK = REPO / "scripts" / "release" / "requirements-runtime-lock.txt"


def _pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        name, _, version = line.partition("==")
        if version:
            pins[name.strip().lower()] = version.strip()
    return pins


def test_cpu_onnxruntime_matches_locked_directml_version() -> None:
    ci = _pins(CI)
    lock = _pins(LOCK)
    assert "onnxruntime" in ci, "requirements-ci.txt 必须显式钉 CPU 版 onnxruntime"
    assert ci["onnxruntime"] == lock["onnxruntime-directml"], (
        "onnxruntime(CPU) 必须与锁文件里的 onnxruntime-directml 同号："
        f"ci={ci['onnxruntime']} lock={lock['onnxruntime-directml']}"
    )


def test_headless_opencv_matches_locked_opencv_version() -> None:
    ci = _pins(CI)
    lock = _pins(LOCK)
    assert "opencv-python-headless" in ci, "requirements-ci.txt 必须显式钉 headless 版 OpenCV"
    assert ci["opencv-python-headless"] == lock["opencv-python"], (
        "opencv-python-headless 必须与锁文件里的 opencv-python 同号："
        f"ci={ci['opencv-python-headless']} lock={lock['opencv-python']}"
    )


def test_constrained_packages_are_not_pinned_twice() -> None:
    ci = _pins(CI)
    for name in ("maafw", "numpy"):
        assert name not in ci, (
            f"{name} 已由 `-c` 引锁文件约束，不要在 requirements-ci.txt 再钉一次"
        )
