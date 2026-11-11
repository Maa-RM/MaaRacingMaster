# -*- coding: utf-8 -*-
"""渲染对齐（校准对齐）可视化：深度模型公平对比面板。

核实问题（维护者 2026-11-11）：各模型深度图的分辨率 / 归一化 / 近远约定 /
绝对尺度互不一致，render_overlay 那种未对齐渲染的肉眼对比不公平——先校准
再下结论。本脚本与 render_overlay 的差异（校准要求逐条）：

1. 同一显示尺度（米制优先）：所有模型先校准到同一「米」刻度再映射颜色。
   - MoGe 生产权重：米制直读。pb.run_moge 内已乘 gate0 depth_scale 0.893，
     此处不再重复乘（与 probe_bakeoff 口径一致；注意 probe_taxi_distance /
     probe_smoothness 在 run_moge 输出之上对 prod 又乘了一次 0.893，属双重
     校正，其 prod 距离序列约偏小 10%，本脚本不复刻）。
   - 其余模型（含 moge2_vits，其 metric 头在本场景偏大 ~1.7x，不作米制）：
     两点仿射标定 depth = a·V + b——
       近点 = 最底一行「路缘两侧都进画面」的行（全幅路宽 = 14m，RULES
       四车道 × 3.5m 画线），物理深度用反投影 z1 = fx_px·W / wpx
       （与 pb.road_width_m 同一 backprojection 关系）；与出租车框相交的
       行不取（车身截断路面平段，路宽被低估）。
       远点 = 近点行与地平线之间的行 r2，物理深度不用它自己的路宽（高处
       平段常被截断 / 边缘不可靠），而用地面透视关系（hyperbolic ground
       model，锚定 gate0 地平线 v_h = 354.2）外推：z2 = z1·(r1−v_h)/(r2−v_h)。
       物理有效性：近=小约定下远场行 V 必须大于近场行（dv > 0，即地面
       近远序成立）且 a > 0；否则如实退回 percentile 归一（未米制），显示
       值域仍用与 MoGe 相同的共享 [vmin, vmax]（只保证显示约定一致）。
2. 同一色彩映射：turbo + 同一「值→颜色」映射——同一帧所有模型共用同一
   [vmin, vmax] = MoGe 米制校准后该帧的 2%~98% 分位值域；近=亮、暗=远。
   仿射输出 ≤ 0（负深度，外推无物理意义）按无效置 NaN。
3. 同一输出分辨率：全部 720×1280；源帧行放最上；所有面板画地平线
   （行 354，gate0 y_h=354.2 取整）、出租车检测框（白）、框底 25% 采样
   条带（黄）。
4. 同帧同序：先 seq48（检测框清晰），再 seq34、seq50 共 3 帧；每帧一个
   网格（源行 + 各模型行，一模型一行），各出一张 aligned_seq<seq>.jpg。
5. 每个面板标注：模型名 + 校准方式 + 框底条带读数（米；percentile 模型为
   模型单位并明确标注 no-metric）。

自包含：不 import maaracing_master、不改既有脚本；复用 probe_bakeoff 的
会话/适配器/近=小换算与 probe_taxi_distance 的检测/跟踪/条带读数。
"""
from __future__ import annotations

import gc
import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import probe_bakeoff as pb  # noqa: E402  复用 make_session/各模型 run/近=小 换算
import probe_taxi_distance as pt  # noqa: E402  复用 detect_cars/track_taxi/band_z

ROOT = Path(__file__).parent
SEG = ROOT / "frames" / "20261111_104222_p2_rate41"
OUT = ROOT / "bakeoff_out"

HORIZON_ROW = 354            # gate0 y_h = 354.2，取整画线
DEPTH_SCALE = 0.893          # gate0 depth_scale：已含在 pb.run_moge 内，不再重复乘
ROAD_FULL_M = 14.0           # RULES 四车道 × 3.5m 画线（仿射近点的物理尺）
MOGE_FX_PX = pb.MOGE_FX_NORM * 1280.0  # 归一化焦距 → 像素焦距（gate0 notes）
MIN_ROW_GAP = 30             # 远点行与近点行的最小行距
MIN_FLAT = 120               # 全幅路宽平段最短像素（同 pt.road_full_width_px）

