#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0 附加（点测）：维护者指认的两帧上，直接量「贴脸大虚线」位置的概率。

标尺=维护者箭头指认的人工 ROI（身份先于统计量）；框在 zoom 图上可视化。
对照：同帧内可见的黄线走廊（人工）。

跑法（仓库根）：
  .venv/Scripts/python.exe tools/experiments/speedrush_lane_model/probe_center_blindspot.py --point
输出：first_look_out/diagnosis/blindspot_point.md / blindspot_point.jpg
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

HERE = Path(__file__).resolve().parent
OUT = HERE / "first_look_out" / "diagnosis"
LETTERBOX_PAD = 12

# 人工 ROI（全幅 x0,x1,y0,y1；来源=维护者 2026-11-11 指认帧 + 箭头位置）
FRAMES = [
    ("20261109_104823_manual_p2", "0204", [
        ("中心大虚线(贴脸)", 830, 935, 530, 718),
        ("右前方虚线(远)", 1005, 1130, 425, 500),
    ]),
    ("20261109_104823_manual_p1", "0170", [
        ("金币右侧大虚线(贴脸)", 1010, 1200, 415, 585),
        ("左黄线走廊(对照)", 30, 480, 430, 718),
    ]),
]

MODELS = [
    ("plus_small(现役)", HERE.parents[2] / "maaracing_master/plugins/speedrush/resources/models/lane/plus_small_384x640.onnx", True),
    ("twinlitenet(原版)", HERE / "weights/twinlitenet_orig_360x640.onnx", False),
]


def lane_prob(sess, rgb, padded):
    inp = sess.get_inputs()[0].name
    outs = [o.name for o in sess.get_outputs()]
    ll_name = next((n for n in outs if "ll" in n.lower()), outs[-1])
    im = cv2.resize(rgb, (640, 360), interpolation=cv2.INTER_AREA)
    if padded:
        im = cv2.copyMakeBorder(im, LETTERBOX_PAD, LETTERBOX_PAD, 0, 0,
                                cv2.BORDER_CONSTANT, value=(114, 114, 114))
    x = np.ascontiguousarray((im.astype(np.float32) / 255.0).transpose(2, 0, 1)[None])
    logits = sess.run([ll_name], {inp: x})[0][0]
    e = np.exp(logits - logits.max(0, keepdims=True))
    prob = (e[1] / e.sum(0)).astype(np.float32)
    if prob.shape[0] == 360 + 2 * LETTERBOX_PAD:
        prob = prob[LETTERBOX_PAD:LETTERBOX_PAD + 360]
    return prob


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    summary = {}
    zooms = []
    for name, path, padded in MODELS:
        sess = ort.InferenceSession(str(path),
                                    providers=["CPUExecutionProvider"])
        for skey, fid, rois in FRAMES:
            fp = Path.home() / "AppData/Roaming/MaaRacingMaster/debug/speedrush" \
                / skey / "raw" / f"{fid}_raw.jpg"
            if not fp.is_file():
                cands = sorted((Path.home() / "AppData/Roaming/MaaRacingMaster"
                                / "debug/speedrush" / skey / "raw").glob(f"*{fid}*"))
                if not cands:
                    print(f"[跳过] {skey}/{fid} 找不到帧")
                    continue
                fp = cands[0]
            rgb = cv2.cvtColor(cv2.imread(str(fp)), cv2.COLOR_BGR2RGB)
            prob = lane_prob(sess, rgb, padded)
            rows = []
            for rname, x0, x1, y0, y1 in rois:
                sub = prob[y0 // 2:(y1 + 1) // 2, x0 // 2:(x1 + 1) // 2]
                xs = [float(v) for v in sub.ravel()]
                med = round(float(np.median(xs)), 3) if xs else None
                p90 = round(float(np.percentile(xs, 90)), 3) if xs else None
                mx = round(max(xs), 3) if xs else None
                rows.append({"roi": rname, "med": med, "p90": p90, "max": mx})
                print(f"[{skey}/{fid}] {name} {rname}: med={med} p90={p90} max={mx}")
            summary.setdefault(name, {})[f"{skey}/{fid}"] = rows
            # 可视化：ROI 框 + 模型响应等值线
            vis = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR).copy()
            g = (prob > 0.30).astype(np.uint8) * 255
            g = cv2.resize(g, (1280, 720), interpolation=cv2.INTER_NEAREST)
            cnts, _ = cv2.findContours(g, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(vis, cnts, -1, (0, 255, 0), 1)
            for rname, x0, x1, y0, y1 in rois:
                cv2.rectangle(vis, (x0, y0), (x1, y1), (0, 0, 255), 2)
                cv2.putText(vis, rname, (x0, max(20, y0 - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 2)
            bar = np.full((24, 1280, 3), 20, np.uint8)
            cv2.putText(bar, f"{name} {skey} fid={fid} "
                        "(red box=量测ROI, green=模型响应>0.30)",
                        (6, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
            zooms.append(np.vstack([bar, vis[360:720]]))

    cv2.imwrite(str(OUT / "blindspot_point.jpg"), np.vstack(zooms),
                [cv2.IMWRITE_JPEG_QUALITY, 88])
    md = ["# 点测：贴脸大虚线 ROI 内的概率（维护者指认帧）", "",
          "ROI 为人工划定（箭头指认），框见 blindspot_point.jpg；对照=同帧黄线走廊。", ""]
    for name, bysess in summary.items():
        md.append(f"## {name}")
        for fk, rows in bysess.items():
            for r in rows:
                md.append(f"- **{fk}** {r['roi']}: med={r['med']} "
                          f"p90={r['p90']} max={r['max']}")
        md.append("")
    (OUT / "blindspot_point.md").write_text("\n".join(md), encoding="utf-8")
    (OUT / "blindspot_point.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"输出：{OUT}")


if __name__ == "__main__":
    main()
