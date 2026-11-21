# -*- coding: utf-8 -*-
"""开局视角守卫：用**车道线反解的相机高**判断当前是否是保险杠机位。

只做状态机，不碰手柄与帧：模块层负责喂样本、按 RB；本模块可离线单测。
判据来自 `lane_obs._cam_h_from_lines`（两条不同道号的线在同一行的像素间距
反解 `cam_h`），**不读深度路面平面的 c**——v5 合成点图把贴地像素的高度钉在
Y≡cam_h，平面 c 恒等于 1.25，读它等于读常数（2026-11-12 实机 18 帧逐帧核对）。
保险杠视角相机高度中位约 1.25m（权威口径 2026-11-11）；现役只用保险杠视角：
游戏开局会把机位打回追尾，按 RB 切换的唯一目的就是切回保险杠（追尾视角
历史读数约 1.6–2.0 m，线几何在撞车段实测段中位 1.605）。

第一阶段允许从默认追尾机位请求一次切换。切换后和第二阶段只采证确认当前
机位；守卫到期仍无法确认时，驾驶层以直行完成当前阶段。
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
    min_samples: int = 3
    deadline_s: float = 10.0     # 从首个有效样本起算，限制机位确认的总耗时
    arm_timeout_s: float = 8.0   # 首个有效样本的最长等待（加载/冷启动不计入视角预算）
    empty_sample_s: float | None = None  # 已有检测但缺少保险杠高度证据时的开局等待

    def __post_init__(self) -> None:
        if not 0.0 < self.cam_h_lo_m < self.cam_h_hi_m:
            raise ValueError(f"view_guard 相机高度带非法：{self.cam_h_lo_m}"
                             f"..{self.cam_h_hi_m}")
        if self.sample_s <= 0 or self.settle_s < 0:
            raise ValueError(f"view_guard 采样窗非法：{self.sample_s}/"
                             f"{self.settle_s}")
        if self.min_samples < 1:
            raise ValueError(f"view_guard 样本数非法：{self.min_samples}")
        if self.deadline_s <= self.sample_s:
            raise ValueError(f"view_guard 截止 {self.deadline_s}s 必须大于"
                             f"采样窗 {self.sample_s}s")
        if self.arm_timeout_s <= 0:
            raise ValueError(f"view_guard 首个样本等待非法：{self.arm_timeout_s}")
        if self.empty_sample_s is not None and self.empty_sample_s <= 0:
            raise ValueError(f"view_guard 空样本窗非法：{self.empty_sample_s}")


class ViewGuard:
    """一个阶段的视角确认状态机（成功/失败即终态，样本不跨视角累积）。"""

    WAIT = "wait"
    SWITCH = "switch"
    OK = "ok"
    FAIL = "fail"
    GRACE_S = 0.4   # 开局切换前给线几何一次短暂宽限

    def __init__(self, cfg: ViewGuardConfig, t0: float, *, allow_switch: bool = True) -> None:
        self.cfg = cfg
        self.t0 = float(t0)
        self.state = "collect"
        self.armed_at: float | None = None
        self.window_start: float | None = None
        self.settle_until = 0.0
        self.samples: list[float] = []
        self._can_switch = allow_switch
        self.median: float | None = None
        self.first_sample_at: float | None = None

    def update(self, now: float, cam_h: float | None,
               new: bool) -> str:
        """喂一拍；``new`` 表示收到新检测帧，即使该帧没有解出高度。"""
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
                and now - self.window_start >= self.cfg.sample_s
                and len(self.samples) >= self.cfg.min_samples):
            self.median = statistics.median(self.samples)
            if self.cfg.cam_h_lo_m <= self.median <= self.cfg.cam_h_hi_m:
                self.state = self.OK
                return self.OK
            if self._can_switch and self.median > self.cfg.cam_h_hi_m:
                return self._switch(now)
        # 只有第一阶段还没切过时，才可以按默认追尾机位尝试一次。孤立的
        # 追尾高度也不能封死这条路，但从首次读到它起仍留足一个采样窗。
        empty_sample_s = self.cfg.empty_sample_s or self.cfg.sample_s
        if (self._can_switch and self.armed_at is not None
                and now - self.armed_at >= empty_sample_s + self.GRACE_S
                and (self.window_start is None or now - self.window_start >= self.cfg.sample_s)
                and all(height > self.cfg.cam_h_hi_m for height in self.samples)):
            self.median = statistics.median(self.samples) if self.samples else None
            return self._switch(now)
        return self.WAIT

    def _switch(self, now: float) -> str:
        self._can_switch = False
        self.state = "settle"
        self.settle_until = now + self.cfg.settle_s
        return self.SWITCH
