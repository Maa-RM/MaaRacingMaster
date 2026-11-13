"""整段真实对局复核：新版权重在生产提取链下会不会多出可见假线。

背景（2026-11-12 维护者裁定）：81 帧的连帧验证显示 verified-74 召回 83%、误检 33%。
但「误检率」是逐帧二分类指标，回答不了真正的问题——**连续驾驶时这些误检会不会变成
眼前能看到的假线**。一个只在单帧闪一下的假线，和一个连续 30 帧稳定存在的假线，
对驾驶体验的影响完全不同。所以本探针在整段会话上逐帧跑两版权重，用生产同口径提取，
统计：

  1. 每帧「新增线」（新版有、现役没有的已接受线）数量与位置；
  2. 新增线的**持续性**（在相邻帧里能否连续追踪到），持续越长越像真线；
  3. 「丢失线」（现役有、新版没有）数量——回归破坏的直接证据。

判据与生产一致：无颜色门、行扫描步长 1、rms<=3.0、theta<=0.35。
注意：这里不判断「新增线是真线还是假线」（那需要人眼），只给出**可疑帧清单 + 原图**，
由人看。探针负责把「该看的帧」压到几十张，而不是替人下结论。

用法：
    python probe_full_session.py <权重名> [会话名 ...] [--step N] [--limit N]
例：
    python probe_full_session.py plus_small_verified74_384x640.onnx --step 2
"""

from __future__ import annotations

import csv
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "fine_tune"))
import probe_first_look as P  # noqa: E402
import make_target_labels as mtl  # noqa: E402

OLD = (HERE.parents[2] / "maaracing_master/plugins/speedrush/resources/models/lane"
       / "plus_small_384x640.onnx")
RAW_ROOT = Path.home() / "AppData/Roaming/MaaRacingMaster/debug/speedrush"
OUT = HERE / "first_look_out" / "full_session"

# 默认跑「训练完全没见过的会话」：133512_p1(120) 与 222251_p1(271)。
# 104823_p2 是验证段所在会话，单独用 --session 指定。
DEFAULT_SESSIONS = [
    "20261109_104823_manual_p2",   # 黄昏，贴脸构型所在会话（180~260 为验证段）
    "20261109_095756_manual_p2",   # 白天城市
]

PROBE_Y = 310          # 内容域读点行
MATCH_X = 18.0         # 两条线在 PROBE_Y 处 x 差 < 该值视为同一条线
TRACK_X = 25.0         # 跨帧追踪同一新增线的 x 容差


def lines_of(sess, bgr):
    prob = P.lane_prob(sess, bgr, padded=True)
    return P.extract_lines(bgr, prob), prob


def x_at(line, y=PROBE_Y):
    return (line["s"] * (y * 2) + line["a"]) / 2.0


def covers(line, y=PROBE_Y):
    return line["y0"] <= y * 2 <= line["y1"]


def compare(old, new):
    """返回 (extras, lost)：在 PROBE_Y 处无对应线的已接受线。"""
    used = set()
    extras = []
    for n in new:
        if not covers(n):
            continue
        nx = x_at(n)
        hit = None
        for i, o in enumerate(old):
            if i in used or not covers(o):
                continue
            if abs(x_at(o) - nx) < MATCH_X:
                hit = i
                break
        if hit is None:
            extras.append(round(float(nx), 1))
        else:
            used.add(hit)
    lost = []
    for i, o in enumerate(old):
        if i in used or not covers(o):
            continue
        lost.append(round(float(x_at(o)), 1))
    return sorted(extras), sorted(lost)


def zone_of(x):
    if 200 <= x <= 340:
        return "目标带"
    if x < 200:
        return "左(黄线/路缘)"
    return "右(白虚线/路肩)"


