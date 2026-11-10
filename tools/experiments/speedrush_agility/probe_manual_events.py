"""手动局 120 分事件对齐影子决策（speedrush）。

**问题**：人工局每回合约 24 次极限超车，程序只有 3~6 次。是程序贴不到带，
还是它在过车前没选那条线？

**口径**：取手动录制「录制采集 / 人工驾驶」两阶段（`debug/speedrush/*_manual_p*`），
按游戏日志的 ``dispatchPlayerCarScore: type=..., score=120`` 时间戳作为判定拍，
对到同一会话 trace 的最近逐拍记录；同时取前 1.2 s 的起意拍。

- 判定拍：游戏派 120 分的那一刻。
- 起意拍：判定拍 −1.2 s（RULES §4.2.1 的判定窗，执行层 0.68 道约 1.0 s + 反应/余量）。
- 自车横位：`scene.ego_m`，换算成距最近车道中心的偏差（车道宽 3.5 m，
  中心 ±1.75 / ±5.25 m）。
- 影子决策：`sel_why`、`x_target`、`plan.x`；`plan.x` 与自车当前位的差是
  「程序是否在跟踪当前线」，与判定位之差是「是否提前起那一下横移」。

用法::

    .venv/Scripts/python.exe -X utf8 probe_manual_events.py \
        --session 094822 --session 103934 --out manual_events.csv

游戏日志路径取 ``MAAR_G112_LOG``（本机私有路径不入库），缺省即报错。
"""

from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import json
import os
import re
import statistics
from pathlib import Path

LANE_W = 3.5
CENTERS = [1.75 + LANE_W * k for k in range(-5, 6)]
TS_RE = re.compile(
    r"\[(\d{4}\.\d{2}\.\d{2})-(\d{2}\.\d{2}\.\d{2}):(\d{3})\]")
EV_RE = re.compile(r"dispatchPlayerCarScore: type=(\w+), score=(\d+)")


def lane_off(x_m: float | None) -> float | None:
    if x_m is None:
        return None
    c = min(CENTERS, key=lambda v: abs(x_m - v))
    return x_m - c


def parse_events(log_path: str, t0: dt.datetime,
                 t1: dt.datetime) -> list[tuple[dt.datetime, str]]:
    out: list[tuple[dt.datetime, str]] = []
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = TS_RE.search(line)
            e = EV_RE.search(line)
            if not (m and e) or e.group(2) != "120":
                continue
            stamp = dt.datetime.strptime(
                m.group(1) + " " + m.group(2) + "." + m.group(3),
                "%Y.%m.%d %H.%M.%S.%f")
            if t0 <= stamp <= t1:
                out.append((stamp, e.group(1)))
    return out


def load_trace(path: str) -> list[dict]:
    ticks: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") is None and rec.get("fid") is not None:
                ticks.append(rec)
    ticks.sort(key=lambda r: r["ts_wall_ns"])
    return ticks


def nearest(trace: list[dict], epoch_ns: float) -> dict | None:
    keys = [r["ts_wall_ns"] for r in trace]
    i = bisect.bisect_left(keys, epoch_ns)
    cand = [trace[j] for j in (i - 1, i, i + 1) if 0 <= j < len(trace)]
    if not cand:
        return None
    cand.sort(key=lambda r: abs(r["ts_wall_ns"] - epoch_ns))
    for rec in cand:
        if rec.get("scene"):
            return rec
    return cand[0]


def window(trace: list[dict], a: float, b: float) -> list[dict]:
    keys = [r["ts_wall_ns"] for r in trace]
    return trace[bisect.bisect_left(keys, a):bisect.bisect_right(keys, b)]


def frame_files(stage_dir: Path) -> dict[int, str]:
    out: dict[int, str] = {}
    path = stage_dir / "frames.jsonl"
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            out[int(rec["frame_id"])] = rec.get("file") or ""
    return out


