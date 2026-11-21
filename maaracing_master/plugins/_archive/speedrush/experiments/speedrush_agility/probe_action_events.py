"""极限动作事件计数（speedrush）：用 HUD 横幅做得分通道的真值账。

**为什么不用 trace 的横距**：极限超车判定在「被超车被甩到身后」那一帧（RULES
§4.2.1），此刻被超车正在自车侧方、被自车遮挡，它的横位读数最差；用那一点的
横距估分会被读数噪声主导（2026-11-09 场次实证：一个拿了 6 个动作事件的阶段，
7 个掠过点在读数上全被分到「接触/欠调」）。横幅是游戏自己派的，不受我们的
估计误差影响，是唯一可用的地面真值。

**口径**：横幅文本 `极限超车 ×N` / `极限变道 ×N` / `木桶旋转 ×N`；文本每次
变化记一次事件（N 变化也算——那是新的一次同种动作）。OCR 乱码行丢弃。分速
从 trace 的 HUD 档位取，按时间戳把调试阶段目录对到 trace 阶段上。

用法：``.venv/Scripts/python.exe -X utf8 probe_action_events.py <session_token>``
（在 ``%APPDATA%/MaaRacingMaster`` 下找 ``debug/<token>`` 与 ``logs/<token>``）
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re

ACTIONS = ("极限超车", "极限变道", "木桶旋转")
NOISE = re.compile(r"[^\u4e00-\u9fff×0-9 ]")


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", "", text or "")


def _banner_events(hud_jsonl: str) -> tuple[collections.Counter, int, int]:
    ev: collections.Counter = collections.Counter()
    prev = ""
    t0 = t1 = 0
    for line in open(hud_jsonl, encoding="utf-8"):
        r = json.loads(line)
        f = r.get("fields", {})
        text = _norm((f.get("event_banner") or {}).get("text"))
        if text and text != prev and not NOISE.search(text):
            kind = next((a for a in ACTIONS if a in text), None)
            if kind:
                ev[kind] += 1
        prev = text
        ts = r.get("ts_ns")
        if ts:
            t0 = t0 or ts
            t1 = ts
    return ev, t0, t1


def _trace_stages(trace_jsonl: str) -> list[dict]:
    occ: dict[tuple, int] = {}
    stages: list[dict] = []
    cur = None
    for line in open(trace_jsonl, encoding="utf-8"):
        r = json.loads(line)
        kind = r.get("type")
        if kind == "stage_begin":
            base = (r.get("run_id"), r["stage_id"])
            occ[base] = occ.get(base, 0) + 1
            cur = {"key": base + (occ[base],), "t0": r["ts_mono_ns"],
                   "rates": []}
            stages.append(cur)
            continue
        if kind == "stage_end" and cur is not None:
            cur["t1"] = r["ts_mono_ns"]
            cur = None
            continue
        if kind is not None or cur is None:
            continue
        rate = r.get("hud_rate")
        if rate is not None:
            cur["rates"].append(rate)
    return stages


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("token")
    ap.add_argument("--root", default=os.path.join(
        os.environ.get("APPDATA", ""), "MaaRacingMaster"))
    args = ap.parse_args()
    debug = os.path.join(args.root, "debug", args.token, "speedrush", "drive")
    trace = os.path.join(args.root, "logs", args.token, "trace",
                         "speedrush.jsonl")
    stages = _trace_stages(trace) if os.path.exists(trace) else []
    print(f"== {args.token}")
    print("阶段        超车 变道 木桶 | 分速均值 ≥120档  样本")
    total = collections.Counter()
    for sd in sorted(glob.glob(os.path.join(debug, "*"))):
        hud = os.path.join(sd, "hud", "hud.jsonl")
        if not os.path.exists(hud):
            continue
        ev, t0, t1 = _banner_events(hud)
        total.update(ev)
        st = next((s for s in stages
                   if s.get("t1") and s["t0"] <= (t0 + t1) // 2 <= s["t1"]),
                  None)
        rates = st["rates"] if st else []
        name = os.path.basename(sd.rstrip("/"))[-8:]
        if rates:
            hi = 100 * sum(1 for x in rates if x >= 120) / len(rates)
            print(f"{name:>8}  {ev['极限超车']:>4} {ev['极限变道']:>4} "
                  f"{ev['木桶旋转']:>4} | {sum(rates)/len(rates):>7.0f} "
                  f"{hi:>5.0f}%  {len(rates):>4}")
        else:
            print(f"{name:>8}  {ev['极限超车']:>4} {ev['极限变道']:>4} "
                  f"{ev['木桶旋转']:>4} |      --     --     -")
    print("合计", "超车", total["极限超车"], "变道", total["极限变道"],
          "木桶", total["木桶旋转"])


if __name__ == "__main__":
    main()