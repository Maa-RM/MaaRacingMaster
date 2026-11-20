"""I 线：运动组（Class C）玩家车的可套用规格。

只读；从客户端数据表（解包后的 lua 数据目录）读出
  - `inf_drive_stage_dat`  RoundCfg：回合 -> 车辆组别
  - `car_dat`             cars：车码 -> 组别 / 车身长（cm）
  - `car_manual_dat`      Manual：车码 -> 轴距 / 长宽高 / 轮距 / 手册极速
  - `tune_refit_dat`      CarPt：车码 -> 调校面板数值（限速 / 质量 / 横向G / 加速 / 制动 …）
并汇总 Class C 分布、按限速映射模式上限、单台车明细。

数据根不写死：`--data-root` 指向解包出的 `.../luasrc/common/data` 目录。

    python maaracing_master/plugins/_archive/speedrush/experiments/speedrush_agility/probe_i_class_c_spec.py --data-root <dir>
    python maaracing_master/plugins/_archive/speedrush/experiments/speedrush_agility/probe_i_class_c_spec.py --data-root <dir> --car 1443
"""
from __future__ import annotations

import argparse
import re
import statistics
from pathlib import Path

# 调校面板 DataItems 里可读出的派生项（名字 -> (pt 索引, 定点缩放, 单位)）
PT_FIELDS = {
    1: ("限速", 1000, "km/h"),
    2: ("0-100km/h", 1000, "s"),
    3: ("100-200km/h", 1000, "s"),
    4: ("100-0 制动", 1000, "m"),
    20: ("60-160km/h", 1000, "s"),
    44: ("质量", 1000, "kg"),
    46: ("横向G", 1000, "g"),
    69: ("马力", 1000, "hp"),
    304: ("漂移40m 车速", 1000, "km/h"),
    305: ("抓地40m 车速", 1000, "km/h"),
}
PT_KEYS = ("DirMax", "Max", "Min", "Origin", "ProMax")


def _blocks(text: str, head: str, tail: str | None = None, indent: int = 6):
    """截取一个表段，按 `[<id>] = {` 切块。"""
    seg = text.split(head, 1)[1]
    if tail:
        seg = seg.split(tail, 1)[0]
    parts = re.split(r"\n%s\[(\d+)\] = \{\n" % (" " * indent), seg)
    return {int(parts[i]): parts[i + 1] for i in range(1, len(parts), 2)}


def _field(body: str, key: str) -> str | None:
    m = re.search(r"(?:\n|^)        %s = (.+?),?\n" % key, body)
    return m.group(1).strip().rstrip(",") if m else None


def _num(raw: str | None) -> float | None:
    if not raw:
        return None
    m = re.match(r'"([\d.]+)', raw)
    return float(m.group(1)) if m else None


def parse_car_dat(text: str) -> dict[int, dict]:
    out = {}
    for cid, body in _blocks(text, "  cars = {", "  misc = {").items():
        out[cid] = {
            "class": (_field(body, "Class") or "").strip('"') or None,
            "car_length_cm": _num(_field(body, "CarLength")),
            "model": (_field(body, "ModelName") or "").strip('"'),
            "brand": (_field(body, "ManufacturerId") or "").strip('"'),
        }
    return out


def parse_manual(text: str) -> dict[int, dict]:
    out = {}
    for cid, body in _blocks(text, "  Manual = {").items():
        out[cid] = {
            k: _num(_field(body, k))
            for k in ("WheelBase", "Length", "Width", "Height", "TopSpeed", "FrontTreadWheel", "RearTreadWheel")
        }
        out[cid]["weight_kg"] = _num(_field(body, "Weight"))
    return out


def parse_carpt(text: str) -> dict[int, dict]:
    out = {}
    for cid, body in _blocks(text, "  CarPt = {", "  CarScoreNew = {").items():
        pts: dict[int, dict] = {}
        for m in re.finditer(r"\n          \[(\d+)\] = \{\n(.*?)\n          \}", body, re.S):
            vals = {k: float(v) for k, v in re.findall(r"\n            (%s) = ([\-\d.]+)" % "|".join(PT_KEYS), m.group(2))}
            pts[int(m.group(1))] = vals
        out[cid] = pts
    return out


def parse_round_cfg(text: str) -> dict[int, str]:
    out = {}
    for rid, body in _blocks(text, "  RoundCfg = {").items():
        m = re.search(r'(?:\n|^)        CarGroup = "(\w)"', body)
        out[rid] = m.group(1) if m else "?"
    return out


def mode_speed_cap_kmh(car_limit_kmh: float) -> float:
    """模式限速助手把车身限速 170-260 线性映射到 120-200，Lua 侧再减 2。"""
    clamped = min(max(car_limit_kmh, 170.0), 260.0)
    return 120.0 + (clamped - 170.0) / 90.0 * 80.0 - 2.0


