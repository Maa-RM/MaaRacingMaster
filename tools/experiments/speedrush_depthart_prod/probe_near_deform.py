# -*- coding: utf-8 -*-
"""近场变形区间：全帧检测下，贴脸/截断车辆的框底几何 vs 深度。

**为什么单独做**：`probe_adjacent_seq.py` 复用了旧出租车脚本的检测 ROI
（y 201~561），近车框底被裁在 561 行，几何在约 3.7 m 处饱和——那段“近场”
数据不能代表生产。生产感知是**全帧推理、不设 ROI**，本探针按同一口径重做。

**要回答**：
1. 贴脸车的框底什么时候还真的接地（框底未出画），什么时候是画面下缘（出画）；
2. 框底出画/横向截断时，框底行几何给出什么（饱和值），深度模型给出什么；
3. 深度在这些变形帧上是否可用（MAD 门），可用时与几何差多少。

输出 `near_deform_out/near_deform.json`、接触区叠加图与摘要。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import probe_adjacent_seq as P  # noqa: E402
import probe_depthart_prod_sim as sim  # noqa: E402
import render_contact_overlay as R  # noqa: E402

ROOT = Path(__file__).parent
OUT = ROOT / "near_deform_out"
OUT_IMG = OUT / "img"
IMGSZ = 640
CONF = 0.35
Z_NEAR_MAX = 8.0
MAX_PER_DRIVE = 22
DRIVES = ["20261113_120909_p2", "20261113_154216_p2",
          "20261113_224456_p2", "20261113_110314_p2"]


def detect_cars_full(sess, rgb: np.ndarray) -> list[tuple]:
    """全帧检测（生产口径：perception 全帧推理、不设 ROI）。"""
    h, w = rgb.shape[:2]
    scale = min(IMGSZ / h, IMGSZ / w)
    nh, nw = int(h * scale), int(w * scale)
    px, py = (IMGSZ - nw) // 2, (IMGSZ - nh) // 2
    padded = np.full((IMGSZ, IMGSZ, 3), 114, np.uint8)
    padded[py:py + nh, px:px + nw] = cv2.resize(
        rgb, (nw, nh), interpolation=cv2.INTER_LINEAR)
    blob = padded.transpose(2, 0, 1)[None].astype(np.float32) / 255.0
    preds = sess.run(None, {"images": blob})[0][0].T
    xywh, cls_conf = preds[:, :4], preds[:, 4:]
    scores, classes = cls_conf.max(1), cls_conf.argmax(1)
    m = (classes == 1) & (scores > CONF)
    if not m.any():
        return []
    b = xywh[m]
    xy = np.stack([b[:, 0] - b[:, 2] / 2, b[:, 1] - b[:, 3] / 2,
                   b[:, 0] + b[:, 2] / 2, b[:, 1] + b[:, 3] / 2], 1)
    keep = cv2.dnn.NMSBoxes(xy.tolist(), scores[m].tolist(), 0.0, 0.45)
    out = []
    for i in np.array(keep).ravel():
        a1, b1, a2, b2 = xy[int(i)]
        out.append((max(0, int((a1 - px) / scale)),
                    max(0, int((b1 - py) / scale)),
                    min(w - 1, int((a2 - px) / scale)),
                    min(h - 1, int((b2 - py) / scale))))
    return out


def classify(box, z: float) -> dict:
    x0, y0, x1, y1 = box
    bottom = y1 >= 714
    left = x0 <= 2
    right = x1 >= 1277
    return {"z": z, "bottom_clip": bottom, "side_clip": left or right,
            "both": bottom and (left or right),
            "h": y1 - y0, "w": x1 - x0,
            "h_frac": (y1 - y0) / 720.0,
            "aspect": (x1 - x0) / max(1, y1 - y0)}


def main() -> None:
    OUT.mkdir(exist_ok=True)
    OUT_IMG.mkdir(exist_ok=True)
    scan = P.load_scan()
    det = sim.session("prod_perception/model.onnx")
    da = sim.session("depthart/relative_mnv4m_slim_spf_448_default.onnx")
    rows = []
    for name in DRIVES:
        if name not in scan:
            continue
        d = scan[name]
        drive = d["drive"]
        recs = d["recs"]
        cand = []
        dets_by_k = {}
        for k, rec in enumerate(recs):
            img = cv2.imread(str(drive / rec["file"]))
            if img is None:
                continue
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            dets = detect_cars_full(det, rgb)
            dets_by_k[k] = dets
            yh = d["yvs"].get(k) or P.YH_FALLBACK
            best = None
            for b in dets:
                z = P.geom_z(b, yh)
                if z is None:
                    continue
                h = b[3] - b[1]
                clip = b[3] >= 714 or b[0] <= 2 or b[2] >= 1277
                if z > Z_NEAR_MAX and h < 160 and not clip:
                    continue
                if best is None or z < best[1]:
                    best = (b, z)
            if best is not None:
                cand.append((k, best[0], best[1]))
        if len(cand) > MAX_PER_DRIVE:
            idx = np.linspace(0, len(cand) - 1, MAX_PER_DRIVE).astype(int)
            cand = [cand[i] for i in sorted(set(int(v) for v in idx))]
        print(f"{name}: full-frame near candidates {len(cand)}", flush=True)
        for k, box, z in cand:
            rec = recs[k]
            img = cv2.imread(str(drive / rec["file"]))
            if img is None:
                continue
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            yh = d["yvs"].get(k) or P.YH_FALLBACK
            dm = P.depth_measure(rgb, box, yh, da, dets_by_k.get(k, (box,)))
            cls = classify(box, z)
            row = {"drive": name, "k": k, "file": rec["file"],
                   "box": list(box), "y_h": yh,
                   "z_geo": z, "z_h": (P.CAR_H * P.FY / max(1, box[3] - box[1])),
                   "x_cam": P.cam_x(box, z),
                   "z_dep": dm.get("z"), "mad": dm.get("mad"),
                   "dep_skip": dm.get("skip"),
                   "corr_px": dm.get("corr_px"), **cls}
            rows.append(row)
            vis = img.copy()
            R.draw_lines(vis, {"box": box, "y_h": yh, "corr_px":
                               dm.get("corr_px") or 0.0, "z_geo": z,
                               "z_dep": dm.get("z"), "h": box[3] - box[1],
                               "w": box[2] - box[0]})
            crop = R.zoom(img, {"box": box, "corr_px": dm.get("corr_px") or 0.0})
            hh = max(vis.shape[0], crop.shape[0])
            canvas = np.full((hh, vis.shape[1] + crop.shape[1], 3), 30,
                             np.uint8)
            canvas[:vis.shape[0], :vis.shape[1]] = vis
            canvas[:crop.shape[0], vis.shape[1]:] = crop
            cv2.imwrite(str(OUT_IMG / f"{name}_k{k:04d}_z{z:.1f}"
                            f"{'_B' if cls['bottom_clip'] else ''}"
                            f"{'_S' if cls['side_clip'] else ''}.png"),
                        canvas)
    summary = {}
    for key in ("all", "bottom_clip", "side_clip", "both", "clean"):
        if key == "all":
            sel = rows
        elif key == "clean":
            sel = [r for r in rows if not r["bottom_clip"] and not r["side_clip"]]
        else:
            sel = [r for r in rows if r[key]]
        ok = [r for r in sel if r["z_dep"] is not None]
        diff = [(r["z_dep"] - r["z_geo"]) / r["z_geo"] for r in ok]
        summary[key] = {
            "n": len(sel), "dep_ok": len(ok),
            "mad": sum(1 for r in sel if r["dep_skip"] == "mad"),
            "z_geo_med": P.med([r["z_geo"] for r in sel]),
            "z_dep_med": P.med([r["z_dep"] for r in sel]),
            "diff_med": P.med(diff),
        }
    (OUT / "near_deform.json").write_text(
        json.dumps({"summary": summary, "rows": rows},
                   indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    print("saved:", OUT / "near_deform.json")


if __name__ == "__main__":
    main()
