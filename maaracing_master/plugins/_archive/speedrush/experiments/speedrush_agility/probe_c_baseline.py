"""C 线：基线对齐速查（回放 vs 实录下发杆值）。

只读；用于在解释任何反事实之前量化回放与实录的偏差。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[5]))
sys.path.insert(0, str(HERE))

from maaracing_master.plugins._archive.speedrush.config import load_decision
from probe_steering_replay import replay


def load_rows(path: Path):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [r for r in rows if r.get("lat_ctrl")]


def groups(rows):
    out = []
    for row in rows:
        if (not out or row["stage_id"] != out[-1][-1]["stage_id"]
                or row["seq"] <= out[-1][-1]["seq"]):
            out.append([])
        out[-1].append(row)
    return out


def main():
    params = load_decision().planner
    for trace in sys.argv[1:]:
        rows = load_rows(Path(trace))
        for i, seg in enumerate(groups(rows), 1):
            actual = np.array([r["steer_x"] / 32767 for r in seg])
            base = replay(seg, params)
            err = base - actual
            print(json.dumps({
                "trace": str(trace), "segment": i,
                "stage": seg[0]["stage_id"], "ticks": len(seg),
                "mean_abs_err": round(float(np.mean(abs(err))), 6),
                "max_abs_err": round(float(np.max(abs(err))), 6),
                "ticks_gt_1lsb": int(np.sum(abs(err) > 1.0 / 32767 + 1e-9)),
                "ticks_gt_0.05": int(np.sum(abs(err) > 0.05)),
                "sign_flip_ticks": int(np.sum(base * actual < 0)),
                "corr": round(float(np.corrcoef(base, actual)[0, 1]), 6),
            }, ensure_ascii=False))


if __name__ == "__main__":
    main()
