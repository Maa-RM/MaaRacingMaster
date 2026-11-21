# -*- coding: utf-8 -*-
"""开局视角守卫：用**车道线反解的相机高**判断当前是否是保险杠机位。

只做状态机，不碰手柄与帧：模块层负责喂样本、按 RB；本模块可离线单测。
判据来自 `lane_obs._cam_h_from_lines`（两条不同道号的线在同一行的像素间距
反解 `cam_h`），**不读深度路面平面的 c**——v5 合成点图把贴地像素的高度钉在
Y≡cam_h，平面 c 恒等于 1.25，读它等于读常数（2026-11-12 实机 18 帧逐帧核对）。
保险杠视角相机高度中位约 1.25m（权威口径 2026-11-11）；现役只用保险杠视角：
游戏开局会把机位打回追尾，按 RB 切换的唯一目的就是切回保险杠（追尾视角
历史读数约 1.6–2.0 m，线几何在撞车段实测段中位 1.605）。

**无判据 ≠ 视角错误**：线观测未启用、或一直给不出可算的相机高时，模块层按
「无法判定」放行（不切视角、不中止阶段）；只有真的量出三次都在带外才 FAIL。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass


@dataclass(frozen=True)
class ViewGuardConfig:
    enabled: bool = False
    cam_h_lo_m: float = 1.12
    cam_h_hi_m: float = 1.55
    sample_s: float = 0.7
    settle_s: float = 0.25
    max_views: int = 3
    min_samples: int = 3
    deadline_s: float = 10.0     # 从首个有效样本起算：覆盖 3 个视角的切换+采样
    arm_timeout_s: float = 8.0   # 首个有效样本的最长等待（加载/冷启动不计入视角预算）

    def __post_init__(self) -> None:
        if not 0.0 < self.cam_h_lo_m < self.cam_h_hi_m:
            raise ValueError(f"view_guard 相机高度带非法：{self.cam_h_lo_m}"
                             f"..{self.cam_h_hi_m}")
        if self.sample_s <= 0 or self.settle_s < 0:
            raise ValueError(f"view_guard 采样窗非法：{self.sample_s}/"
                             f"{self.settle_s}")
        if self.max_views < 1 or self.min_samples < 1:
            raise ValueError(f"view_guard 计数非法：{self.max_views}/"
                             f"{self.min_samples}")
        if self.deadline_s <= self.sample_s:
            raise ValueError(f"view_guard 截止 {self.deadline_s}s 必须大于"
                             f"采样窗 {self.sample_s}s")
        if self.arm_timeout_s <= 0:
            raise ValueError(f"view_guard 首个样本等待非法：{self.arm_timeout_s}")


class ViewGuard:
    """一个阶段的视角确认状态机（成功/失败即终态，样本不跨视角累积）。"""

    WAIT = "wait"
    SWITCH = "switch"
    OK = "ok"
    FAIL = "fail"
    GRACE_S = 0.4   # 样本不足时窗口只延长一次：线观测冷启动前几拍可能无解

    def __init__(self, cfg: ViewGuardConfig, t0: float) -> None:
        self.cfg = cfg
        self.t0 = float(t0)
        self.state = "collect"
        self.armed_at: float | None = None
        self.window_start: float | None = None
        self.grace_until: float | None = None
        self.settle_until = 0.0
        self.samples: list[float] = []
        self.views = 1                 # 当前是第几个视角（1 起）
        self.median: float | None = None
        self.first_sample_at: float | None = None

    def update(self, now: float, cam_h: float | None,
               new: bool) -> str:
        """喂一拍；``new``=本拍是否有新的线观测检测帧（复用帧不重复计样本）。

        产线调用方（module._tick_view_guard）在调用前先处理「一直拿不到样本」
        的情形（按无法判定放行）——所以本类的 arm 超时 FAIL 只在直接使用者里
        成立；量出三次带外仍然走 FAIL。"""
        if self.state in (self.OK, self.FAIL):
            return self.state
        # 截止从首个有效样本起算：游戏加载/深度冷启动不占视角预算
        if self.first_sample_at is None:
            if now - self.t0 > self.cfg.arm_timeout_s:
                self.state = self.FAIL
                return self.FAIL
        elif now - self.first_sample_at > self.cfg.deadline_s:
            self.state = self.FAIL
            return self.FAIL
        if self.state == "settle":
            if now < self.settle_until:
                return self.WAIT
            self.state = "collect"
            self.armed_at = None
            self.window_start = None
            self.grace_until = None
            self.samples = []
        if new:
            if self.armed_at is None:
                self.armed_at = now
            if cam_h is not None and cam_h > 0.0:
                if self.first_sample_at is None:
                    self.first_sample_at = now
                if self.window_start is None:
                    self.window_start = now
                self.samples.append(float(cam_h))
        if (self.window_start is not None
                and now - self.window_start >= self.cfg.sample_s):
            if len(self.samples) < self.cfg.min_samples:
                if self.grace_until is None:
                    self.grace_until = (self.window_start
                                        + self.cfg.sample_s + self.GRACE_S)
                if now < self.grace_until:
                    return self.WAIT
            return self._decide(now)
        # 整个窗口一个有效样本都没有（线几何算不出相机高）：按坏视角处理
        if (self.window_start is None and self.armed_at is not None
                and now - self.armed_at >= self.cfg.sample_s):
            if self.grace_until is None:
                self.grace_until = (self.armed_at + self.cfg.sample_s
                                    + self.GRACE_S)
            if now < self.grace_until:
                return self.WAIT
            return self._decide(now)
        return self.WAIT

    def _decide(self, now: float) -> str:
        self.median = statistics.median(self.samples) if self.samples else None
        if (len(self.samples) >= self.cfg.min_samples
                and self.median is not None
                and self.cfg.cam_h_lo_m <= self.median <= self.cfg.cam_h_hi_m):
            self.state = self.OK
            return self.OK
        if self.views >= self.cfg.max_views:
            self.state = self.FAIL
            return self.FAIL
        self.views += 1
        self.state = "settle"
        self.settle_until = now + self.cfg.settle_s
        return self.SWITCH
