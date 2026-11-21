# -*- coding: utf-8 -*-
"""无限驾驶结算记录存储与战绩看板载荷：只有程序局（模块流程内）的结算入库。

**入库门槛（v1 口径的延续）**：记录只在模块流程的结算读数处产生——程序没跑的
局（玩家在大厅自己开的局）根本不经过这条路径，天然不进看板。字段按 v2 最终
结算页所展示的内容定：第 N 关 / 挑战成功·失败 / 本场得分 / 过关条件 / 关卡进度
n/5 / 本轮总得分；过关判定 = 本场得分 ≥ 过关条件（PVE 规则口径），页面上的
「过关条件」与固定数值真源一致，不重复 OCR。

**存储形态**：JSONL 追加（一行一次结算），读侧容忍坏行——单条 OCR 毛刺或截断
不该让整份战绩读不出来。体量极小（每局一条），不做压缩维护。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from maaracing_master.core.dashboard_contract import DASHBOARD_CONTRACT_VERSION
from maaracing_master.core.paths import data_dir
from maaracing_master.plugins.infinite_drive import flow_data

SCHEMA_VERSION = 1

# 单条得分的合理上限：OCR 毛刺（v1 实测 6 位数粘连）判为不可解析而不是入库
_MAX_SCORE = 999_999

_RE_PROGRESS = re.compile(r"(\d+)\s*/\s*(\d+)")
_RE_TOTAL = re.compile(r"总得分[：:]\s*([\d,，\s]+)")


def store_path() -> Path:
    """结算记录文件：``data/infinite_drive/records.jsonl``（用户数据目录）。"""
    return data_dir() / "infinite_drive" / "records.jsonl"


def parse_score(text: str) -> int | None:
    """结算数字 → int；去千分位/全角逗号/空白，超合理范围判 None（OCR 毛刺）。"""
    cleaned = re.sub(r"[,\s，]", "", str(text or ""))
    if not cleaned.isdigit():
        return None
    value = int(cleaned)
    return value if 0 <= value <= _MAX_SCORE else None


def record_settlement(score_text: str, progress_text: str, *,
                      session_token: str | None, round_no: int,
                      now: datetime | None = None) -> dict:
    """把结算页读数折算成一条记录（纯函数，不落盘）。

    解析失败的字段显式置 None 并保留原文——OCR 毛刺必须是可辨认的状态，
    不许静默丢字段（没采集到 ≠ 字段恰好没有）。
    """
    score = parse_score(score_text)
    progress = _RE_PROGRESS.search(progress_text or "")
    level = int(progress.group(1)) if progress else None
    level_total = int(progress.group(2)) if progress else None
    total_match = _RE_TOTAL.search(progress_text or "")
    total_score = parse_score(total_match.group(1)) if total_match else None
    target = flow_data.PVE_STAGE_TARGETS.get(level) if level is not None else None
    cleared = None
    if score is not None and target is not None:
        cleared = score >= target
    return {
        "schema": SCHEMA_VERSION,
        "ts": (now or datetime.now()).isoformat(timespec="seconds"),
        "session": session_token,
        "round_no": round_no,
        "mode": "pve",
        "level": level,
        "level_total": level_total,
        "score": score,
        "score_raw": (score_text or "").strip(),
        "target": target,
        "cleared": cleared,
        "total_score": total_score,
        "progress_raw": (progress_text or "").strip(),
    }


def append(entry: dict) -> None:
    """追加一条记录（一行一个 JSON）。目录不存在则创建。"""
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read(limit: int | None = None) -> list[dict]:
    """读全部记录（时间序，旧行在前）；``limit`` 取最近 N 条。坏行跳过。"""
    path = store_path()
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows[-limit:] if limit else rows


def read_dashboard_payload() -> dict:
    """战绩看板载荷：统计 + 最近结算列表；无记录返回空态。"""
    rows = read()
    if not rows:
        return {
            "version": DASHBOARD_CONTRACT_VERSION,
            "title": "极速狂飙战绩",
            "blocks": [{"type": "text", "text": "还没有结算记录——用程序跑一局就有了。"}],
        }

    today = datetime.now().date()

    def _ts_date(row: dict):
        try:
            return datetime.fromisoformat(str(row.get("ts"))).date()
        except (TypeError, ValueError):
            return None

    today_rows = [r for r in rows if _ts_date(r) == today]
    today_cleared = sum(1 for r in today_rows if r.get("cleared") is True)
    scores = [r["score"] for r in rows if isinstance(r.get("score"), int)]
    best = max(scores) if scores else None
    stats = {
        "type": "stats",
        "items": [
            {"label": "今日结算", "value": str(len(today_rows))},
            {"label": "今日过关", "value": str(today_cleared)},
            {"label": "累计结算", "value": str(len(rows))},
            {"label": "累计最高分",
             "value": "—" if best is None else f"{best:,}"},
        ],
    }
    list_rows = []
    for row in rows[-5:][::-1]:
        level = row.get("level")
        score = row.get("score")
        target = row.get("target")
        text = f"第{level}关 · {score:,} 分" if isinstance(score, int) and level \
            else (row.get("score_raw") or "读数未解析")
        trailing = f"目标 {target:,}" if isinstance(target, int) else "—"
        cleared = row.get("cleared")
        if cleared is True:
            badge = {"text": "过关", "tone": "positive"}
        elif cleared is False:
            badge = {"text": "未达", "tone": "negative"}
        else:
            badge = {"text": "读数存疑", "tone": "muted"}
        try:
            clock = datetime.fromisoformat(str(row.get("ts"))).strftime("%H:%M")
        except (TypeError, ValueError):
            clock = "—"
        list_rows.append({"leading": clock, "text": text,
                          "trailing": trailing, "badge": badge})
    return {
        "version": DASHBOARD_CONTRACT_VERSION,
        "title": "极速狂飙战绩",
        "subtitle": f"累计 {len(rows)} 条",
        "blocks": [stats, {"type": "list", "page_size": 5, "rows": list_rows}],
    }
