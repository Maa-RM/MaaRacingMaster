#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动器只读侦查：有没有官方静默/无窗启动参数。

启动器是 PyInstaller 打包的 Python 应用，命令行参数处理住在其主脚本（CArchive 里
type='s' 的条目）中。本探针做三件事，全程只读、不改启动器任何文件：
1. 解析 CArchive 目录（PyInstaller 归档格式是公开稳定的），列出条目；
2. 解压主脚本字节码，落盘 _out/ 备查；
3. 从字节码里抽取可读字符串，找 argv 处理与疑似静默参数的字面量。

不反编译、不重打包、不抽函数外跑——后两者才是版本偏移风险的来源。
用法（仓库根）：
    .venv/Scripts/python.exe tools/experiments/launcher_silent_check/probe_inspect.py
"""

from __future__ import annotations

import re
import struct
import sys
import zlib
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))

from maaracing_master.core import game_process  # noqa: E402  读取面复用：卸载表反查

_MAGIC = b"MEI\014\013\012\013\016"
_COOKIE21 = struct.Struct("!8sIIII64s")  # PyInstaller 2.1+：magic, pkgLen, toc, tocLen, pyvers, pylib
_COOKIE20 = struct.Struct("!8sIIII")     # 2.0：无 pylib 名
_ENTRY = struct.Struct("!iIIIBc")        # entrySize, pos, csize, usize, cmprsFlag, type

_INTERESTING = (
    "argv", "argparse", "getopt", "add_argument",
    "--", "-s", "-q", "-h",
    "silent", "hide", "minimi", "tray", "background", "headless",
    "b_hide", "b_background", "b_boot_launch", "boot_launch",
    "start_game", "startgame", "check_update", "update",
)

_PRINTABLE = re.compile(rb"[\x20-\x7e]{4,}")


def _find_cookie(data: bytes) -> tuple[int, tuple] | None:
    """从文件尾部向前找 CArchive cookie，返回 (cookiePos, 解析后的字段)。"""
    # cookie 之后可能还挂着数字签名等尾部数据（本机启动器尾部约 10KB），
    # 不能只扫文件尾部固定窗口——全文件扫描、取最后一个命中。
    pos = data.rfind(_MAGIC)
    if pos == -1:
        return None
    try:
        magic, pkg_len, toc, toc_len, _pyvers, _pylib = _COOKIE21.unpack_from(data, pos)
        if magic == _MAGIC and 0 < toc < pkg_len and 0 < toc_len < pkg_len:
            return pos, ("21", pkg_len, toc, toc_len)
    except struct.error:
        pass
    try:
        magic, pkg_len, toc, toc_len, _pyvers = _COOKIE20.unpack_from(data, pos)
        if magic == _MAGIC and 0 < toc < pkg_len and 0 < toc_len < pkg_len:
            return pos, ("20", pkg_len, toc, toc_len)
    except struct.error:
        pass
    return None


def _parse_toc(data: bytes, cookie: tuple) -> list[dict]:
    """解析 CArchive TOC：每个条目 = 头 + 变长名字。"""
    _ver, pkg_len, toc, toc_len = cookie
    pos, field = _find_cookie(data)[0], cookie[2]
    cookie_size = _COOKIE21.size if _ver == "21" else _COOKIE20.size
    archive_start = pos + cookie_size - pkg_len
    toc_pos = archive_start + field
    out: list[dict] = []
    p = toc_pos
    end = toc_pos + toc_len
    while p + _ENTRY.size <= end:
        entry_size, entry_pos, csize, usize, cmprs, ctype = _ENTRY.unpack_from(data, p)
        name = data[p + _ENTRY.size: p + entry_size].rstrip(b"\x00").decode("utf-8", "replace")
        raw = data[archive_start + entry_pos: archive_start + entry_pos + csize]
        out.append({"name": name, "type": ctype.decode(), "cmprs": bool(cmprs),
                    "csize": csize, "usize": usize, "raw": raw})
        p += entry_size
    return out


def _dump_strings(blob: bytes, label: str) -> list[str]:
    """抽取可读字符串并标注命中关键词的行。"""
    hits = []
    for m in _PRINTABLE.finditer(blob):
        s = m.group().decode("ascii", "replace")
        low = s.lower()
        if any(k in low for k in _INTERESTING):
            hits.append(s)
    print(f"-- {label}: 共命中 {len(hits)} 条")
    return hits


def main() -> int:
    exe = game_process.detect_launcher_exe()
    if exe is None or not exe.is_file():
        print("!! 反查不到启动器")
        return 2
    data = exe.read_bytes()
    found = _find_cookie(data)
    if found is None:
        print("!! 未找到 PyInstaller cookie——可能不是 PyInstaller 打包或格式不同")
        return 2
    cookie = found[1]
    print(f"CArchive cookie: 格式={cookie[0]} 包长={cookie[1]} TOC偏移={cookie[2]} TOC长={cookie[3]}")

    entries = _parse_toc(data, cookie)
    print(f"条目数: {len(entries)}")
    for e in entries:
        print(f"  [{e['type']}] {e['name']}  压缩={e['cmprs']}  {e['csize']}->{e['usize']}B")

    out_dir = Path(__file__).resolve().parent / "_out"
    out_dir.mkdir(exist_ok=True)
    all_hits: list[str] = [f"launcher={exe}", f"cookie={cookie[0]} entries={len(entries)}", ""]

    for e in entries:
        if e["type"] not in ("s", "m", "M"):
            continue
        blob = zlib.decompress(e["raw"]) if e["cmprs"] else e["raw"]
        if e["type"] == "s":
            tag = "main_script"
        else:
            tag = f"module_{e['name']}"
        (out_dir / f"inspect_{tag}.pyc").write_bytes(blob)
        hits = _dump_strings(blob, tag)
        if hits:
            all_hits.append(f"== {tag} ==")
            all_hits.extend(sorted(set(hits)))
            all_hits.append("")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"inspect_strings_{stamp}.txt"
    out.write_text("\n".join(all_hits) + "\n", encoding="utf-8")
    print(f"\n命中清单已存: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
