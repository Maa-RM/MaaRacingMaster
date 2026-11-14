# -*- coding: utf-8 -*-
"""把「框底行」与「深度修正接触行」画到同一帧上，供肉眼判定哪条线真的接地。

输入 `adjacent_seq_out/adjacent_seq.json`（`probe_adjacent_seq.py` 的产物）：
- 绿框：YOLO 框；
- 红线：地平线 `y_h`；
- 蓝线：框底行 `y_bot`（几何测距用的接触行）；
- 紫线：深度修正行 `r* = y_bot + corr_px`（深度测距用的接触行）；
- 右侧为 4~8 倍最近邻放大，看清两条线相对轮胎/阴影的位置。

只渲染，不改任何读数。输出 `contact_out/`（实验产物，gitignored）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import probe_adjacent_seq as P  # noqa: E402

ROOT = Path(__file__).parent
SRC = ROOT / "adjacent_seq_out" / "adjacent_seq.json"
OUT = ROOT / "contact_out"

GREEN = (0, 255, 0)
RED = (0, 0, 255)
BLUE = (255, 0, 0)
MAGENTA = (255, 0, 255)
YELLOW = (0, 255, 255)
WHITE = (255, 255, 255)


def drive_path(name: str) -> Path:
    for sess, drives in P.SESSIONS:
        if name in drives:
            return P.DEBUG / sess / "speedrush" / "drive" / name
    raise KeyError(name)


def pick_rows(rows: list[dict], n: int = 6) -> list[dict]:
    valid = [r for r in rows if r["z_geo"] and r.get("corr_px") is not None]
    if not valid:
        return []
    valid.sort(key=lambda r: -r["z_geo"])
    qs = np.linspace(0.05, 0.95, n)
    out, used = [], set()
    for q in qs:
        idx = int(round(q * (len(valid) - 1)))
        if idx in used:
            continue
        used.add(idx)
        out.append(valid[idx])
    return out


def draw_lines(img, row, scale_note: bool = True) -> None:
    x0, y0, x1, y1 = row["box"]
    yh = row["y_h"]
    corr = row["corr_px"]
    r_star = y1 + corr
    cv2.rectangle(img, (x0, y0), (x1, y1), GREEN, 2)
    cv2.line(img, (0, int(round(yh))), (img.shape[1] - 1, int(round(yh))),
             RED, 1)
    cv2.line(img, (x0, int(y1)), (x1, int(y1)), BLUE, 1)
    cv2.line(img, (x0, int(round(r_star))), (x1, int(round(r_star))),
             MAGENTA, 1)
    if scale_note:
        lines = [
            f"z_geo={row['z_geo']:.1f}m",
            "z_dep=-" if row["z_dep"] is None else f"z_dep={row['z_dep']:.1f}m",
            f"y_bot={y1}  r*={r_star:.1f}  corr={corr:+.1f}px",
            f"h={row['h']}px  w={row['w']}px",
        ]
        for i, t in enumerate(lines):
            cv2.putText(img, t, (12, 28 + 26 * i),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.62, WHITE, 2)


def zoom(img, row) -> np.ndarray:
    """接触区放大：只截车身下缘到路面，8~16 倍最近邻，两条接触行原样保留。"""
    x0, y0, x1, y1 = row["box"]
    h = y1 - y0
    scale = 16 if h < 24 else (10 if h < 48 else (6 if h < 90 else 4))
    mx = max(6, min(24, (x1 - x0) // 3))
    top = max(0, y1 - h - 4)
    cy0, cy1 = top, min(img.shape[0], y1 + max(10, h // 6))
    cx0, cx1 = max(0, x0 - mx), min(img.shape[1], x1 + mx)
    crop = img[cy0:cy1, cx0:cx1].copy()
    crop = cv2.resize(crop, (crop.shape[1] * scale, crop.shape[0] * scale),
                      interpolation=cv2.INTER_NEAREST)
    for y, color in ((y1, BLUE), (y1 + row["corr_px"], MAGENTA)):
        yy = int(round((y - cy0) * scale))
        if 0 <= yy < crop.shape[0]:
            cv2.line(crop, (0, yy), (crop.shape[1] - 1, yy), color, 1)
    cv2.putText(crop, f"x{scale}  blue=y_bot  magenta=r*", (6, 16),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, YELLOW, 1)
    return crop


def main() -> None:
    OUT.mkdir(exist_ok=True)
    data = json.loads(SRC.read_text(encoding="utf-8"))
    n = 0
    for seq in data["sequences"]:
        drive = drive_path(seq["drive"])
        for row in pick_rows(seq["rows"]):
            img = cv2.imread(str(drive / row["file"]))
            if img is None:
                continue
            full = img.copy()
            draw_lines(full, row)
            crop = zoom(img, row)
            hh = max(full.shape[0], crop.shape[0])
            canvas = np.full((hh, full.shape[1] + crop.shape[1], 3), 30,
                             np.uint8)
            canvas[:full.shape[0], :full.shape[1]] = full
            canvas[:crop.shape[0], full.shape[1]:] = crop
            name = (f"{seq['drive']}_t{seq['track_idx']}_k{row['k']:04d}"
                    f"_z{row['z_geo']:.0f}.png")
            cv2.imwrite(str(OUT / name), canvas)
            n += 1
    print(f"rendered {n} overlays -> {OUT}")


if __name__ == "__main__":
    main()
