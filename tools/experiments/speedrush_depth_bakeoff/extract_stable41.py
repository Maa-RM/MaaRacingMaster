# -*- coding: utf-8 -*-
"""稳定 41 段提取（2026-11-11 重写，修复首版两个筛选错误）。

首版错误（维护者目检推翻）：
1. **HUD 行号错当帧号**：hud.jsonl 的 seq 是 OCR 行计数器，不是原始帧号
   （HUD seq 16 实际对应 raw seq 79）——按 seq 拷 raw 帧全部错位，拷到的
   是错位起步帧，图里速度五花八门。
2. **单行=41 非稳定段**：HUD 稀疏采样（整段约 60 行，相邻行隔 1~2s），
   速度在 38~55 摆动，单行=41 只是「路过 41」；起步阶段加速爬升必路过 41，
   所以筛出的帧堆在起步区段。

本版判据（维护者定调：稳定在 41、不是起步路过）：
- HUD 连续 ≥3 行（seq 相邻无跳）全部 trusted 速度=41 → 稳定 41 游程；
- 逐行经 frames.jsonl 按 frame_id 最近对齐到原始帧（Δ<100ms 且文件存在）；
- 记录游程前后各 2 行速度作上下文，供目检核对是否真稳定。

速度取值：rate_a 可信用 rate_a，否则 rate_b 可信用 rate_b（同前版口径）。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

DEBUG = Path(r"C:/Users/yomen/AppData/Roaming/MaaRacingMaster/debug")
OUT_BASE = Path(__file__).parent / "frames"
MIN_RUN = 3        # 连续 =41 的最少 HUD 行数
ALIGN_MS = 100.0   # frame_id 最近对齐容忍差
RATE = 41


def speed_of(row: dict) -> float | None:
    fa = row["fields"].get("rate_a") or {}
    fb = row["fields"].get("rate_b") or {}
    if fa.get("trusted"):
        return fa.get("value")
    if fb.get("trusted"):
        return fb.get("value")
    return None


def find_runs(rows: list[dict]) -> tuple[list[list[int]], list[float | None]]:
    """连续 seq 且速度全=41 的游程（返回行下标列表）与逐行速度。"""
    vals = [speed_of(r) for r in rows]
    runs, cur = [], []
    for i, v in enumerate(vals):
        if v == RATE and (not cur or rows[i]["seq"] == rows[cur[-1]]["seq"] + 1):
            cur.append(i)
        else:
            if len(cur) >= MIN_RUN:
                runs.append(cur)
            cur = [i] if v == RATE else []
    if len(cur) >= MIN_RUN:
        runs.append(cur)
    return runs, vals


def main() -> None:
    n_runs, n_frames = 0, 0
    for hud_file in sorted(DEBUG.glob("**/hud.jsonl")):
        drive_dir = hud_file.parent.parent
        drive = drive_dir.name
        frames_jsonl = drive_dir / "frames.jsonl"
        raw_dir = drive_dir / "raw"
        if not frames_jsonl.exists() or not raw_dir.is_dir():
            continue
        rows = [json.loads(l) for l in
                hud_file.read_text(encoding="utf-8").splitlines()]
        raw_index = {r["frame_id"]: r for r in
                     (json.loads(l) for l in
                      frames_jsonl.read_text(encoding="utf-8").splitlines())}
        runs, vals = find_runs(rows)
        if not runs:
            continue
        out = OUT_BASE / f"{drive}_stable41"
        seg_frames = []
        for run in runs:
            ctx = ([(rows[i]["seq"], vals[i]) for i in range(run[0] - 2, run[0])
                    if i >= 0],
                   [(rows[i]["seq"], vals[i]) for i in range(run[-1] + 1,
                                                              run[-1] + 3)
                    if i < len(rows)])
            print(f"{drive}: HUD 行 {rows[run[0]]['seq']}~{rows[run[-1]]['seq']} "
                  f"连续 =41，前文 {ctx[0]} 后文 {ctx[1]}")
            for i in run:
                h = rows[i]
                near = min(raw_index.values(),
                           key=lambda r: abs(r["frame_id"] - h["frame_id"]))
                d_ms = abs(near["ts_ns"] - h["ts_ns"]) / 1e6
                if d_ms > ALIGN_MS or not (raw_dir / near["file"].split("/")[-1]).exists():
                    print(f"  [skip] HUD seq {h['seq']}: 对齐 Δ{d_ms:.0f}ms "
                          f"或 raw 缺失")
                    continue
                seg_frames.append({
                    "hud_seq": h["seq"], "frame_id": h["frame_id"],
                    "ts_ns": h["ts_ns"], "file": near["file"].split("/")[-1],
                    "align_ms": round(d_ms, 1)})
        if len(seg_frames) < MIN_RUN:
            continue
        out.mkdir(parents=True, exist_ok=True)
        for f in seg_frames:
            shutil.copy2(raw_dir / f["file"], out / f["file"])
        (out / "meta.json").write_text(json.dumps(
            {"segment": drive, "rate_value": RATE, "min_run": MIN_RUN,
             "frames": seg_frames}, indent=1, ensure_ascii=False),
            encoding="utf-8")
        print(f"  → {out.name}: {len(seg_frames)} 帧")
        n_runs += 1
        n_frames += len(seg_frames)
    print(f"\n== 共 {n_runs} 段 {n_frames} 帧")


if __name__ == "__main__":
    main()
