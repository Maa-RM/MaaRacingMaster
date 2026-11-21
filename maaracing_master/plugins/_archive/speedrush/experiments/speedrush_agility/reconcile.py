"""对账：已保存报告 vs 现场复算，以及方案里的数字锚点。

两种用法可同时给：

1. `--trace/--pads-dir`：用同一实现重算一遍，逐项比对存档报告，
   打印「路径 / 存档值 / 复算值 / 是否一致」。口径或输入指纹不同直接判
   「不可直接比较」，不逐项比对。
2. `--plan`：扫描方案里的 `{{R:路径}}` 锚点，解析成报告里的实际值。
   锚点写错、路径不存在一律失败——方案只能引用，不能自己存数。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ANCHOR = re.compile(r"\{\{R:([^}]+)\}\}")
MAX_PRINT = 40


def leaves(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from leaves(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from leaves(v, f"{prefix}[{i}]")
    else:
        yield prefix, obj


def resolve(doc, path):
    """按 `a.b[0].c` 或 `a["k.k"]` 解析；任何一段不存在就抛 KeyError。

    含点的键（如 θ 取值 `"0.1"`）必须用引号下标，不能写成 `by_theta.0.1`。
    """
    cur = doc
    for name, quoted, idx in re.findall(r'([^.\[\]"]+)|\["([^"]+)"\]|\[(\d+)\]', path):
        if idx:
            cur = cur[int(idx)]
        elif quoted:
            cur = cur[quoted]
        else:
            cur = cur[name]
    return cur


def recompute(args):
    cmd = [sys.executable, os.path.join(HERE, "recompute_report.py"),
           "--trace", args.trace, "--emit-json"]
    if args.pads_dir:
        cmd += ["--pads-dir", args.pads_dir]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        raise SystemExit("复算失败：\n" + out.stderr[-2000:])
    return json.loads(out.stdout)


def compare(saved, fresh):
    keys = ("caliber_version", "input", "params", "manual_inputs",
            "implementation_sha256_16", "mode_assignment")
    for k in keys:
        if saved.get(k) != fresh.get(k):
            print(f"不可直接比较：`{k}` 不同\n  存档={saved.get(k)}\n  复算={fresh.get(k)}")
            return 2
    old = dict(leaves(saved))
    new = dict(leaves(fresh))
    diffs = [(p, old[p], new[p]) for p in old.keys() & new.keys() if old[p] != new[p]]
    only_old = old.keys() - new.keys()
    only_new = new.keys() - old.keys()
    print(f"逐项对账：存档 {len(old)} 项 / 复算 {len(new)} 项 / "
          f"不一致 {len(diffs)} 项 / 仅存档有 {len(only_old)} / 仅复算有 {len(only_new)}")
    for p, a, b in diffs[:MAX_PRINT]:
        print(f"  ✗ {p}: 存档={a!r} 复算={b!r}")
    for p in list(only_old)[:MAX_PRINT]:
        print(f"  ✗ {p}: 仅存档有（{old[p]!r}）")
    for p in list(only_new)[:MAX_PRINT]:
        print(f"  ✗ {p}: 仅复算有（{new[p]!r}）")
    return 0 if not (diffs or only_old or only_new) else 1


def check_plan(plan_path, report, strict=False):
    text = open(plan_path, encoding="utf-8").read()
    hits = []
    bare = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for path in ANCHOR.findall(line):
            try:
                val = resolve(report, path)
                hits.append((lineno, path, val, True))
            except (KeyError, IndexError, TypeError):
                hits.append((lineno, path, None, False))
        if strict:
            stripped = ANCHOR.sub("", line)
            for num in re.findall(r"\d+\.\d+", stripped):
                bare.append((lineno, num, line.strip()[:60]))
    bad = [h for h in hits if not h[3]]
    print(f"方案锚点：{len(hits)} 处 / 解析失败 {len(bad)} 处")
    for lineno, path, val, ok in hits[:MAX_PRINT]:
        print(f"  {'✓' if ok else '✗'} L{lineno} [{path}] = {val!r}")
    if strict:
        print(f"裸小数：{len(bare)} 处（只允许通过 {{R:...}} 引用）")
        for lineno, num, ctx in bare[:MAX_PRINT]:
            print(f"  ✗ L{lineno} {num} | {ctx}")
    return 0 if not bad and not (strict and bare) else 1


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--saved", default=os.path.join(HERE, "RECOMPUTE_REPORT.json"))
    ap.add_argument("--trace")
    ap.add_argument("--pads-dir", default="")
    ap.add_argument("--plan")
    ap.add_argument("--plan-strict", action="store_true",
                    help="同时拒绝方案里的裸小数（必须走 {{R:...}} 锚点）")
    ap.add_argument("--selftest", action="store_true",
                    help="自检：把存档里一个数字改错，确认对账会报不一致")
    args = ap.parse_args()

    saved = json.load(open(args.saved, encoding="utf-8"))
    if args.selftest:
        for path, val in leaves(saved):
            if isinstance(val, (int, float)) and not isinstance(val, bool) \
                    and not path.startswith("params."):
                holder = saved
                parts = [p for p in re.findall(r"([^.\[\]]+)|\[(\d+)\]", path)]
                for name, idx in parts[:-1]:
                    holder = holder[int(idx)] if idx else holder[name]
                last = parts[-1]
                holder[int(last[1]) if last[1] else last[0]] = val + 1
                print(f"自检：人为把 {path} 改成 {val + 1}（原 {val}）")
                break
    rc = 0
    if args.trace:
        rc |= compare(saved, recompute(args))
    if args.plan:
        rc |= check_plan(args.plan, saved, args.plan_strict)
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
