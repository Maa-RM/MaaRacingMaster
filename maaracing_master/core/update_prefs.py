# -*- coding: utf-8 -*-
"""宿主更新偏好的只读镜像：所有「从发行货架取字节」的动作跟同一个首选源走。

真源是宿主写出的 ``<用户数据根>/config/update-prefs.json``——唯一写者是
``apps/MaaRacingMaster.Shell/UpdatePreferences.cs``（默认 cnb、可选 github）。
这里只读、不写，也不另立默认：读不到或字段非法一律按 cnb，与
``UpdatePreferences.Load`` 同口径。插件不再各自决定「先试哪个源」，
否则同一个偏好会有两份实现，改一边漏一边。
"""
from __future__ import annotations

import json
from pathlib import Path

from maaracing_master.core.paths import user_data_dir

DEFAULT_SOURCE = "cnb"
KNOWN_SOURCES = ("cnb", "github")
PREFS_NAME = "update-prefs.json"


def prefs_path() -> Path:
    return user_data_dir() / "config" / PREFS_NAME


def preferred_source() -> str:
    """返回 "cnb" 或 "github"；缺失、损坏、非法值都回 cnb（宿主同口径）。"""
    try:
        data = json.loads(prefs_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DEFAULT_SOURCE
    source = data.get("source") if isinstance(data, dict) else None
    return source if source in KNOWN_SOURCES else DEFAULT_SOURCE