def _summary(name: str, values: list[float]) -> str:
    if not values:
        return f"{name}: 无数据"
    q = statistics.quantiles(values, n=4)
    return (f"{name}: n={len(values)} min={min(values):.1f} q25={q[0]:.1f} "
            f"med={statistics.median(values):.1f} q75={q[2]:.1f} max={max(values):.1f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", required=True, help="解包数据目录（含 car_dat.lua 等）")
    ap.add_argument("--car", type=int, default=1443, help="要单列明细的车码（默认 1443）")
    args = ap.parse_args()
    root = Path(args.data_root)
    read = lambda name: (root / name).read_text(encoding="utf-8", errors="replace")  # noqa: E731

    cars = parse_car_dat(read("car_dat.lua"))
    manual = parse_manual(read("car_manual_dat.lua"))
    carpt = parse_carpt(read("tune_refit_dat.lua"))
    rounds = parse_round_cfg(read("inf_drive_stage_dat.lua"))

    print("== 回合 -> 组别 ==")
    print("  " + ", ".join(f"{r}:{g}" for r, g in sorted(rounds.items())))

    print("\n== 车码组别分布 ==")
    dist: dict[str, int] = {}
    for v in cars.values():
        dist[v["class"]] = dist.get(v["class"], 0) + 1
    print("  " + ", ".join(f"{k}={v}" for k, v in sorted(dist.items(), key=lambda x: str(x[0]))))

    cls = [cid for cid, v in cars.items() if v["class"] == "C"]
    print(f"\n== Class C 玩家车规格（{len(cls)} 台，其中 {sum(1 for c in cls if c in carpt)} 台有调校面板数据） ==")
    hdr = f"{'车码':>5} {'轴距':>5} {'车长':>5} {'车宽':>5} {'车高':>5} {'手册极速':>7} {'限速原厂':>7} {'限速满改':>7} {'质量':>5} {'横向G':>5} {'型号'}"
    print(hdr)

    def cell(value, width, fmt="{:.0f}"):
        return f"{'—':>{width}}" if value is None else f"{fmt.format(value):>{width}}"

    for cid in sorted(cls):
        m, p = manual.get(cid, {}), carpt.get(cid, {})
        lim_o = p.get(1, {}).get("Origin")
        lim_m = p.get(1, {}).get("Max")
        mass = p.get(44, {}).get("Origin")
        latg = p.get(46, {}).get("Origin")
        print(f"{cid:>5} {cell(m.get('WheelBase'), 5)} {cell(m.get('Length'), 5)} "
              f"{cell(m.get('Width'), 5)} {cell(m.get('Height'), 5)} {cell(m.get('TopSpeed'), 7)} "
              f"{cell(None if lim_o is None else lim_o / 1000, 7, '{:.1f}')} "
              f"{cell(None if lim_m is None else lim_m / 1000, 7, '{:.1f}')} "
              f"{cell(None if mass is None else mass / 1000, 5)} "
              f"{cell(None if latg is None else latg / 1000, 5, '{:.2f}')} "
              f"{cars[cid]['model']}")

    print("\n== Class C 分布（原厂 / 满改 两档） ==")
    for key, label in (("Origin", "原厂"), ("Max", "满改")):
        for pidx, (fname, scale, unit) in PT_FIELDS.items():
            vals = [carpt[c][pidx][key] / scale for c in cls if pidx in carpt.get(c, {}) and key in carpt[c][pidx]]
            if vals:
                print("  " + _summary(f"{fname}({label},{unit})", vals))
    for fname in ("WheelBase", "Length", "Width", "Height"):
        vals = [manual[c][fname] for c in cls if c in manual and manual[c][fname]]
        print("  " + _summary(f"{fname}(mm)", vals))

    print("\n== 由限速推模式上限（170-260 -> 120-200，减 2） ==")
    for key, label in (("Origin", "原厂"), ("Max", "满改")):
        caps = [mode_speed_cap_kmh(carpt[c][1][key] / 1000) for c in cls if 1 in carpt.get(c, {}) and key in carpt[c][1]]
        print("  " + _summary(f"上限 km/h（{label}）", caps))
        print("  " + _summary(f"上限 m/s（{label}）", [c / 3.6 for c in caps]))

    print(f"\n== 车码 {args.car} 明细 ==")
    m, p = manual.get(args.car, {}), carpt.get(args.car, {})
    print(f"  组别={cars.get(args.car, {}).get('class')} 型号={cars.get(args.car, {}).get('model')}")
    print("  " + " ".join(f"{k}={m.get(k)}" for k in ("WheelBase", "Length", "Width", "Height",
                                                     "FrontTreadWheel", "RearTreadWheel", "TopSpeed", "weight_kg")))
    for pidx, (fname, scale, unit) in PT_FIELDS.items():
        if pidx in p:
            vals = p[pidx]
            print(f"  {fname:>12s}({unit}): 原厂={vals.get('Origin', float('nan')) / scale:.2f} "
                  f"满改={vals.get('Max', float('nan')) / scale:.2f} 定向满改={vals.get('ProMax', float('nan')) / scale:.2f}")
    if 1 in p:
        for key in ("Origin", "Max"):
            cap = mode_speed_cap_kmh(p[1][key] / 1000)
            print(f"  由限速 {p[1][key] / 1000:.0f} km/h（{key}）推模式上限 = {cap:.1f} km/h = {cap / 3.6:.1f} m/s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
