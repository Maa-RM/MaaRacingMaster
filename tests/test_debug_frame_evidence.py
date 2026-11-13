# -*- coding: utf-8 -*-
"""证据帧契约：落盘图**永不被绘制**，标注只以数据形态随帧落盘。

背景（2026-11-12）：从前 PEEP 预览、Studio 回放、模板标定三处共用同一张落盘图，
而这张图被状态文字和 ROI 框画过。于是「从回放里裁一张模板」等于「从带字幕的图上
裁模板」——公告页左上角返回按钮被状态行压住，同一模板从 0.95 掉到 0.74，好图被
判成废图。修法不是加个「裁剪时避开文字」的补丁，而是把像素与标注彻底分开：

- 落盘帧 = 原始像素（本文件锁死这条）
- 标注   = `frames.jsonl` 每行的 `overlay` 字段（归一化坐标 + 文案）
- 视图   = 标注画在副本上的瞬时图，只喂 PEEP（不落盘）
"""
from __future__ import annotations

import json

import numpy as np

from maaracing_master.core.debug import NavigationDebugger


def _frame(h: int = 240, w: int = 426) -> np.ndarray:
    """带纹理的帧：纯色会被 JPEG 压得几乎无损，掩盖不了绘制污染。"""
    y, x = np.mgrid[0:h, 0:w]
    rgb = np.stack([(x * 255 // max(w - 1, 1)),
                    (y * 255 // max(h - 1, 1)),
                    ((x + y) * 3 % 256)], axis=-1).astype(np.uint8)
    # 左上角一块高对比「控件」，正是模板裁剪会碰到的位置
    rgb[10:40, 10:60] = (250, 250, 250)
    return rgb


def _hub(tmp_path) -> NavigationDebugger:
    d = NavigationDebugger(tmp_path)
    d.enabled = True
    d.peep_enabled = True
    d.begin_run("run_evidence")
    return d


def _state() -> dict:
    return {
        "nav": {
            "node": "startup.boot", "phase": "template",
            "template": "hall_icon.png", "score": 0.93,
            "box": [40, 30, 120, 90],     # 像素坐标，落在 426×240 帧内
        },
        "intent": {"point": [210, 150], "label": "点娱乐玩法", "status": "pending"},
    }


def _saved_frames(tmp_path) -> list[dict]:
    rows: list[dict] = []
    for path in tmp_path.rglob("frames.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _offer_one(tmp_path, frame, state: dict | None = None):
    """跑一帧直到排空，返回 (落盘图, 帧索引行)。"""
    import cv2

    d = _hub(tmp_path)
    try:
        seg = d.open_segment("nav", "进入大厅", peep_fps=1000, save_fps=1000)
        if state is not None:
            seg.publish(state)
        seg.offer(frame, fid=1)
        assert d._wait_drain(5.0), "调试流未在期限内排空"
        d.close_segment(seg, "reached")
        assert d._wait_drain(5.0)
        rows = _saved_frames(tmp_path)
        assert len(rows) == 1, f"应恰好落盘一帧，实际 {len(rows)}"
        path = next(tmp_path.rglob(f"{int(rows[0]['seq']):06d}.jpg"))
        return cv2.imread(str(path)), rows[0]
    finally:
        d.end_run()


class TestEvidenceFrameIsClean:
    def test_saved_pixels_match_source(self, tmp_path):
        """落盘图必须与采集帧一致（只允许 JPEG 压缩误差），不得有字幕/框。"""
        import cv2

        rgb = _frame()
        saved, _ = _offer_one(tmp_path, rgb, _state())
        assert saved is not None
        # 采集给的是 RGB，落盘与 cv2.imread 都是 BGR：先对齐再比
        src_bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        assert saved.shape == src_bgr.shape
        diff = np.abs(saved.astype(np.int16) - src_bgr.astype(np.int16))
        assert diff.mean() < 6.0, f"落盘图被改写过，平均像素差 {diff.mean():.2f}"

    def test_bottom_band_left_untouched(self, tmp_path):
        """底部黑带只在视图帧上出现；证据帧的最后一行仍是游戏画面。"""
        src = _frame()
        saved, _ = _offer_one(tmp_path, src, _state())
        assert saved is not None
        src_row = src[-1].astype(np.int16).mean()
        out_row = saved[-1].astype(np.int16).mean()
        assert abs(out_row - src_row) < 24, "证据帧底部被黑带覆盖"

    def test_overlay_is_data_not_pixels(self, tmp_path):
        """标注写在帧索引里：归一化坐标 + 文案槽位。"""
        _, row = _offer_one(tmp_path, _frame(), _state())
        overlay = row.get("overlay")
        assert isinstance(overlay, dict), "帧索引缺 overlay 字段"
        box = overlay["nav_box"]
        assert len(box) == 4
        assert all(0.0 <= v <= 1.0 for v in box), f"overlay 坐标应为归一化值: {box}"
        slots = {x["slot"]: x["text"] for x in overlay["lines"]}
        assert "startup.boot" in slots["nav"]
        assert "0.930" in slots["nav"]
        assert overlay["intent"]["text"].startswith("点娱乐玩法")


class TestViewFrameIsDrawn:
    def test_overlay_renderer_changes_copy_only(self, tmp_path):
        """视图渲染只改副本：能画出框与黑带，传进来的原帧一个像素都不动。"""
        d = NavigationDebugger(tmp_path)
        frame = _frame()
        pristine = frame.copy()
        payload = {"size": [426, 240], "lines": [
            {"slot": "nav", "text": "startup.boot · template"},
            {"slot": "head", "text": "nav · #1 · 0ms"}],
            "nav_box": [0.1, 0.1, 0.4, 0.4],
            "intent": {"at": [0.5, 0.5], "text": "点击"}}
        view = d._render_overlay(frame.copy(), payload)
        assert np.array_equal(frame, pristine), "渲染不得改动传入帧"
        assert not np.array_equal(view, pristine), "视图帧应当有绘制内容"
        # 底部黑带：中间偏暗（带上还压着文字，取中位数看底色）
        assert np.median(view[-12:]) < 40, "视图帧底部应是黑带"
        # 框外远处未被触碰（避开底部黑带）
        assert np.array_equal(view[120:150, 300:400], pristine[120:150, 300:400])

    def test_payload_without_nav_state_has_no_box(self, tmp_path):
        """无导航状态时只留 head 行，不留空框：空的 nav_box 会让回放画出幽灵矩形。"""
        _, row = _offer_one(tmp_path, _frame(), None)
        overlay = row["overlay"]
        assert "nav_box" not in overlay
        assert "intent" not in overlay
        assert all(x["slot"] == "head" for x in overlay["lines"])

    def test_invalid_shape_yields_empty_payload(self):
        """解释器拿不到尺寸时返回空 dict，不猜坐标。"""
        d = NavigationDebugger.__new__(NavigationDebugger)
        assert d._overlay_payload((0, 0), None) == {}
        assert d._overlay_payload(None, None) == {}
