# -*- coding: utf-8 -*-
"""逐帧核对页：把「真值 / 现役 / 新版」三条判定画在同一张原始分辨率图上。

为什么要有它（2026-11-12 维护者质疑统计判据）：
`probe_segment_stability.py` 的 TP/FP 是两套代理判定的交叉表，两套都有主观阈值
（亮度真值的形状闸、目标带的硬编码 x 范围）。统计数字再漂亮也不能替代人眼。
本脚本只做一件事：把每一帧的判定依据摊开给人看，不做任何结论。

每帧一图，四层信息：
- 黄色细线 = 亮度真值（`make_target_labels.extract` 的掩码中心线）
- 蓝线 = 现役模型经生产口径提取出的线
- 红线 = 待评模型经生产口径提取出的线
- 洋红框 = 目标带（x 200~340, y 265~360）
左上角文字给出该帧的三项判定，便于和 CSV 对账。

用法：python probe_segment_review.py [权重文件名] [起帧] [止帧]
产物：first_look_out/segment/review/{frames/*.jpg,index.html,review.csv}
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "fine_tune"))

import probe_first_look as P          # noqa: E402
import make_target_labels as mtl      # noqa: E402

OLD = (HERE.parents[2] / "maaracing_master/plugins/speedrush/resources/models/lane"
       / "plus_small_384x640.onnx")
NEW = HERE / "weights" / (sys.argv[1] if len(sys.argv) > 1
                          else "plus_small_paperloss_thin_384x640.onnx")
F0 = int(sys.argv[2]) if len(sys.argv) > 2 else 180
F1 = int(sys.argv[3]) if len(sys.argv) > 3 else 260
RAW = (Path.home() / "AppData/Roaming/MaaRacingMaster/debug/speedrush"
       / "20261109_104823_manual_p2" / "raw")
OUT = HERE / "first_look_out" / "segment" / "review"
(OUT / "frames").mkdir(parents=True, exist_ok=True)

BAND_X = (200, 340)
BAND_Y = (265, 360)
PROBE_Y = 310


def line_x_at(line, y_content):
    return (line["s"] * (y_content * 2) + line["a"]) / 2.0


def target_hit(lines):
    for c in lines:
        # 不要求 band=="near"：生产的 accepted 判定只看 rms/theta，近段拟合失败
        # 时用远段（f0239 就是 33 点、rms 0.8、x=309 的合格线，却被 band 卡掉）。
        # 只要求线的 y 范围覆盖读点所在行。
        if not (c["y0"] <= PROBE_Y * 2 <= c["y1"]):
            continue
        x = line_x_at(c, PROBE_Y)
        if BAND_X[0] - 6 <= x <= BAND_X[1] + 6:
            return True, round(float(x), 1)
    return False, None


def draw_lines(im, lines, color, thick=2):
    for c in lines:
        ys = np.arange(max(int(c["y0"]) // 2, 206), min(int(c["y1"]) // 2, 359), 2)
        if len(ys) < 2:
            continue
        xs = (c["s"] * (ys * 2) + c["a"]) / 2.0
        pts = np.stack([xs * 2, ys * 2], 1).astype(np.int32)
        cv2.polylines(im, [pts], False, color, thick)


def main():
    so = ort.InferenceSession(str(OLD), providers=["CPUExecutionProvider"])
    sn = ort.InferenceSession(str(NEW), providers=["CPUExecutionProvider"])
    rows = []
    for fid in range(F0, F1 + 1):
        fp = RAW / f"{fid:04d}_raw.jpg"
        if not fp.exists():
            continue
        bgr = cv2.imread(str(fp))
        if bgr is None:
            continue
        content = cv2.resize(bgr, (640, 360), interpolation=cv2.INTER_AREA)
        mask, info = mtl.extract(content)
        gt = info is not None

        p_old = P.lane_prob(so, bgr, padded=True)
        l_old = P.extract_lines(bgr, p_old)
        p_new = P.lane_prob(sn, bgr, padded=True)
        l_new = P.extract_lines(bgr, p_new)
        hit_old, x_old = target_hit(l_old)
        hit_new, x_new = target_hit(l_new)

        vis = bgr.copy()
        # 真值：黄色
        if mask is not None:
            big = cv2.resize(mask, (bgr.shape[1], bgr.shape[0]),
                             interpolation=cv2.INTER_NEAREST) > 127
            vis[big] = (0.25 * vis[big] + 0.75 * np.array([0, 255, 255],
                                                          np.float32)).astype(np.uint8)
        # 目标带：洋红
        cv2.rectangle(vis, (BAND_X[0] * 2, BAND_Y[0] * 2),
                      (BAND_X[1] * 2, BAND_Y[1] * 2 - 1), (255, 0, 255), 2)
        draw_lines(vis, l_old, (255, 120, 0), 2)      # 现役：蓝
        draw_lines(vis, l_new, (0, 0, 255), 3)        # 新版：红

        tag = f"f{fid:04d}"
        cv2.putText(vis, f"{tag}  GT={'Y' if gt else 'n'}  native={'Y' if hit_old else 'n'}"
                         f"({x_old})  new={'Y' if hit_new else 'n'}({x_new})",
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (255, 255, 255), 2)
        cv2.putText(vis, "yellow=GT  blue=native  red=new  magenta=band",
                    (10, 58), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (220, 220, 220), 1)
        cv2.imwrite(str(OUT / "frames" / f"{tag}.jpg"), vis,
                    [cv2.IMWRITE_JPEG_QUALITY, 88])
        rows.append(dict(fid=fid, gt=int(gt), gt_x=None if not gt else
                         round(float(np.mean(np.nonzero(mask)[1])), 1),
                         old_hit=int(hit_old), old_x=x_old,
                         new_hit=int(hit_new), new_x=x_new))

    with (OUT / "review.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    items = "\n".join(
        f'<div class="f" data-i="{i}"><img src="frames/f{r["fid"]:04d}.jpg">'
        f'<div class="cap">f{r["fid"]:04d} · GT {"有" if r["gt"] else "无"} · '
        f'现役 {"检出" if r["old_hit"] else "无"} · 新版 {"检出" if r["new_hit"] else "无"}'
        f'</div></div>' for i, r in enumerate(rows))
    html = """<!doctype html><meta charset="utf-8">
