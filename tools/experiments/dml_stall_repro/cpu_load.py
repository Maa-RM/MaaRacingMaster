"""可控背景 CPU 争抢源：把复发窗观测到的背景占用补成实验变量。

2026-11-07 22:23 离线复发窗：系统 CPU 98% 而本进程仅 312% → 背景约 8.6 核；
2026-11-08 四轮有效阴性背景仅约 2.3 核。争抢是否为必要条件未验，本脚本把该
变量补到复发窗强度再跑对照。每 worker 一个独立进程纯自旋（绕开 GIL），亲和性
绑到与 OCR 绑核同一组 P-core，让争抢落在业务线程真正跑的核上。

用法（仓库根）：
  .venv/Scripts/python.exe tools/experiments/dml_stall_repro/cpu_load.py --workers 6
父进程被杀时子进程自检退出（Windows 杀父不连带子进程）。
"""
from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import time

import psutil


def _spin(parent_pid: int, cores: list[int]) -> None:
    psutil.Process().cpu_affinity(cores)
    while psutil.pid_exists(parent_pid):
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 2.0:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workers", type=int, default=6, help="自旋进程数（≈占用核数）")
    ap.add_argument("--cores", default="0-7", help="亲和核范围，缺省与 OCR 绑核同组 P-core")
    args = ap.parse_args()
    lo, _, hi = args.cores.partition("-")
    cores = list(range(int(lo), int(hi or lo) + 1))
    ctx = mp.get_context("spawn")
    kids = [ctx.Process(target=_spin, args=(os.getpid(), cores), daemon=True)
            for _ in range(args.workers)]
    for k in kids:
        k.start()
    print(f"背景争抢源：{len(kids)} worker 绑核 {cores}，父 pid {os.getpid()}", flush=True)
    try:
        for k in kids:
            k.join()
    except KeyboardInterrupt:
        pass
    finally:
        for k in kids:
            k.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())