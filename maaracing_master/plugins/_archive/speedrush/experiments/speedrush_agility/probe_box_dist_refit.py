"""框高粗距重标定（speedrush，离线）。

**问什么**：`z = K / 框高` 里的 K 是在旧焦距（630）下标定的，焦距改成 470 后
`K` 没有跟着改，粗距系统性偏大。用**当前尺子**的会话重新拟合 K。

口径：

- 只取同拍同时有「框高」与「锚点米制纵深」的车（`otab` 的 `src=metric`）。
- 纵深限制 8~40 米（锚点在近段最可信；40 米外锚点本身误差大）。
- 每个样本给出 `K = 框高 × 锚点纵深`，报分位数与按距离档的中位。
- 现役常数 `K=1285` 作为对照；理论上 K = 焦距 × 有效车高。

用法::

    .venv/Scripts/python.exe -X utf8 probe_box_dist_refit.py <会话 ...>
"""

from __future__ import annotations

import json
import os
import statistics as st
import sys

ROOT = os.path.expandvars(r"%APPDATA%/MaaRacingMaster/logs")
K_OLD = 1285.0
Z_LO, Z_HI = 8.0, 40.0
H_MIN = 18


def load(sess: str):
    p = os.path.join(ROOT, sess, "trace", "speedrush.jsonl")
    return [json.loads(l) for l in open(p, encoding="utf-8")
            if l.startswith("{") and '"fid"' in l]


def analyze(sess: str) -> None:
    ticks = load(sess)
    ks = []
    bands = {10: [], 20: [], 30: [], 40: []}
    ratios = []
    for r in ticks:
        hh = {d["id"]: d["h"] for d in (r.get("car_h") or [])
              if d.get("h") and d["h"] >= H_MIN}
        for row in ((r.get("otab") or {}).get("t") or []):
            if len(row) < 12 or row[0] != "car" or row[9] != "metric":
                continue
            z = row[4]
            h = hh.get(row[1])
            if z is None or h is None or not (Z_LO <= z <= Z_HI):
                continue
            ks.append(h * z)
            ratios.append((K_OLD / h) / z)
            for b in bands:
                if z <= b:
                    bands[b].append(h * z)
                    break
    if not ks:
        print(f"== {sess}: 无可用配对")
        return
    ks.sort()
    q = lambda f: ks[min(len(ks) - 1, int(len(ks) * f))]
    print(f"== {sess}  样本 {len(ks)}  焦距名义 470")
    print(f"   K 中位 {st.median(ks):.0f}  p10 {q(0.1):.0f}  p90 {q(0.9):.0f}"
          f"  跨度 {ks[0]:.0f}~{ks[-1]:.0f}")
    print(f"   隐含有效车高（K/470）中位 {st.median(ks)/470:.2f} m"
          f"  p10 {q(0.1)/470:.2f}  p90 {q(0.9)/470:.2f}")
    rr = sorted(ratios)
    print(f"   现役 K=1285 的粗距/锚距 比值 中位 {st.median(rr):.2f}"
          f"  p10 {rr[int(len(rr)*0.1)]:.2f}  p90 {rr[int(len(rr)*0.9)]:.2f}")
    for b, a in bands.items():
        if a:
            print(f"   {b-10:>2.0f}~{b:>2.0f} m 段：K 中位 {st.median(a):.0f}"
                  f"  n={len(a)}")


if __name__ == "__main__":
    for s in sys.argv[1:]:
        analyze(s)
