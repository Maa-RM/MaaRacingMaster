# -*- coding: utf-8 -*-
"""离线回放：地平线发布值旧/新平滑逻辑对比。

用 trace 的 ``y_v_raw`` 变化点重建“有效原始值”序列（值不变=没有新样本），
分别喂旧逻辑（断检清史后 prev=None 直接采纳第一帧）和新逻辑（清史后补窗
YV_WIN 拍、整窗持续偏离出口），比较发布值、对窗口中位的偏差与收敛时间。

输出 vanish_replay_out/replay_summary.json 与每局最差段的复现图。
"""

from __future__ import annotations

import json
import os
import sys
from collections import deque
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from maaracing_master.plugins.speedrush import lane_obs as la  # noqa: E402

APPDATA = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
LOGS = APPDATA / "MaaRacingMaster" / "logs"
OUT = Path(__file__).parent / "vanish_replay_out"
SESSIONS = ("20261113_203823", "20261113_212838", "20261113_224406")


def read_jsonl(path: Path):
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def replay(rows: list[dict]) -> tuple[list[dict], int]:
    """Replay the new smoother; the recorded old value is the old series.

    The trace freezes ``y_v_raw`` between valid samples, so the true last-sample
    time is not recoverable from the trace. The old direct-adoption jump
    (y_v == raw, previous y_v >10px away) is the observable signature of the
    gap-clear path; force the new logic's refill at that sample.
    """
    new_hist: deque = deque(maxlen=la.YV_WIN)
    new_prev = None
    new_t = 0.0
    new_refill = False
    last_raw = None
    last_pub = None
    prev_rec = None
    forced = 0
    out = []
    for r in rows:
        lo = r.get("lane_obs") or {}
        raw = lo.get("y_v_raw")
        pub = lo.get("y_v")
        now = r["ts_mono_ns"] / 1e9
        changed_raw = raw is not None and (
            last_raw is None or float(raw) != float(last_raw))
        changed_pub = pub is not None and (
            last_pub is None or float(pub) != float(last_pub))
        if raw is not None and (changed_raw or changed_pub):
            direct = (pub is not None and prev_rec is not None
                      and abs(float(pub) - float(raw)) <= 0.02
                      and abs(float(pub) - prev_rec) > 10.0)
            if direct:
                new_hist.clear()
                new_refill = True
                new_t = now
                forced += 1
            new_prev, new_t, new_refill = la._smooth_vanish(
                new_hist, float(raw), now, new_t, new_prev, new_refill)
            last_raw = float(raw)
        if pub is not None:
            last_pub = float(pub)
            prev_rec = float(pub)
        ref = float(np.median(new_hist)) if new_hist else None
        out.append({"fid": r.get("fid"), "t": now, "raw": raw,
                    "old": pub, "new": new_prev, "ref": ref, "rec": pub})
    return out, forced


def segments(replay_rows: list[dict]) -> list[list[dict]]:
    segs, cur = [], []
    for r in replay_rows:
        old = r["old"]
        if old is not None and float(old) > 370.0:
            if cur and r["t"] - cur[-1]["t"] > 2.0:
                segs.append(cur)
                cur = []
            cur.append(r)
        elif cur:
            segs.append(cur)
            cur = []
    if cur:
        segs.append(cur)
    return segs