def draw(im, lines, color, thick=2, only=None):
    for k, c in enumerate(lines):
        if only is not None and k not in only:
            continue
        ys = np.arange(max(int(c["y0"]) // 2, 206), min(int(c["y1"]) // 2, 359), 2)
        if len(ys) < 2:
            continue
        xs = (c["s"] * (ys * 2) + c["a"]) / 2.0
        pts = np.stack([xs * 2, ys * 2], 1).astype(np.int32)
        cv2.polylines(im, [pts], False, color, thick)


def main():
    args = [a for a in sys.argv[1:]]
    weight = None
    step = 1
    limit = 0
    sessions = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--step":
            step = int(args[i + 1]); i += 2; continue
        if a == "--limit":
            limit = int(args[i + 1]); i += 2; continue
        if a == "--session":
            sessions.append(args[i + 1]); i += 2; continue
        if weight is None:
            weight = a
        else:
            sessions.append(a)
        i += 1
    weight = weight or "plus_small_verified74_384x640.onnx"
    new_path = HERE / "weights" / weight
    if not new_path.exists():
        raise SystemExit(f"权重不存在: {new_path}")
    sessions = sessions or DEFAULT_SESSIONS

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "frames").mkdir(exist_ok=True)

    so = ort.InferenceSession(str(OLD), providers=["CPUExecutionProvider"])
    sn = ort.InferenceSession(str(new_path), providers=["CPUExecutionProvider"])

    rows = []
    t0 = time.perf_counter()
    for sess_name in sessions:
        d = RAW_ROOT / sess_name / "raw"
        if not d.exists():
            print(f"[跳过] {sess_name} 无 raw 目录")
            continue
        files = sorted(d.glob("*.jpg"))[::step]
        if limit:
            files = files[:limit]
        print(f"[{sess_name}] {len(files)} 帧（step={step}）")
        for k, fp in enumerate(files):
            bgr = cv2.imread(str(fp))
            if bgr is None:
                continue
            lo, _ = lines_of(so, bgr)
            ln, prob = lines_of(sn, bgr)
            ex, lost = compare(lo, ln)
            # 亮度真值（不碰模型）：这一帧画面里到底有没有贴脸构型那条线
            content = cv2.resize(bgr, (640, 360), interpolation=cv2.INTER_AREA)
            gm, ginfo = mtl.extract(content)
            gt_x = (ginfo["x"] + ginfo["w"] / 2) if ginfo else None
            # 有效帧：现役看到路（>=2 条线）或真值确实有目标线；
            # 发车倒计时、隧道转场、车尾特写这类画面两版都看不到路，不计入误检分母。
            valid = (len(lo) >= 2) or (gm is not None)
            hit_new = any(covers(c) and abs(x_at(c) - (gt_x or -999)) < 20 for c in ln) if gt_x else False
            rows.append(dict(
                session=sess_name, fid=int(fp.stem.split("_")[0]),
                n_old=len(lo), n_new=len(ln),
                extras=len(ex), lost=len(lost),
                ex_x=" ".join(f"{v:.0f}" for v in ex),
                lost_x=" ".join(f"{v:.0f}" for v in lost),
                ex_zone=" ".join(sorted({zone_of(v) for v in ex})),
                gt=1 if gm is not None else 0,
                gt_x="" if gt_x is None else round(float(gt_x), 1),
                gt_hit=1 if hit_new else 0,
                valid=1 if valid else 0,
                _ex=ex, _lost=lost, _path=str(fp),
            ))
            if (k + 1) % 50 == 0:
                print(f"   {k+1}/{len(files)}  ({time.perf_counter()-t0:.0f}s)")

    if not rows:
        raise SystemExit("没有可用帧")

    # ---- 持续性：把相邻帧里 x 相近的新增线连成轨迹 ----
    for sess_name in {r["session"] for r in rows}:
        seq = [r for r in rows if r["session"] == sess_name]
        seq.sort(key=lambda r: r["fid"])
        open_tracks = []   # [last_fid, last_x, length]
        for r in seq:
            still = []
            for tr in open_tracks:
                if r["fid"] - tr[0] <= 3 * step:
                    still.append(tr)
            open_tracks = still
            for x in r["_ex"]:
                best = None
                for tr in open_tracks:
                    if abs(tr[1] - x) < TRACK_X:
                        best = tr
                        break
                if best is None:
                    open_tracks.append([r["fid"], x, 1])
                else:
                    best[0], best[1], best[2] = r["fid"], x, best[2] + 1
            r["max_run"] = max([tr[2] for tr in open_tracks], default=0)

    total = len(rows)
    valid = [r for r in rows if r["valid"]]
    with_ex = [r for r in valid if r["extras"] > 0]
    lost_rows = [r for r in valid if r["lost"] > 0]
    persistent = [r for r in valid if r.get("max_run", 0) >= 3]
    gt_rows = [r for r in rows if r["gt"] == 1]

    print("\n=== 汇总 ===")
    print(f"总帧 {total}，有效驾驶帧 {len(valid)}（现役看到路或真值有目标线）")
    print(f"有效帧里有新增线 {len(with_ex)} "
          f"({100*len(with_ex)/max(1,len(valid)):.1f}%)  "
          f"有丢失线 {len(lost_rows)} ({100*len(lost_rows)/max(1,len(valid)):.1f}%)")
    print(f"新增线持续 >=3 帧的帧 {len(persistent)} "
          f"({100*len(persistent)/max(1,len(valid)):.1f}%)")
    print(f"平均每帧新增 {np.mean([r['extras'] for r in valid]):.2f} 条，"
          f"现役 {np.mean([r['n_old'] for r in valid]):.2f} 条，"
          f"新版 {np.mean([r['n_new'] for r in valid]):.2f} 条")
    print(f"\n亮度真值判定有贴脸线 {len(gt_rows)} 帧，"
          f"新版检出 {sum(r['gt_hit'] for r in gt_rows)} 帧"
          f"（召回 {100*sum(r['gt_hit'] for r in gt_rows)/max(1,len(gt_rows)):.0f}%）")

    from collections import Counter
    zc = Counter()
    for r in with_ex:
        for z in r["ex_zone"].split():
            zc[z] += 1
    print("新增线所在区域分布（帧次）:", dict(zc))

    # ---- 落盘 CSV ----
    fields = ["session", "fid", "valid", "gt", "gt_x", "gt_hit",
              "n_old", "n_new", "extras", "lost",
              "ex_x", "lost_x", "ex_zone", "max_run"]
    with (OUT / "full_session.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # ---- 可疑帧出图：新增最多的 24 帧 ----
    cand = [r for r in valid if r["extras"] > 0] or valid
    picks = sorted(cand, key=lambda r: (-r["extras"], -r.get("max_run", 0)))[:24]
    tiles = []
    for r in picks:
        bgr = cv2.imread(r["_path"])
        lo, _ = lines_of(so, bgr)
        ln, prob = lines_of(sn, bgr)
        vis = bgr.copy()
        # 概率响应淡淡的绿，便于判断「模型到底看到了什么」
        heat = cv2.resize((prob > 0.3).astype(np.uint8) * 255,
                          (bgr.shape[1], bgr.shape[0]),
                          interpolation=cv2.INTER_NEAREST) > 0
        vis[heat] = (0.7 * vis[heat] + 0.3 * np.array([0, 255, 0], np.float32)).astype(np.uint8)
        draw(vis, lo, (255, 120, 0), 3)          # 现役：蓝
        draw(vis, ln, (0, 0, 255), 3)            # 新版：红
        # 目标带
        cv2.rectangle(vis, (200 * 2, 265 * 2), (340 * 2, 719), (255, 0, 255), 2)
        cv2.putText(vis, f"{r['session'][9:13]}{r['session'][-2:]}_{r['fid']:04d} "
                         f"new+{r['extras']} lost{r['lost']} run{r.get('max_run',0)}",
                    (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.85, (255, 255, 255), 2)
        tiles.append(cv2.resize(vis, (640, 360)))
    while len(tiles) % 3:
        tiles.append(np.zeros_like(tiles[0]))
    sheet = np.vstack([np.hstack(tiles[i:i + 3]) for i in range(0, len(tiles), 3)])
    cv2.imwrite(str(OUT / "suspect_sheet.jpg"), sheet, [cv2.IMWRITE_JPEG_QUALITY, 90])
    print(f"\n可疑帧拼图: {OUT/'suspect_sheet.jpg'}")
    print(f"逐帧表:     {OUT/'full_session.csv'}")


if __name__ == "__main__":
    main()
