"""横向震荡诊断（speedrush，离线）。

**问什么**：直道巡航时方向盘来回摆，是「测量在抖」还是「控制律在追」？

口径：

- 取 `route.ph == "keep"` 且目标道 `x_target` 在窗口内不变的段（排除并线引起的位移）。
- **高频幅度**：信号减去 0.5 秒滑动均值后的标准差（道）。
- 同时看三条线：`executed_lane`（执行横位估计）、`-ro_off`（路中心读数反号＝实测自车
  横位）、`steer_norm`（杆值）。
- **换向率**：高频分量每分钟过零次数，衡量摆动快慢。
- 线段太短（<0.8 秒）不计。

判读：若 `-ro_off` 的高频幅度与 `executed_lane` 同量级，说明摆动来自测量；若只有
`executed_lane` 与杆在摆而 `-ro_off` 平滑，则是控制律在追自己的状态。

用法::

    .venv/Scripts/python.exe -X utf8 probe_oscillation.py <会话 ...>
"""

from __future__ import annotations

import json
import os
import statistics as st
import sys

ROOT = os.path.expandvars(r"%APPDATA%/MaaRacingMaster/logs")
WIN_S, MIN_SEG_S = 0.5, 0.8


def load(sess: str):
    p = os.path.join(ROOT, sess, "trace", "speedrush.jsonl")
    ticks = [json.loads(l) for l in open(p, encoding="utf-8")
             if l.startswith("{") and '"fid"' in l]
    ticks.sort(key=lambda r: r["ts_ns"])
    return ticks


def _hp(seq):
    """减 0.5 秒滑动均值后的残差。"""
    out = []
    ts = [t for t, _ in seq]
    k = 0
    for i, (t, v) in enumerate(seq):
        while ts[k] < t - WIN_S * 1e9:
            k += 1
        win = [x for _, x in seq[k:i + 1]]
        out.append(v - sum(win) / len(win))
    return out


def analyze(sess: str) -> None:
    ticks = load(sess)
    segs, cur = [], []
    for r in ticks:
        rt = r.get("route") or {}
        x = r.get("x_target")
        if rt.get("ph") == "keep" and x is not None:
            if cur and abs(x - cur[-1][1]) < 1e-3:
                cur.append((r["ts_ns"], x, r))
            else:
                if len(cur) > 1:
                    segs.append(cur)
                cur = [(r["ts_ns"], x, r)]
        else:
            if len(cur) > 1:
                segs.append(cur)
            cur = []
    if len(cur) > 1:
        segs.append(cur)

    ex_amp, ro_amp, st_amp, cross = [], [], [], []
    for seg in segs:
        dur = (seg[-1][0] - seg[0][0]) / 1e9
        if dur < MIN_SEG_S:
            continue
        ex = [(t, r.get("executed_lane") or 0.0) for t, _, r in seg]
        ro = [(t, -(r.get("ro_off") or 0.0)) for t, _, r in seg]
        stk = [(t, r.get("steer_norm") or 0.0) for t, _, r in seg]
        eh, rh, sh = _hp(ex), _hp(ro), _hp(stk)
        ex_amp.append(st.pstdev(eh))
        ro_amp.append(st.pstdev(rh))
        st_amp.append(st.pstdev(sh))
        zc = sum(1 for a, b in zip(sh, sh[1:]) if a * b < 0)
        cross.append(zc / dur * 60)
    if not ex_amp:
        print(f"== {sess}: 没有可用巡航段")
        return
    print(f"== {sess}  巡航段 {len(ex_amp)}  合计 "
          f"{sum((s[-1][0]-s[0][0])/1e9 for s in segs if (s[-1][0]-s[0][0])/1e9 >= MIN_SEG_S):.0f}s")
    print(f"   高频幅度（中位） 执行横位 {st.median(ex_amp):.3f} 道"
          f"  路中心读数 {st.median(ro_amp):.3f} 道"
          f"  杆值 {st.median(st_amp):.3f}")
    print(f"   杆值换向率 中位 {st.median(cross):.0f} 次/分钟"
          f"  p90 {sorted(cross)[int(len(cross)*0.9)]:.0f}")


if __name__ == "__main__":
    for s in sys.argv[1:]:
        analyze(s)