SEQS = [48, 34, 50]          # 渲染顺序：seq48（框清晰）→ 34 → 50
PANEL_W, PANEL_H = 1280, 720

SPECS = [                    # 与 probe_smoothness.DEPTH_SPECS 同一组模型
    ("moge_prod_q4f16", "prod_moge_q4f16.onnx", None),
    ("moge2_vits_fp32", "moge2_vits/model_fp32.onnx", None),
    ("zipdepth_fp16", "zipdepth/zipdepth_fp16_opset17.onnx", None),
    ("depthart_mnv4s_448", "depthart/relative_mnv4s_448_default.onnx", None),
    ("yolo26n", "yolo26_depth/yolo26n-depth.onnx", None),
    ("lite_mono_640x192", "lite_mono_base_640x192", (192, 640)),
    ("midas_small", "midas/model-small.onnx", None),
]

METHOD_TXT = {"metric": "metric-direct",
              "affine": "2pt-affine",
              "percentile": "percentile(no-metric)"}


# ── 两点仿射标定 ──────────────────────────────────────────────────────────

def metric_rows(v: np.ndarray, valid: np.ndarray, box) -> list[dict]:
    """收集「路缘两侧都进画面」的行（全幅路宽 = 14m 物理尺）。

    行级判据复刻 probe_taxi_distance.road_full_width_px（那里只返回最底
    一行）；这里收集全部合格行供报告与近点选择。与出租车框相交的行跳过
    （车身截断路面平段，量到的宽 < 全幅 14m）。每行附：wpx（平段宽 px）、
    v（平段 V 中位，近=小约定）、z = fx_px·W / wpx（反投影米制深度）。"""
    h, w = v.shape
    x0, y0, x1, y1 = (int(round(c)) for c in box)
    out = []
    for r in range(min(700, h - 1), 380, -1):
        if y0 - 4 <= r <= y1 + 4:
            continue                      # 出租车框行：路宽被车身截断，不可信
        row = v[r].astype(np.float64)
        ok = valid[r] & np.isfinite(row)
        if ok.sum() < 100:
            continue
        med = np.median(row[ok])
        mad = np.median(np.abs(row[ok] - med)) * 1.4826 + 1e-9
        flat = ok & (np.abs(row - med) < 3.0 * mad)
        best_len, best_iv, run, start = 0, None, 0, 0
        for i in range(w):
            if flat[i]:
                if run == 0:
                    start = i
                run += 1
                if run > best_len:
                    best_len, best_iv = run, (start, i + 1)
            else:
                run = 0
        if best_iv is None or best_len < MIN_FLAT:
            continue
        a, b = best_iv
        # 平段两端都在画面内 ⇒ 路缘两侧都进画面（同 pt.road_full_width_px）
        if a > 0.02 * w and b < 0.98 * w:
            vmed = float(np.median(row[a:b]))
            z = MOGE_FX_PX * ROAD_FULL_M / float(best_len)
            out.append({"row": r, "wpx": float(best_len), "v": vmed, "z": z})
    return out


