"""Class C 车辆规格可读面清点（speedrush 敏捷性 G 线）。

**问什么**：玩家自车是 Class C 时，Lua 侧能从数据表里读到哪些车辆规格，
以及这些规格的覆盖度（有多少车缺字段）、量级范围是多少。

口径：

- 信源一 `car_dat.lua` 的 `cars.Content`：Class、CarLength、Layout、
  InitialPerformance/FullPerformance、Year 等；单位除 CarLength（cm）外为表内约定值。
- 信源二 `car_manual_dat.lua` 的 `Manual.Content`：WheelBase / TopSpeed /
  HundredKmAcceleration / Weight / Gear 等，均为**展示字符串**（"2630 mm"、"200 km/h"），
  不是引擎真源；这里只统计覆盖度与量级，不当物理常量使用。
- 两者都只是 Lua 可读的派生数据；引擎侧的 CTF 与 Specs 不在此脚本可达范围。

用法::

    .venv/Scripts/python.exe -X utf8 probe_g2_classc_specs.py
    .venv/Scripts/python.exe -X utf8 probe_g2_classc_specs.py --src <luasrc 根>

`--src` 缺省读环境变量 `MAAR_LUASRC`；两者都没有时报错退出（不在仓库里写本机路径）。
"""

from __future__ import annotations

import argparse
import os
import re
import statistics
import sys

CAR_DAT = os.path.join("common", "data", "car_dat.lua")
MANUAL_DAT = os.path.join("common", "data", "car_manual_dat.lua")


def _load(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def parse_cars(text: str) -> dict[int, dict[str, str]]:
    """抽出 cars.Content 下每辆车的 Class / CarLength / Layout。"""
    # cars 段起点到 misc 段（下一个顶层键）之间
    start = text.index("\n  cars = {")
    end = text.index("\n  misc = {")
    body = text[start:end]
    out: dict[int, dict[str, str]] = {}
    cur = None
    for line in body.splitlines():
        m = re.match(r"\s*\[(\d+)\] = \{", line)
        if m:
            cur = int(m.group(1))
            out[cur] = {}
            continue
        if cur is None:
            continue
        m = re.match(r'\s*(Class|CarLength|Layout) = "?([^",]+)"?,?', line)
        if m:
            out[cur][m.group(1)] = m.group(2).strip()
        if re.match(r"\s*\},?\s*$", line) and "Class" in out[cur]:
            cur = None
    return out


def parse_manual(text: str) -> dict[int, dict[str, str]]:
    """抽出 Manual.Content 下每辆车的展示字段（字符串原样）。"""
    start = text.index("\n  Manual = {")
    body = text[start:]
    out: dict[int, dict[str, str]] = {}
    cur = None
    for line in body.splitlines():
        m = re.match(r"\s*\[(\d+)\] = \{", line)
        if m:
            cur = int(m.group(1))
            out[cur] = {}
            continue
        if cur is None:
            continue
        m = re.match(r'\s*(\w+) = "([^"]*)",?', line)
        if m:
            out[cur][m.group(1)] = m.group(2)
        else:
            m = re.match(r"\s*(\w+) = (-?\d+(?:\.\d+)?),?$", line)
            if m:
                out[cur][m.group(1)] = m.group(2)
        if re.match(r"\s*\},?\s*$", line) and out[cur]:
            cur = None
    return out


def _num(s: str | None) -> float | None:
    if not s:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def _stat(vals: list[float]) -> str:
    if not vals:
        return "n=0"
    vals = sorted(vals)
    return (
        f"n={len(vals)} min={vals[0]:g} p50={statistics.median(vals):g} max={vals[-1]:g}"
    )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.environ.get("MAAR_LUASRC"))
    args = ap.parse_args(argv)
    if not args.src:
        print("缺少 --src，也没有 MAAR_LUASRC 环境变量", file=sys.stderr)
        return 2

    cars = parse_cars(_load(os.path.join(args.src, CAR_DAT)))
    manual = parse_manual(_load(os.path.join(args.src, MANUAL_DAT)))

    by_class: dict[str, list[int]] = {}
    for cid, row in cars.items():
        by_class.setdefault(row.get("Class", "?"), []).append(cid)

    print("== car_dat.cars 覆盖 ==")
    for k in sorted(by_class):
        print(f"  Class {k}: {len(by_class[k])} cars")
    cls_c = sorted(by_class.get("C", []))
    print(f"  Class C 区间: {cls_c[0]}..{cls_c[-1]}" if cls_c else "  无 Class C")

    print("\n== Class C：car_manual_dat 展示字段覆盖与量级 ==")
    for field, unit, scale in (
        ("WheelBase", "mm", 1.0),
        ("TopSpeed", "km/h", 1.0),
        ("HundredKmAcceleration", "s", 1.0),
        ("Length", "mm", 1.0),
        ("Width", "mm", 1.0),
        ("Height", "mm", 1.0),
        ("FrontTreadWheel", "mm", 1.0),
        ("RearTreadWheel", "mm", 1.0),
        ("Weight", "kg", 1.0),
        ("Gear", "count", 1.0),
    ):
        vals, have = [], 0
        for cid in cls_c:
            raw = manual.get(cid, {}).get(field)
            if raw and not raw.startswith("T_"):
                v = _num(raw)
                if v is not None:
                    vals.append(v * scale)
                    have += 1
        print(f"  {field:<22} 有值 {have}/{len(cls_c)}  {_stat(vals)} [{unit}]")

    print("\n== 全表覆盖对照（所有 Class）==")
    for field in ("WheelBase", "TopSpeed", "Weight", "Gear"):
        have = sum(
            1
            for cid, row in manual.items()
            if row.get(field) and not row[field].startswith("T_")
        )
        print(f"  {field:<10} 有值 {have}/{len(manual)}")

    print("\n== 样本（前 8 辆 Class C）==")
    for cid in cls_c[:8]:
        print(
            f"  {cid}: Class={cars[cid].get('Class')} Len={cars[cid].get('CarLength')} "
            f"Layout={cars[cid].get('Layout')} "
            f"WB={manual.get(cid, {}).get('WheelBase')} "
            f"Vmax={manual.get(cid, {}).get('TopSpeed')} "
            f"0-100={manual.get(cid, {}).get('HundredKmAcceleration')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