def manual_stages(root: Path, sessions: set[str] | None) -> list[dict]:
    stages: list[dict] = []
    for meta_path in sorted(root.glob("debug/speedrush/*_manual_p*/meta.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        token = meta.get("session")
        if not token:
            continue
        if sessions and not any(token == s or token.endswith(s)
                               for s in sessions):
            continue
        start = dt.datetime.fromisoformat(meta["started_at"])
        end = dt.datetime.fromisoformat(meta["ended_at"])
        stages.append({
            "session": token,
            "phase": meta.get("phase"),
            "dir": meta_path.parent,
            "start": start,
            "end": end,
        })
    return stages


def summarize(rows: list[dict]) -> None:
    ov = [r for r in rows if r["type"] == "overtake"]
    if not ov:
        return
    offs = [abs(r["ego_off"]) for r in ov if r["ego_off"] is not None]
    offs12 = [abs(r["ego_off_12"]) for r in ov if r["ego_off_12"] is not None]
    deltas = [abs(r["ego_off"] - r["ego_off_12"]) for r in ov
              if r["ego_off"] is not None and r["ego_off_12"] is not None]
    track = [abs(r["plan_12"] - r["ego_12"]) for r in ov
             if r["plan_12"] is not None and r["ego_12"] is not None]
    ahead = [abs(r["plan_12"] - r["ego_m"]) for r in ov
             if r["plan_12"] is not None and r["ego_m"] is not None]

    def counts(key: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in ov:
            v = r[key] or "none"
            out[v] = out.get(v, 0) + 1
        return out

    print(f"\n== overtake 120 events: {len(ov)}")
    print(f"ego |off| @judge  P50={statistics.median(offs):.2f}m "
          f"P90={sorted(offs)[int(.9 * len(offs)) - 1]:.2f}m "
          f">0.85m={sum(v > .85 for v in offs) / len(offs):.0%}")
    print(f"ego |off| @-1.2s P50={statistics.median(offs12):.2f}m "
          f"|move| P50={statistics.median(deltas):.2f}m")
    print(f"plan@-1.2 tracks current: |plan-ego12| P50="
          f"{statistics.median(track):.2f}m "
          f"(<0.5m {sum(v < .5 for v in track) / len(track):.0%})")
    print(f"plan@-1.2 anticipates judge: |plan-egoJ| P50="
          f"{statistics.median(ahead):.2f}m "
          f"(<0.7m {sum(v < .7 for v in ahead) / len(ahead):.0%})")
    print("sel @judge:", counts("sel"))
    print("sel @-1.2 :", counts("sel_12"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.join(
        os.environ.get("APPDATA", ""), "MaaRacingMaster"))
    ap.add_argument("--log", default=os.environ.get("MAAR_G112_LOG"))
    ap.add_argument("--session", action="append",
                    help="只跑指定会话；可重复")
    ap.add_argument("--out", help="CSV 输出路径")
    args = ap.parse_args()
    if not args.log:
        raise SystemExit("缺少游戏日志：设 MAAR_G112_LOG 或传 --log")
    root = Path(args.root)
    sessions = set(args.session) if args.session else None
    stages = manual_stages(root, sessions)
    if not stages:
        raise SystemExit("没找到手动录制 meta.json")

    traces: dict[str, list[dict]] = {}
    rows: list[dict] = []
    for st in stages:
        token = st["session"]
        trace_path = root / "logs" / token / "trace" / "speedrush.jsonl"
        if not trace_path.exists():
            print(f"[skip] {token}: 缺 trace {trace_path}")
            continue
        if token not in traces:
            traces[token] = load_trace(str(trace_path))
        trace = traces[token]
        frames = frame_files(st["dir"])
        events = parse_events(args.log, st["start"], st["end"])
        for stamp, kind in events:
            epoch = stamp.timestamp() * 1e9
            cur = nearest(trace, epoch)
            early = nearest(trace, epoch - 1.2e9)
            if cur is None or early is None:
                continue
            ego = (cur.get("scene") or {}).get("ego_m")
            ego12 = (early.get("scene") or {}).get("ego_m")
            plan = cur.get("plan") if isinstance(cur.get("plan"), dict) else {}
            plan12 = early.get("plan") if isinstance(early.get("plan"), dict) else {}
            plan_x = plan.get("x")
            plan_x12 = plan12.get("x")
            seg = window(trace, epoch - 1.2e9, epoch)
            steers = [abs(r.get("steer_raw") or 0.0) for r in seg]
            vlats = [abs(r.get("v_lat") or 0.0) for r in seg]
            dbg = early.get("sel_dbg") or {}
            cars = dbg.get("car") or []
            rows.append({
                "session": token,
                "phase": st["phase"],
                "time": stamp.strftime("%H:%M:%S.%f")[:-3],
                "type": kind,
                "fid": cur.get("fid"),
                "frame": frames.get(int(cur.get("fid") or -1), ""),
                "ego_m": ego,
                "ego_off": lane_off(ego),
                "ego_12": ego12,
                "ego_off_12": lane_off(ego12),
                "sel": cur.get("sel_why"),
                "sel_12": early.get("sel_why"),
                "xt": cur.get("x_target"),
                "plan": plan_x,
                "plan_12": plan_x12,
                "steer_max": max(steers) if steers else None,
                "vlat_max": max(vlats) if vlats else None,
                "cars_x_t": json.dumps(
                    [[round(c[0], 2), c[1]] for c in cars[:6]],
                    ensure_ascii=False) if cars else "",
            })

    rows.sort(key=lambda r: (r["session"], r["time"]))
    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"wrote {len(rows)} rows -> {args.out}")
    for row in rows[:10]:
        print(f"{row['time']} {row['type']:10} ego={row['ego_m']} "
              f"off={row['ego_off']} sel12={row['sel_12']} "
              f"plan12={row['plan_12']}")
    summarize(rows)


if __name__ == "__main__":
    main()