def plot(rows: list[dict], path: Path, title: str):
    t0 = rows[0]["t"]
    ts = [r["t"] - t0 for r in rows]
    canvas = np.full((520, 1280, 3), 26, np.uint8)
    left, right, top, bottom = 70, 1240, 40, 440
    y_lo, y_hi = 330.0, 390.0

    def px(t):
        return int(left + (right - left) * t / max(ts[-1], 1e-6))

    def py(v):
        return int(bottom - (bottom - top) * ((v - y_lo) / (y_hi - y_lo)))

    for v in (340, 350, 360, 370, 380, 390):
        cv2.line(canvas, (left, py(v)), (right, py(v)), (55, 55, 55), 1)
        cv2.putText(canvas, str(v), (24, py(v) + 5), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, (180, 180, 180), 1, cv2.LINE_AA)
    for v in (la.FALLBACK_Y_V if hasattr(la, "FALLBACK_Y_V") else 361.0,):
        for x in range(left, right, 28):
            cv2.line(canvas, (x, py(v)), (min(right, x + 14), py(v)),
                     (150, 150, 150), 1)

    def series(key, color, thick):
        pts = [(px(t), py(r[key])) for t, r in zip(ts, rows)
               if r.get(key) is not None]
        for i in range(1, len(pts)):
            cv2.line(canvas, pts[i - 1], pts[i], color, thick, cv2.LINE_AA)

    series("raw", (0, 150, 255), 1)
    series("ref", (170, 170, 170), 2)
    series("old", (0, 255, 255), 2)
    series("new", (0, 220, 0), 2)
    legend = (("old", (0, 255, 255)), ("new", (0, 220, 0)),
              ("window median", (170, 170, 170)), ("raw", (0, 150, 255)))
    for i, (lab, col) in enumerate(legend):
        x = left + 10 + i * 190
        cv2.line(canvas, (x, 14), (x + 26, 14), col, 3)
        cv2.putText(canvas, lab, (x + 32, 19), cv2.FONT_HERSHEY_SIMPLEX,
                    0.52, (230, 230, 230), 1, cv2.LINE_AA)
    cv2.putText(canvas, title, (left, 40), cv2.FONT_HERSHEY_SIMPLEX,
                0.58, (235, 235, 235), 1, cv2.LINE_AA)
    cv2.putText(canvas, "time (s)", (left, 500), cv2.FONT_HERSHEY_SIMPLEX,
                0.5, (180, 180, 180), 1, cv2.LINE_AA)
    cv2.imwrite(str(path), canvas)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    summary = {}
    for name in SESSIONS:
        trace = LOGS / name / "trace" / "speedrush.jsonl"
        rows = [r for r in read_jsonl(trace)
                if r.get("fid") is not None and isinstance(r.get("lane_obs"), dict)]
        rows.sort(key=lambda r: r["fid"])
        rep, forced = replay(rows)
        segs = segments(rep)
        seg_summary = []
        worst = None
        worst_rows = None
        for seg in segs:
            old_max = max(r["old"] for r in seg if r["old"] is not None)
            new_max = max(r["new"] for r in seg if r["new"] is not None)
            ref_end = seg[-1]["ref"]
            old_bad = sum(1 for r in seg if r["ref"] is not None
                          and abs(r["old"] - r["ref"]) > 10.0)
            new_bad = sum(1 for r in seg if r["ref"] is not None
                          and abs(r["new"] - r["ref"]) > 10.0)
            seg_summary.append({"t0": round(seg[0]["t"], 2),
                                "t1": round(seg[-1]["t"], 2),
                                "dur_s": round(seg[-1]["t"] - seg[0]["t"], 2),
                                "old_max": round(old_max, 1),
                                "new_max": round(new_max, 1),
                                "ref_end": None if ref_end is None else round(ref_end, 1),
                                "old_gt10": old_bad, "new_gt10": new_bad})
            if worst is None or old_max - (ref_end or 0) > \
                    worst["old_max"] - (worst["ref_end"] or 0):
                worst = seg_summary[-1]
                worst_rows = seg
        changed5 = sum(1 for r in rep if r["old"] is not None
                       and r["new"] is not None
                       and abs(float(r["old"]) - float(r["new"])) > 5.0)
        changed10 = sum(1 for r in rep if r["old"] is not None
                        and r["new"] is not None
                        and abs(float(r["old"]) - float(r["new"])) > 10.0)
        summary[name] = {
            "rows": len(rep), "forced_gap": forced,
            "segments": seg_summary,
            "changed_gt5": changed5, "changed_gt10": changed10,
        }
        if worst_rows:
            plot(worst_rows, OUT / f"replay_{name}.png",
                 f"{name} old vs new (worst segment)")
        print(f"{name}: forced_gap {forced} segs {seg_summary} "
              f"changed>5 {changed5} >10 {changed10}")
    (OUT / "replay_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