def calibrate_affine(v: np.ndarray, valid: np.ndarray, rows: list[dict],
                     box) -> tuple[dict | None, str]:
    """两点仿射标定 depth = a·V + b（近点 + 透视关系远点），返回 (系数, 原因)。

    近点：最底一行合格行（r1, wpx1, V1），z1 = fx_px·14 / wpx1。
    远点：r2 = r1 − max(30, (r1−380)//2)（且不低于地平线下 25 行），其物理
    深度由地面透视关系外推 z2 = z1·(r1−v_h)/(r2−v_h)（锚定 gate0 地平线
    354.2，即「地平线 354 处归一」），V2 = 该行有效像素中位（排除出租车框
    列 ±4，框内不是路面）。仿射只保证路面标定段内准确；框底条带的 V 若在
    标定段之外（近障碍 / 远物），读数是仿射外推，报告中如实标注。
    """
    v_h = pb.HORIZON_V
    r1, wpx1, v1 = rows[0]["row"], rows[0]["wpx"], rows[0]["v"]
    z1 = MOGE_FX_PX * ROAD_FULL_M / wpx1
    r2 = r1 - max(MIN_ROW_GAP, (r1 - 380) // 2)
    r2 = max(r2, int(v_h) + 25)
    x0, y0, x1, y1 = (int(round(c)) for c in box)
    row = v[r2]
    ok = valid[r2] & np.isfinite(row)
    ok[max(0, x0 - 4):min(ok.size, x1 + 5)] = False   # 排除出租车框列
    if ok.sum() < 50:
        return None, "far_row_empty"
    v2 = float(np.median(row[ok]))
    z2 = z1 * (r1 - v_h) / (r2 - v_h)
    dv = v2 - v1
    if not np.isfinite(dv) or dv <= 0:
        # 远场行 V 不大于近场行 ⇒ 该帧地面近远序不成立，仿射无物理意义
        return None, "dv_nonpos"
    a = (z2 - z1) / dv
    b = z1 - a * v1
    if not (np.isfinite(a) and np.isfinite(b)) or a <= 0:
        return None, "a_nonpos"
    return ({"a": float(a), "b": float(b), "r1": r1, "wpx1": wpx1,
             "z1": z1, "v1": v1, "r2": r2, "z2": z2, "v2": v2}), "ok"


# ── 渲染 ─────────────────────────────────────────────────────────────────

def colorize(v: np.ndarray, valid: np.ndarray, vmin: float,
             vmax: float) -> np.ndarray:
    """turbo 热图，亮=近、暗=远（与 render_overlay.heat 同约定）；
    无效/NaN 像素置黑。所有模型共用调用方传入的同一 [vmin, vmax]。"""
    norm = np.clip((v - vmin) / (vmax - vmin + 1e-9), 0.0, 1.0)
    norm = np.nan_to_num(norm, nan=1.0)   # NaN → 0 亮度，且避免 uint8 cast 告警
    img = cv2.applyColorMap(((1.0 - norm) * 255).astype(np.uint8),
                            cv2.COLORMAP_TURBO)
    img[~valid] = 0
    return img


def annotate(img: np.ndarray, box, title: str, sub: str) -> np.ndarray:
    """所有面板统一画：地平线（行 354，品红）、检测框（白）、框底 25%
    条带（黄）、模型名 + 校准方式 + 条带读数。"""
    cv2.line(img, (0, HORIZON_ROW), (PANEL_W - 1, HORIZON_ROW), (255, 0, 255), 1)
    x0, y0, x1, y1 = (int(round(c)) for c in box)
    cv2.rectangle(img, (x0, y0), (x1, y1), (255, 255, 255), 2)
    yb = y0 + (y1 - y0) * 3 // 4
    cv2.rectangle(img, (x0, yb), (x1, y1), (0, 255, 255), 2)
    cv2.putText(img, title, (8, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                (255, 255, 255), 2)
    cv2.putText(img, sub, (8, 62), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                (0, 255, 255), 2)
    return img


def build_runner(name: str, rel: str, hw):
    """逐模型建会话 + 推理适配器（分发复刻 render_overlay，不改既有脚本）。"""
    if name.startswith("moge"):
        sess = pb.make_session(rel, free_dims={"batch_size": 1, "height": 720,
                                               "width": 1280})
        return sess, (lambda rgb, s=sess: pb.run_moge(s, rgb))
    if name.startswith("lite_mono"):
        enc = pb.make_session(rel + "/encoder.onnx")
        dec = pb.make_session(rel + "/decoder.onnx")
        return enc, (lambda rgb, e=enc, d=dec, h_=hw:
                     pb.run_lite_mono(e, d, rgb, h_))
    if name.startswith("yolo26"):
        sess = pb.make_session(rel)
        return sess, (lambda rgb, s=sess: pb.run_yolo26(s, rgb))
    if name.startswith("zipdepth"):
        sess = pb.make_session(rel)
        return sess, (lambda rgb, s=sess: pb.run_zipdepth(s, rgb))
    if name.startswith("depthart"):
        sess = pb.make_session(rel)
        return sess, (lambda rgb, s=sess: pb.run_depthart(s, rgb, False))
    sess = pb.make_session(rel)   # midas
    return sess, (lambda rgb, s=sess: pb.run_midas(s, rgb))


def main() -> None:
    OUT.mkdir(exist_ok=True)
    meta = json.loads((SEG / "meta.json").read_text(encoding="utf-8"))
    info = meta["frames"]
    frames = [cv2.cvtColor(cv2.imread(str(SEG / r["file"])), cv2.COLOR_BGR2RGB)
              for r in info]
    print(f"segment {SEG.name}: {len(frames)} frames @ {frames[0].shape}")

    # 检测 + 跟踪（perception 检测，ROI (0,201,1280,561)；双向最近中心匹配）
    det_sess = pb.make_session("prod_perception/model.onnx")
    dets = [pt.detect_cars(det_sess, fr) for fr in frames]
    del det_sess
    gc.collect()
    track = pt.track_taxi(dets)

    # 列帧 = seq 48 → 34 → 50（同帧同序）
    cols = []
    for seq in SEQS:
        idx = next((i for i, r in enumerate(info) if r["seq"] == seq), None)
        if idx is None or track[idx] is None:
            raise SystemExit(f"seq{seq}: 无跟踪框 (idx={idx})，无法渲染")
        cols.append(idx)
    print("columns:", [f"seq{info[i]['seq']} box={track[i]}" for i in cols])

    # 逐模型：建会话 → 3 帧推理 + 校准 → del 释放显存（不增 GPU 并发）
    per_model: list[tuple[str, list[dict]]] = []
    for name, rel, hw in SPECS:
        sess, run = build_runner(name, rel, hw)
        fr_data = []
        for i in cols:
            box = track[i]
            v_raw, valid = run(frames[i])
            v = pb.to_near_small(v_raw)              # 统一「近=小」，<=0 → NaN
            v = pb.resize_to(v, (PANEL_H, PANEL_W))  # 统一 720×1280
            valid = pb.resize_to(valid.astype(np.float32),
                                 (PANEL_H, PANEL_W)) > 0.5
            rec: dict = {}
            if name.startswith("moge_prod"):
                # 校准 1（米制直读）：run_moge 内已乘 gate0 depth_scale 0.893
                rec["method"] = "metric"
                rec["v"] = v
                rec["valid"] = valid
            else:
                # 校准 1（两点仿射）：近点 = 路缘两侧进画面行的全幅 14m 路宽
                rows = metric_rows(v, valid, box)
                if not rows:
                    af, why = None, "rows<2"
                else:
                    af, why = calibrate_affine(v, valid, rows, box)
                if af:
                    a, b = af["a"], af["b"]
                    vc = a * v + b
                    ok2 = valid & np.isfinite(vc) & (vc > 0)  # 负深度=外推无意义
                    rec["method"] = "affine"
                    rec["v"] = np.where(ok2, vc, np.nan)
                    rec["valid"] = ok2
                else:
                    # 解不出两点：如实标注 percentile 归一（未米制），
                    # 显示仍用与 MoGe 相同的共享 [vmin, vmax]
                    rec["method"] = "percentile"
                    rec["v"] = v
                    rec["valid"] = valid
                rec["affine"] = af
                rec["affine_why"] = why
                rec["metric_rows"] = [
                    {"row": r["row"], "wpx": r["wpx"], "z_m": round(r["z"], 2),
                     "v": round(r["v"], 8)} for r in rows]
            # 框底条带读数（校准后 = 米；percentile 模型 = 模型单位）
            rec["band"] = pt.band_z(rec["v"], box)
            fr_data.append(rec)
        per_model.append((name, fr_data))
        del sess, run
        gc.collect()
        msg = "  ".join(
            f"seq{info[i]['seq']}="
            f"{d['band'] if d['band'] is None else round(d['band'], 3)}"
            f"({METHOD_TXT[d['method']]})"
            for i, d in zip(cols, fr_data))
        print(f"[done] {name}: {msg}")

    # 校准 2（同一值域）：MoGe 米制校准后每帧 2%~98% 分位，全模型共用
    ranges = []
    for k in range(len(cols)):
        d = per_model[0][1][k]          # SPECS[0] = moge_prod_q4f16
        vv = d["v"][d["valid"] & np.isfinite(d["v"])]
        lo, hi = (float(x) for x in np.percentile(vv, [2, 98]))
        ranges.append((lo, hi))
    print("shared [vmin, vmax] (MoGe p2~p98):",
          [(round(lo, 2), round(hi, 2)) for lo, hi in ranges])

    # 校准 3/4：每帧一张网格（源行最上 + 一模型一行），全部 720×1280 面板
    saved = []
    for k, i in enumerate(cols):
        seq = info[i]["seq"]
        vmin, vmax = ranges[k]
        rows_img = []
        # 源帧行放最上（同样画地平线/框/条带）
        im = cv2.cvtColor(frames[i], cv2.COLOR_RGB2BGR)
        if im.shape[:2] != (PANEL_H, PANEL_W):
            im = cv2.resize(im, (PANEL_W, PANEL_H), interpolation=cv2.INTER_AREA)
        rows_img.append(annotate(im, track[i], f"source seq{seq}",
                                 f"shared range [{vmin:.2f}, {vmax:.2f}] m"))
        for name, fr_data in per_model:
            d = fr_data[k]
            img = colorize(d["v"], d["valid"], vmin, vmax)
            zb = d["band"]
            if zb is None:
                btxt = "band=n/a"
            elif d["method"] == "percentile":
                btxt = f"band={zb:.3g}u(no-metric)"
            else:
                btxt = f"band={zb:.1f}m"
            sub = METHOD_TXT[d["method"]] + "  " + btxt
            if d.get("affine"):
                sub += f"  a={d['affine']['a']:.4g} b={d['affine']['b']:.4g}"
            rows_img.append(annotate(img, track[i], f"{name} seq{seq}", sub))
        canvas = np.vstack(rows_img)
        outp = OUT / f"aligned_seq{seq}.jpg"
        cv2.imwrite(str(outp), canvas, [cv2.IMWRITE_JPEG_QUALITY, 88])
        saved.append(outp)
        print("saved:", outp, f"({canvas.shape[1]}x{canvas.shape[0]})")

    # 标定明细（供报告与后续核对；与既有 *_results.json 同区存放）
    report = {
        "segment": SEG.name,
        "seqs": SEQS,
        "boxes": {f"seq{info[i]['seq']}": track[i] for i in cols},
        "shared_range_p2_p98_moge": {f"seq{info[i]['seq']}": ranges[k]
                                     for k, i in enumerate(cols)},
        "models": {
            name: [
                {"seq": info[i]["seq"], "method": d["method"],
                 "band": d["band"],
                 "affine": d.get("affine"), "affine_why": d.get("affine_why"),
                 "metric_rows": d.get("metric_rows")}
                for i, d in zip(cols, fr_data)
            ]
            for name, fr_data in per_model
        },
    }
    rp = OUT / "aligned_report.json"
    rp.write_text(json.dumps(report, indent=1, ensure_ascii=False),
                  encoding="utf-8")
    print("saved:", rp)
    print("ALL OK")


if __name__ == "__main__":
    main()