<title>逐帧核对 __RANGE__</title>
<style>
body{margin:0;background:#111;color:#eee;font:14px/1.5 system-ui,sans-serif}
header{position:sticky;top:0;background:#1b1b1b;padding:10px 14px;border-bottom:1px solid #333}
header b{color:#7cf} .k{color:#888}
main{display:grid;gap:14px;padding:14px}
.f{display:none} .f.on{display:block}
img{width:100%;max-width:1280px;display:block;border:1px solid #333;border-radius:6px}
.cap{padding:6px 2px;color:#bbb}
</style>
<header><b id="pos">1 / __N__</b>
<span class="k">← → 翻页 · 黄色=亮度真值 · 蓝线=现役 · 红线=新版 · 洋红框=目标带</span></header>
<main>__ITEMS__</main>
<script>
const fs=[...document.querySelectorAll('.f')];let i=0;
function show(){fs.forEach((f,j)=>f.classList.toggle('on',j===i));
document.getElementById('pos').textContent=(i+1)+' / '+fs.length;}
addEventListener('keydown',e=>{
 if(e.key==='ArrowRight'||e.key==='d'){i=Math.min(i+1,fs.length-1);show();}
 if(e.key==='ArrowLeft'||e.key==='a'){i=Math.max(i-1,0);show();}
});
show();
</script>"""
    html = (html.replace("__ITEMS__", items)
                .replace("__N__", str(len(rows)))
                .replace("__RANGE__", f"f{F0}-f{F1}"))
    (OUT / "index.html").write_text(html, encoding="utf-8")
    print(f"帧 {len(rows)}；产物 {OUT/'index.html'}、review.csv、frames/")


if __name__ == "__main__":
    main()
