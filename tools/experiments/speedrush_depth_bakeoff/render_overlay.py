# -*- coding: utf-8 -*-
"""小号模型可视化叠加：深度热图 + 出租车框 + 底带/整框采样区 + 读数标注。

核实问题（维护者 2026-11-11）：同图差恰为 0 的 MNV4-S/MiDaS 是真稳定还是
超平滑退化；yolo26n/Lite-Mono 的同图差小是否可信。对照 = MoGe 生产权重。
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

import sys
sys.path.insert(0, str(Path(__file__).parent))
import probe_bakeoff as pb  # noqa: E402
import probe_taxi_distance as pt  # noqa: E402
from probe_smoothness import whole_box_z, DEPTH_SPECS  # noqa: E402

ROOT = Path(__file__).parent
OUT = ROOT / "bakeoff_out"
SEG = ROOT / "frames" / "20261111_104222_p2_rate41"
DEPTH_SCALE = 0.893

SHOW = ["moge_prod_q4f16", "zipdepth_fp16", "yolo26n", "lite_mono_640x192",
        "depthart_mnv4s_448", "midas_small"]
N_FRAMES = 3  # 首/中/末 跟踪帧


def heat(v: np.ndarray, box, band_rect) -> np.ndarray:
    lo, hi = np.nanpercentile(v, [2, 98])
    norm = np.clip((v - lo) / (hi - lo + 1e-9), 0, 1)
    img = cv2.applyColorMap(((1 - norm) * 255).astype(np.uint8),
                            cv2.COLORMAP_TURBO)  # 亮=近
    x0, y0, x1, y1 = box
    cv2.rectangle(img, (x0, y0), (x1, y1), (255, 255, 255), 2)
    bx0, by0, bx1, by1 = band_rect
    cv2.rectangle(img, (bx0, by0), (bx1, by1), (0, 255, 255), 2)  # 黄=底带
    return img


def main() -> None:
    meta = json.loads((SEG / "meta.json").read_text(encoding="utf-8"))
    info = meta["frames"]
    frames = [cv2.cvtColor(cv2.imread(str(SEG / r["file"])), cv2.COLOR_BGR2RGB)
              for r in info]
    det_sess = pb.make_session("prod_perception/model.onnx")
    dets = [pt.detect_cars(det_sess, fr) for fr in frames]
    track = pt.track_taxi(dets)
    idx = [i for i, b in enumerate(track) if b]
    if not idx:
        print("no tracked frames")
        return
    sel = [idx[0], idx[len(idx) // 2], idx[-1]]
    print(f"frames: {[info[i]['seq'] for i in sel]}")

    cells = []  # (label, heat_img, text)
    for name, rel, hw, is_prod in DEPTH_SPECS:
        if name not in SHOW:
            continue
        if name.startswith("moge"):
            sess = pb.make_session(rel, free_dims={"batch_size": 1,
                                                   "height": 720,
                                                   "width": 1280})
            run = lambda rgb, s=sess: pb.run_moge(s, rgb)
        elif name.startswith("lite_mono"):
            enc = pb.make_session(rel + "/encoder.onnx")
            dec = pb.make_session(rel + "/decoder.onnx")
            run = lambda rgb, e=enc, d=dec, h_=hw: pb.run_lite_mono(e, d, rgb, h_)
        elif name.startswith("yolo26"):
            sess = pb.make_session(rel)
            run = lambda rgb, s=sess: pb.run_yolo26(s, rgb)
        elif name.startswith("zipdepth"):
            sess = pb.make_session(rel)
            run = lambda rgb, s=sess: pb.run_zipdepth(s, rgb)
        elif name.startswith("depthart"):
            sess = pb.make_session(rel)
            run = lambda rgb, s=sess: pb.run_depthart(s, rgb, False)
        else:
            sess = pb.make_session(rel)
            run = lambda rgb, s=sess: pb.run_midas(s, rgb)

        for k, i in enumerate(sel):
            box = track[i]
            v_raw, valid = run(frames[i])
            v = pb.to_near_small(v_raw)
            v = pb.resize_to(v, (720, 1280))
            zb = pt.band_z(v, box)
            zw = whole_box_z(v, box)
            if is_prod and zb is not None:
                zb *= DEPTH_SCALE
            if is_prod and zw is not None:
                zw *= DEPTH_SCALE
            gh, gw = v.shape[:2]
            x0 = max(0, int(box[0] * gw / 1280)); y0 = max(0, int(box[1] * gh / 720))
            x1 = min(gw, int(box[2] * gw / 1280)); y1 = min(gh, int(box[3] * gh / 720))
            yb = y0 + (y1 - y0) * 3 // 4
            img = heat(v, (x0, y0, x1, y1), (x0, yb, x1, y1))
            txt = f"band={zb and round(zb,1)} whole={zw and round(zw,1)}"
            cells.append((f"{name} seq{info[i]['seq']}", img, txt))

    # 网格：每行 3 帧，一模型一行
    cols = len(sel)
    rows = []
    ci = 0
    for _ in SHOW:
        row_imgs, row_txts = [], []
        for _c in range(cols):
            if ci < len(cells):
                label, img, txt = cells[ci]
            else:
                label, img, txt = "", np.zeros_like(cells[0][1]), ""
            img = cv2.resize(img, (640, 360))
            cv2.putText(img, label, (8, 24), cv2.FONT_HERSHEY_SIMPLEX,
                        0.6, (255, 255, 255), 2)
            cv2.putText(img, txt, (8, 50), cv2.FONT_HERSHEY_SIMPLEX,
                        0.55, (0, 255, 255), 2)
            row_imgs.append(img)
            ci += 1
        rows.append(np.hstack(row_imgs))
    # 原图行放最上
    srcs = []
    for i in sel:
        im = cv2.resize(cv2.cvtColor(frames[i], cv2.COLOR_RGB2BGR), (640, 360))
        box = track[i]
        cv2.rectangle(im, (box[0] // 2, box[1] // 2),
                      (box[2] // 2, box[3] // 2), (255, 255, 255), 2)
        srcs.append(im)
    canvas = np.vstack([np.hstack(srcs)] + rows)
    outp = OUT / f"overlay_{SEG.name}.jpg"
    cv2.imwrite(str(outp), canvas, [cv2.IMWRITE_JPEG_QUALITY, 88])
    print("saved:", outp, f"({canvas.shape[1]}x{canvas.shape[0]})")


if __name__ == "__main__":
    main()
