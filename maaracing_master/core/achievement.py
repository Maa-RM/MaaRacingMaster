# -*- coding: utf-8 -*-
"""成就系统机制（逻辑预埋）——注册索引、catalog 校验、追踪引擎、进度存档、事实门面。

契约正文以本模块 docstring 为 home（退役后提炼进 core/CODE_WIKI.md 成就系统节）。
本模块只做通用机制，不认识任何插件业务：

1. **声明索引**：registry 扫描插件 manifest 的 ACHIEVEMENTS 指针，加载 catalog 经
   ``register_catalog`` 校验后入 ``ACHIEVEMENT_REGISTRY``（无运行态派生索引，非第二份
   持久化定义）。core 自带一份目录（``CORE_CATALOG_PATH``，mod_id 固定为 ``core``，
   该命名空间为保留值）走同一个 ``register_catalog``；引擎由 sidecar 持有，初始化时
   读索引、恢复进度、绑定门面。
2. **校验**：catalog 按 schema 版本与规则白名单过滤，非法条目丢弃并 warning，不猜。
3. **引擎**：消费事实信封 → 规则匹配 → 进度累计 → 有界去重 → 原子持久化 → 解锁判定。
   事务顺序：锁内算出一致新状态 → 持久化成功才置为已提交 → 提交后锁外发解锁回调。
4. **存档**：读盘与导入共用值规范化（进度只收有限数值、解锁时间戳收非负数值、去重键
   只收非空字符串）；导入遇到非法值整份拒绝。损坏档先复制留证再修复为规范档，留证
   失败转只读，原文件不动。导入与启动共用补偿路径：只补「进度达标但缺解锁记录」的
   条目，不撤销既有解锁，恢复出的解锁不弹通知。
5. **门面**：``report_fact`` 尽力而为、永不抛；未绑定引擎即无操作。
6. **重置**：仅支持全量；单项重置会牵连依赖它的元成就，级联语义定案后再开放。

分层：本模块不 import 任何插件（红线 ADR 0001）；全局成就 ID 命名空间由注册到的
mod_id 派生，不硬编码插件名。
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from maaracing_master.core.logger import logger
from maaracing_master.core.paths import data_dir

ACHIEVEMENT_CATALOG_SCHEMA_VERSION = 1
ACHIEVEMENT_STORE_SCHEMA_VERSION = 1

# core 自带成就目录的保留 mod_id：插件目录不得占用此命名空间（registry 侧拒绝）。
CORE_MOD_ID = "core"
CORE_CATALOG_PATH = Path(__file__).resolve().parent / "resources" / "achievements.json"

# 视觉档位：第四档（隐藏）不在此枚举内，由条目的 hidden 布尔承担。
TIER_TYPES = ("normal", "rare", "precious")

# 去重保留窗口：consumed_keys  FIFO 容量。去重保证仅限窗口内（契约 I3），
# 窗口外重报不保证去重。值由边界测试锁死。
DEDUP_WINDOW = 4096

AGG_TYPES = frozenset({"count", "count_if", "sum", "max", "all_unlocked"})
_LOCAL_ID_RE = re.compile(r"^[a-z0-9_]+$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

MAX_ACHIEVEMENTS = 256
MAX_TITLE = 60
MAX_DESCRIPTION = 300
MAX_CATEGORY = 40
MAX_FACT_KIND = 120
MAX_FIELD = 60
MAX_HINT = 120

# 声明索引：mod_id → 规范化 catalog（含全局 ID）。registry 扫描时填充，引擎初始化时读取。
ACHIEVEMENT_REGISTRY: dict[str, dict] = {}


class AchievementContractError(ValueError):
    """catalog 整体不可用（schema 版本不支持、顶层形状错误）。"""


class AchievementStoreError(RuntimeError):
    """存档读写/版本校验失败：导入、导出、重置的统一失败出口。"""


class AchievementFrozenError(AchievementStoreError):
    """存档由更高版本写入：只读冻结，写操作一律拒绝（不改运行结局）。"""


def _clean_text(value: Any, field: str, limit: int,
                warnings: list[str], *, required: bool = True) -> str | None:
    if value is None:
        if required:
            warnings.append(f"{field} 缺失")
        return None
    if not isinstance(value, str):
        warnings.append(f"{field} 不是字符串")
        return None
    if _CONTROL_RE.search(value):
        warnings.append(f"{field} 含控制字符")
        return None
    if not value.strip():
        if required:
            warnings.append(f"{field} 为空")
        return None
    if len(value) > limit:
        warnings.append(f"{field} 超 {limit} 字已截断")
        return value[:limit]
    return value


def _finite_number(value: Any) -> float | None:
    """有限数值（拒绝 bool / NaN / inf / 非数 / 超出 float 范围的大整数）；否则 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value):
            return None
        return float(value)
    except OverflowError:  # 大整数转 float 越界：按非法值处理，别让它把调用方炸掉
        return None


def _finite_or_int(value: Any) -> bool:
    """可安全参与累加/落盘的数值：int 视为有限，float 须有限。

    不能直接用 ``math.isfinite``：超出 float 范围的大整数会让它抛 OverflowError。
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _validate_condition(cond: Any, field: str, warnings: list[str]) -> dict | None:
    if not isinstance(cond, dict) or not cond:
        warnings.append(f"{field} 条件形状非法")
        return None
    keys = set(cond)
    if keys == {"eq"}:
        if _finite_number(cond["eq"]) is None and not isinstance(cond["eq"], (str, bool)):
            warnings.append(f"{field} eq 值非法")
            return None
        return {"eq": cond["eq"]}
    if keys <= {"gte", "lte"} and keys:
        out: dict = {}
        for k in keys:
            num = _finite_number(cond[k])
            if num is None:
                warnings.append(f"{field} {k} 非有限数值")
                return None
            out[k] = num
        return out
    if keys == {"in"}:
        if not isinstance(cond["in"], list) or not cond["in"]:
            warnings.append(f"{field} in 需非空列表")
            return None
        return {"in": list(cond["in"])}
    warnings.append(f"{field} 条件键非法（仅 eq/gte/lte/in）")
    return None


def _validate_rule(rule: Any, warnings: list[str]) -> dict | None:
    if not isinstance(rule, dict):
        warnings.append("rule 不是对象")
        return None
    agg = rule.get("agg")
    # 先验类型再判白名单：不可哈希值（如数组）会让集合成员判断抛 TypeError，
    # 冒到 registry 会把整份目录一起丢掉。
    if not isinstance(agg, str) or agg not in AGG_TYPES:
        warnings.append(f"rule.agg 非法: {agg!r}")
        return None
    if agg == "all_unlocked":
        if rule.get("scope") != "plugin":
            warnings.append("all_unlocked scope 必须为 plugin")
            return None
        return {"agg": "all_unlocked", "scope": "plugin"}
    fact = _clean_text(rule.get("fact"), "rule.fact", MAX_FACT_KIND, warnings)
    if fact is None:
        return None
    target = _finite_number(rule.get("target"))
    if target is None or target <= 0:
        warnings.append("rule.target 必须为 > 0 的有限数值")
        return None
    out: dict = {"agg": agg, "fact": fact, "target": target}
    if agg in ("sum", "max"):
        field = _clean_text(rule.get("field"), "rule.field", MAX_FIELD, warnings)
        if field is None:
            return None
        out["field"] = field
    if agg == "count_if":
        where_raw = rule.get("where")
        if not isinstance(where_raw, dict) or not where_raw:
            warnings.append("count_if where 需非空对象")
            return None
        where: dict = {}
        for fname, cond in where_raw.items():
            if not isinstance(fname, str) or not fname:
                warnings.append("where 字段名非法")
                return None
            vc = _validate_condition(cond, f"where.{fname}", warnings)
            if vc is None:
                return None
            where[fname] = vc
        out["where"] = where
    return out


def validate_catalog(raw: Any, mod_id: str) -> tuple[dict, tuple[str, ...]]:
    """校验并规范化 catalog；非法条目丢弃 + warning，整体不可用抛 AchievementContractError。

    mod_id 不含 '.'（命名空间分隔符），local_id 匹配 [a-z0-9_]+；全局 ID = mod_id.local_id。
    """
    warnings: list[str] = []
    if not isinstance(mod_id, str) or not mod_id or "." in mod_id:
        raise AchievementContractError(f"mod_id 非法（不得含 '.'）: {mod_id!r}")
    if not isinstance(raw, dict):
        raise AchievementContractError("catalog 顶层不是对象")
    version = raw.get("schema_version")
    if version != ACHIEVEMENT_CATALOG_SCHEMA_VERSION:
        raise AchievementContractError(f"catalog schema_version 不支持: {version!r}")
    items = raw.get("achievements")
    if not isinstance(items, list):
        raise AchievementContractError("catalog.achievements 不是列表")
    if len(items) > MAX_ACHIEVEMENTS:
        warnings.append(f"成就数超 {MAX_ACHIEVEMENTS}，超出部分丢弃")
        items = items[:MAX_ACHIEVEMENTS]

    entries: list[dict] = []
    seen: set[str] = set()
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            warnings.append(f"条目 {idx} 不是对象，丢弃")
            continue
        local_id = item.get("id")
        if not isinstance(local_id, str) or not _LOCAL_ID_RE.match(local_id):
            warnings.append(f"条目 {idx} id 非法（需 [a-z0-9_]+），丢弃")
            continue
        if local_id in seen:
            warnings.append(f"条目 {idx} id 重复: {local_id}，丢弃")
            continue
        rule = _validate_rule(item.get("rule"), warnings)
        if rule is None:
            warnings.append(f"条目 {local_id} rule 非法，丢弃")
            continue
        title = _clean_text(item.get("title"), f"{local_id}.title", MAX_TITLE, warnings)
        if title is None:
            warnings.append(f"条目 {local_id} 缺标题，丢弃")
            continue
        description = _clean_text(item.get("description"), f"{local_id}.description",
                                  MAX_DESCRIPTION, warnings, required=False) or ""
        category = _clean_text(item.get("category"), f"{local_id}.category",
                               MAX_CATEGORY, warnings, required=False) or ""
        hidden = bool(item.get("hidden", False))
        tier = item.get("tier", "normal")
        if tier not in TIER_TYPES:
            # 展示字段，不丢条目：非法值降级为 normal（与旧 catalog 缺省行为一致）。
            warnings.append(f"条目 {local_id} tier 非法（{tier!r}），按 normal 处理")
            tier = "normal"
        hint = _clean_text(item.get("hint"), f"{local_id}.hint",
                           MAX_HINT, warnings, required=False) or ""
        seen.add(local_id)
        entries.append({
            "id": local_id,
            "global_id": f"{mod_id}.{local_id}",
            "title": title,
            "description": description,
            "category": category,
            "tier": tier,
            "hint": hint,
            "hidden": hidden,
            "rule": rule,
        })

    # 空集合保护：插件无任何非元成就时，其 all_unlocked 条目丢弃（防 vacuously 自动解锁）。
    has_non_meta = any(e["rule"]["agg"] != "all_unlocked" for e in entries)
    if not has_non_meta:
        kept = []
        for e in entries:
            if e["rule"]["agg"] == "all_unlocked":
                warnings.append(f"条目 {e['id']} 元成就依赖空集合，丢弃")
            else:
                kept.append(e)
        entries = kept

    return {"schema_version": ACHIEVEMENT_CATALOG_SCHEMA_VERSION,
            "mod_id": mod_id, "achievements": entries}, tuple(warnings)


def register_catalog(mod_id: str, raw: Any) -> tuple[dict | None, tuple[str, ...]]:
    """校验 catalog 并入声明索引；整体不可用返回 (None, 原因)，不写入索引。"""
    try:
        normalized, warnings = validate_catalog(raw, mod_id)
    except AchievementContractError as exc:
        return None, (str(exc),)
    ACHIEVEMENT_REGISTRY[mod_id] = normalized
    return normalized, warnings


def register_core_catalog(path: Path | None = None) -> bool:
    """加载 core 自带成就目录并入声明索引；任何失败只 WARNING，返回是否入索引。"""
    src = Path(path) if path is not None else CORE_CATALOG_PATH
    try:
        raw = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.log(f"[achievement] core 成就目录不可用（{src.name}）: {exc!r}", "WARNING")
        return False
    normalized, warnings = register_catalog(CORE_MOD_ID, raw)
    for w in warnings:
        logger.log(f"[achievement] core 成就: {w}", "WARNING")
    if normalized is None:
        logger.log("[achievement] core 成就目录校验失败，跳过", "WARNING")
        return False
    logger.log(f"[achievement] 已注册 core 成就目录（{len(normalized['achievements'])} 条）", "DEBUG")
    return True


def _store_path() -> Path:
    return data_dir() / "achievements.json"


def _empty_state() -> dict:
    return {
        "store_schema_version": ACHIEVEMENT_STORE_SCHEMA_VERSION,
        "progress": {},
        "unlocked": {},
        "consumed_keys": [],
    }


def _normalize_store(raw: dict) -> tuple[dict, list[str]]:
    """把存档/备份里的值收敛成引擎可安全运算的类型；返回 (规范态, 问题清单)。

    保守原则：能无损保留的值一律保留（int 保持 int、`max` 的 null 初值保留），
    只剔除无法参与运算或无法落盘的值。不认领未知 global_id——版本回退后进度还要保留。
    """
    issues: list[str] = []
    state = _empty_state()

    progress = raw.get("progress")
    if progress is None:
        state["progress"] = {}
    elif not isinstance(progress, dict):
        issues.append("progress 不是对象")
    else:
        for key, value in progress.items():
            if not isinstance(key, str):
                issues.append(f"progress 键非字符串: {key!r}")
            elif value is None or _finite_number(value) is not None:
                state["progress"][key] = value
            else:
                issues.append(f"progress[{key}] 非有限数值: {value!r}")

    unlocked = raw.get("unlocked")
    if unlocked is None:
        state["unlocked"] = {}
    elif not isinstance(unlocked, dict):
        issues.append("unlocked 不是对象")
    else:
        for key, value in unlocked.items():
            if not isinstance(key, str):
                issues.append(f"unlocked 键非字符串: {key!r}")
            elif _finite_number(value) is not None and value >= 0:
                state["unlocked"][key] = int(value)
            else:
                issues.append(f"unlocked[{key}] 不是非负时间戳: {value!r}")

    consumed = raw.get("consumed_keys")
    if consumed is None:
        state["consumed_keys"] = []
    elif not isinstance(consumed, list):
        issues.append("consumed_keys 不是列表")
    else:
        for value in consumed:
            if isinstance(value, str) and value:
                state["consumed_keys"].append(value)
            else:
                issues.append(f"consumed_keys 含非法键: {value!r}")

    return state, issues


def _match_condition(value: Any, cond: dict) -> bool:
    if value is None:
        return False
    if "eq" in cond:
        return value == cond["eq"]
    if "in" in cond:
        return value in cond["in"]
    num = _finite_number(value)
    if num is None:
        return False
    if "gte" in cond and num < cond["gte"]:
        return False
    if "lte" in cond and num > cond["lte"]:
        return False
    return True


def _match_where(payload: dict, where: dict) -> bool:
    return all(_match_condition(payload.get(field), cond)
               for field, cond in where.items())


class AchievementEngine:
    """成就追踪引擎：由 sidecar 创建并持有生命周期。

    on_unlock(global_id, entry, unlocked_ts) 在锁外调用；entry 为规范化条目（含标题/档位/
    触发条件文案）。
    store_path 缺省走用户数据目录；测试可注入临时路径。
    """

    def __init__(self, *, store_path: Path | None = None,
                 on_unlock: Callable[[str, dict, int | None], None] | None = None,
                 registry: dict[str, dict] | None = None) -> None:
        self._path = Path(store_path) if store_path is not None else _store_path()
        self._on_unlock = on_unlock
        self._lock = Lock()
        self._frozen = False
        # None | "version"（更高版本写入）| "corrupt"（损坏且无法留备份）：停写原因，
        # 供 UI 区分展示——把两种只读都说成"版本过高"是在撒谎。
        self._frozen_reason: str | None = None
        # 本次加载修复时留下的原文件备份文件名（不含目录），供 UI 告知恢复路径。
        self._repair_backup: str | None = None
        # 磁盘存档的版本（缺失/非法按本程序支持版本计），供 UI 展示降级原因。
        self._store_version: int = ACHIEVEMENT_STORE_SCHEMA_VERSION
        reg = registry if registry is not None else ACHIEVEMENT_REGISTRY
        self._entries: dict[str, dict] = {}
        self._rules_by_kind: dict[str, list[dict]] = {}
        self._meta_by_mod: dict[str, list[dict]] = {}
        self._non_meta_by_mod: dict[str, list[str]] = {}
        for mod_id, catalog in reg.items():
            for entry in catalog.get("achievements", []):
                gid = entry["global_id"]
                self._entries[gid] = entry
                rule = entry["rule"]
                if rule["agg"] == "all_unlocked":
                    self._meta_by_mod.setdefault(mod_id, []).append(entry)
                else:
                    self._rules_by_kind.setdefault(rule["fact"], []).append(entry)
                    self._non_meta_by_mod.setdefault(mod_id, []).append(gid)
        state = self._load_state()
        self._progress: dict[str, Any] = state["progress"]
        self._unlocked: dict[str, int] = state["unlocked"]
        self._consumed: OrderedDict[str, bool] = OrderedDict(
            (k, True) for k in state["consumed_keys"][-DEDUP_WINDOW:])
        # 加载期还原：补齐「进度达标但缺解锁记录」的条目并求值元成就；落盘成功才提交。
        # 普通成就的补偿静默（属于恢复，不是实时解锁）；元成就维持既有通知行为。
        # 任何异常都不留半提交态、不上抛（不阻断 sidecar 启动）。
        if not self._frozen:
            new_progress = dict(self._progress)
            new_unlocked = dict(self._unlocked)
            recovered, newly = self._reconcile_locked(new_progress, new_unlocked)
            if recovered or newly:
                try:
                    self._save_state_locked(new_progress, new_unlocked, self._consumed)
                    self._progress = new_progress
                    self._unlocked = new_unlocked
                except Exception as exc:  # noqa: BLE001 —— 初始化失败降级，不阻断启动
                    logger.log(f"[achievement] 加载期补偿存档失败（未提交）: {exc!r}", "WARNING")
                    newly = []
            self._notify(newly)

    # ---------- 存档 ----------

    def _load_state(self) -> dict:
        """读盘并把值收敛成规范态；损坏档先留证再修复，留证失败则转只读。

        返回的进度/解锁态保证可直接参与运算与 JSON 序列化（不含 NaN/Infinity/错类型）。
        """
        self._repair_backup = None
        if not self._path.is_file():
            return _empty_state()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.log(f"[achievement] 存档损坏，重建为空: {exc!r}", "WARNING")
            if self._preserve_corrupt_store("解析失败"):
                self._rewrite_repaired(_empty_state())
            return _empty_state()
        if not isinstance(raw, dict):
            logger.log("[achievement] 存档形状非法，重建为空", "WARNING")
            if self._preserve_corrupt_store("顶层非对象"):
                self._rewrite_repaired(_empty_state())
            return _empty_state()

        version = raw.get("store_schema_version")
        version_ok = isinstance(version, int) and not isinstance(version, bool)
        if version_ok:
            self._store_version = version
            if version > ACHIEVEMENT_STORE_SCHEMA_VERSION:
                # 用户降级：保留原文件、停写，防降级清档毁进度。
                logger.log(f"[achievement] 存档版本 {version} 高于支持 "
                           f"{ACHIEVEMENT_STORE_SCHEMA_VERSION}，保留原文件停写", "WARNING")
                self._frozen = True
                self._frozen_reason = "version"
                return _empty_state()

        state, issues = _normalize_store(raw)
        if version is not None and not version_ok:
            # 版本字段读不懂按损坏处理：先留证再修复，不猜它是不是更高版本。
            issues.insert(0, f"store_schema_version 非法: {version!r}")
        if issues:
            if not self._preserve_corrupt_store(f"{len(issues)} 处非法值"):
                return _empty_state()
            self._rewrite_repaired(state)
        return state

    def _preserve_corrupt_store(self, reason: str) -> bool:
        """修复前把原文件复制一份留证；成功返回 True，失败转只读并返回 False。

        只新建副本、不动原文件：留证失败时宁可不写盘，也不让后续保存覆盖唯一原件。
        """
        for attempt in range(10):
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            suffix = f"-{attempt}" if attempt else ""
            dest = self._path.with_name(f"{self._path.stem}.corrupt-{stamp}{suffix}.json")
            try:
                with dest.open("xb") as fh:
                    fh.write(self._path.read_bytes())
            except FileExistsError:
                continue
            except OSError as exc:
                logger.log(f"[achievement] 存档损坏（{reason}）但无法留备份，转只读: {exc!r}",
                           "WARNING")
                self._frozen = True
                self._frozen_reason = "corrupt"
                return False
            self._repair_backup = dest.name
            logger.log(f"[achievement] 存档损坏（{reason}），原文件已备份为 {dest.name}，随后修复",
                       "WARNING")
            return True
        logger.log("[achievement] 存档备份命名冲突，转只读（原文件未动）", "WARNING")
        self._frozen = True
        self._frozen_reason = "corrupt"
        return False

    def _rewrite_repaired(self, state: dict) -> None:
        """把修复后的规范态立即落盘，避免每次启动重复修复；写失败只告警。"""
        try:
            self._save_state_locked(
                state["progress"], state["unlocked"],
                OrderedDict((k, True) for k in state["consumed_keys"]))
        except Exception as exc:  # noqa: BLE001 —— 修复是旁路，写失败不阻断启动
            logger.log(f"[achievement] 修复后的存档写回失败（保持内存态）: {exc!r}", "WARNING")

    def _save_state_locked(self, progress: dict, unlocked: dict,
                           consumed: OrderedDict) -> None:
        """把候选状态原子落盘；失败清理临时文件并上抛（调用方据此不提交）。"""
        state = {
            "store_schema_version": ACHIEVEMENT_STORE_SCHEMA_VERSION,
            "progress": progress,
            "unlocked": unlocked,
            "consumed_keys": list(consumed.keys()),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
        try:
            # allow_nan=False：非有限数漏网时落盘失败并回滚，绝不写成非标准 JSON。
            tmp.write_text(json.dumps(state, ensure_ascii=False, allow_nan=False),
                           encoding="utf-8")
            os.replace(tmp, self._path)
        finally:
            tmp.unlink(missing_ok=True)

    # ---------- 导入 / 导出 / 重置 ----------

    @property
    def frozen(self) -> bool:
        """只读停写：高版本存档（保护降级）或损坏档无法留备份。"""
        return self._frozen

    @property
    def frozen_reason(self) -> str | None:
        """停写原因：`version`（更高版本写入）｜`corrupt`（损坏且无法留备份）｜None。"""
        return self._frozen_reason

    @property
    def repaired_backup(self) -> str | None:
        """本次加载修复时留下的原文件备份文件名；未发生修复为 None。"""
        return self._repair_backup

    @property
    def store_version(self) -> int:
        """磁盘存档的版本；缺失或非法按本程序支持版本计。"""
        return self._store_version

    @property
    def supported_store_version(self) -> int:
        return ACHIEVEMENT_STORE_SCHEMA_VERSION

    def reload_from_disk(self) -> bool:
        """重新从磁盘载入状态，并按文件重算冻结态；返回载入后是否可写。

        与启动加载走同一条路径（规范化 + 损坏修复 + 补偿），避免出现第二套加载语义。
        """
        with self._lock:
            self._frozen = False
            self._frozen_reason = None
            state = self._load_state()
            self._progress = state["progress"]
            self._unlocked = state["unlocked"]
            self._consumed = OrderedDict(
                (k, True) for k in state["consumed_keys"][-DEDUP_WINDOW:])
            if not self._frozen:
                new_progress = dict(self._progress)
                new_unlocked = dict(self._unlocked)
                recovered, newly = self._reconcile_locked(new_progress, new_unlocked)
                if recovered or newly:
                    try:
                        self._save_state_locked(new_progress, new_unlocked, self._consumed)
                        self._progress = new_progress
                        self._unlocked = new_unlocked
                    except Exception as exc:  # noqa: BLE001 —— 载入期补偿失败不阻断
                        logger.log(f"[achievement] 重载补偿存档失败（未提交）: {exc!r}", "WARNING")
            return not self._frozen

    def _read_json(self, path: Path) -> dict:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AchievementStoreError(f"读取失败（{path.name}）: {exc!r}") from exc
        if not isinstance(raw, dict):
            raise AchievementStoreError(f"顶层不是对象（{path.name}）")
        return raw

    def _write_json(self, path: Path, payload: dict) -> None:
        """原子写：临时名 + replace，失败清理临时文件并上抛（不留半截文件）。"""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AchievementStoreError(f"目录不可写（{path.parent}）: {exc!r}") from exc
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False),
                           encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            raise AchievementStoreError(f"写入失败（{path.name}）: {exc!r}") from exc
        except ValueError as exc:
            raise AchievementStoreError(
                f"数据含非法数值，无法写成合法备份（{path.name}）: {exc!r}") from exc
        finally:
            tmp.unlink(missing_ok=True)

    def _backup_locked(self, tag: str) -> Path | None:
        """把现存档另存一份（`<stem>.<tag>-<时间戳>.json`）；无存档返回 None。"""
        if not self._path.is_file():
            return None
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = self._path.with_name(f"{self._path.stem}.{tag}-{stamp}.json")
        try:
            dest.write_bytes(self._path.read_bytes())
        except OSError as exc:
            raise AchievementStoreError(f"备份现档失败: {exc!r}") from exc
        return dest

    def export_store(self, dest: Path) -> int:
        """把存档导出到 dest（文件本体 + 两个元信息键），返回条目数。

        导出的是**存档文件本身**，不另设格式，少一层转换就少一类 bug。文件不存在时
        （从没产生过进度）导出空态：合法备份比一句无差别错误有用。
        损坏且无法留备份时改导出内存态（规范后的空档）：原文件含非法值，照抄只会
        产出导入端必然拒绝的备份。
        """
        dest = Path(dest)
        with self._lock:
            if self._path.is_file() and self._frozen_reason != "corrupt":
                payload = dict(self._read_json(self._path))
            else:
                payload = _empty_state()
                payload["progress"] = dict(self._progress)
                payload["unlocked"] = dict(self._unlocked)
                payload["consumed_keys"] = list(self._consumed.keys())
            from maaracing_master import __version__  # 延迟导入：避免模块级循环
            payload["exported_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            payload["app_version"] = __version__
            self._write_json(dest, payload)
            return len(payload.get("progress") or {})

    def import_store(self, src: Path) -> int:
        """用备份覆盖当前存档，并把内存态同步到新档；返回条目数。

        保护顺序：校验版本与数值 → 备份现档 → 补偿解锁/元成就 → 原子写入 → 提交内存。
        任一步失败，原存档与内存态都不变。备份含非法数值时整份拒绝（不静默清洗）。
        不清洗未知 global_id（版本回退后进度还在）；补偿只增不减，不撤销历史解锁，
        且不触发解锁通知（恢复备份不该刷一屏提示）。
        """
        src = Path(src)
        with self._lock:
            if self._frozen:
                raise AchievementFrozenError(
                    f"存档由更高版本（v{self._store_version}）写入，请先升级程序")
            raw = self._read_json(src)
            version = raw.get("store_schema_version")
            if not isinstance(version, int) or isinstance(version, bool) \
                    or version > ACHIEVEMENT_STORE_SCHEMA_VERSION:
                raise AchievementStoreError(f"备份版本不支持: {version!r}")
            for key in ("progress", "unlocked", "consumed_keys"):
                if key not in raw:
                    raise AchievementStoreError("备份形状非法（progress/unlocked/consumed_keys）")
            state, issues = _normalize_store(raw)
            if issues:
                raise AchievementStoreError(
                    f"备份含非法数值（{len(issues)} 处），已取消导入：{issues[0]}")
            new_progress = state["progress"]
            new_unlocked = state["unlocked"]
            new_consumed = OrderedDict(
                (k, True) for k in state["consumed_keys"][-DEDUP_WINDOW:])
            # 先补偿再落盘：补齐「进度达标但缺解锁记录」的条目与元成就，一次提交。
            self._reconcile_locked(new_progress, new_unlocked)

            # 先备份现档再覆盖：顺序反了备份的就是新档，等于没备份。
            self._backup_locked("pre-import")
            self._save_state_locked(new_progress, new_unlocked, new_consumed)

            self._progress = new_progress
            self._unlocked = new_unlocked
            self._consumed = new_consumed
            return len(self._progress)

    def reset(self, scope: str) -> int:
        """重置进度：仅支持 `all`（全清进度、解锁记录与去重键）。

        单项重置暂不开放：撤销本项会牵连依赖它的元成就，级联语义定案前保留该路径
        只会制造已解锁态不一致。冻结态一律拒绝：保护高版本存档不被降级程序覆盖。
        """
        if scope != "all":
            raise AchievementStoreError(f"不支持的重置范围: {scope!r}（仅支持 all）")
        with self._lock:
            if self._frozen:
                raise AchievementFrozenError(
                    f"存档由更高版本（v{self._store_version}）写入，请先升级程序")
            new_progress: dict = {}
            new_unlocked: dict = {}
            new_consumed: OrderedDict = OrderedDict()
            cleared = len(set(self._progress) | set(self._unlocked))
            self._save_state_locked(new_progress, new_unlocked, new_consumed)
            self._progress = new_progress
            self._unlocked = new_unlocked
            self._consumed = new_consumed
            return cleared

    # ---------- 进度与解锁 ----------

    def _init_progress(self, entry: dict) -> Any:
        return None if entry["rule"]["agg"] == "max" else 0

    def _is_unlocked(self, entry: dict, progress: Any) -> bool:
        agg = entry["rule"]["agg"]
        if agg == "all_unlocked":
            return False
        if progress is None:
            return False
        return progress >= entry["rule"]["target"]

    def _eval_meta_locked(self, already: set[str], unlocked: dict) -> list[str]:
        """求值元成就（锁内调用，操作候选 unlocked 副本）；返回新解锁的全局 ID。"""
        newly: list[str] = []
        unlocked_set = set(unlocked) | already
        for mod_id, metas in self._meta_by_mod.items():
            deps = self._non_meta_by_mod.get(mod_id, [])
            if not deps or not all(d in unlocked_set for d in deps):
                continue
            for meta in metas:
                gid = meta["global_id"]
                if gid not in unlocked_set:
                    unlocked[gid] = time.time_ns()
                    unlocked_set.add(gid)
                    newly.append(gid)
        return newly

    def _reconcile_locked(self, progress: dict, unlocked: dict) -> tuple[list[str], list[str]]:
        """补齐「进度已达标但缺解锁记录」的普通成就，再求值元成就。

        只增不减：已有解锁记录一律不动——规则目标提高不得撤销历史解锁。
        返回 (补回的普通成就, 新解锁的元成就)；调用方负责落盘与是否通知。
        """
        recovered: list[str] = []
        for gid, entry in self._entries.items():
            rule = entry["rule"]
            if rule["agg"] == "all_unlocked" or gid in unlocked:
                continue
            current = progress.get(gid)
            if current is None or not self._is_unlocked(entry, current):
                continue
            unlocked[gid] = time.time_ns()
            recovered.append(gid)
        return recovered, self._eval_meta_locked(set(recovered), unlocked)

    def _notify(self, global_ids: list[str]) -> None:
        if self._on_unlock is None:
            return
        for gid in global_ids:
            entry = self._entries.get(gid)
            if entry is None:
                continue
            try:
                self._on_unlock(gid, entry, self._unlocked.get(gid))
            except Exception as exc:  # noqa: BLE001 —— 通知尽力而为，不回灌引擎
                logger.log(f"[achievement] 解锁回调异常 {gid}: {exc!r}", "WARNING")

    def process_fact(self, kind: str, dedup_key: str, payload: dict,
                     ts_wall_ns: int | None = None) -> bool:
        """处理一条事实；返回是否成功提交。异常向上抛由门面兜底（契约 I1）。"""
        if not isinstance(kind, str) or not isinstance(dedup_key, str) or not dedup_key:
            return False
        if not isinstance(payload, dict):
            return False
        matching = self._rules_by_kind.get(kind)
        if not matching:
            return False
        with self._lock:
            if self._frozen:
                return False
            if dedup_key in self._consumed:
                return False
            new_progress = dict(self._progress)
            new_unlocked = dict(self._unlocked)
            newly: list[str] = []
            for entry in matching:
                rule = entry["rule"]
                agg = rule["agg"]
                gid = entry["global_id"]
                cur = new_progress.get(gid, self._init_progress(entry))
                if agg == "count":
                    nxt = (cur or 0) + 1
                elif agg == "count_if":
                    nxt = (cur or 0) + (1 if _match_where(payload, rule["where"]) else 0)
                elif agg == "sum":
                    val = _finite_number(payload.get(rule["field"]))
                    nxt = (cur or 0) + (val if val is not None else 0)
                    if not _finite_or_int(nxt):
                        # 溢出拒绝本次更新（不做饱和：饱和会悄悄改写真实累计值）。
                        _warn_throttled(f"[achievement] sum 聚合溢出，本次不累计: {gid}")
                        continue
                elif agg == "max":
                    val = _finite_number(payload.get(rule["field"]))
                    if val is None:
                        continue
                    nxt = val if cur is None else max(cur, val)
                else:
                    continue
                new_progress[gid] = nxt
                if gid not in new_unlocked and self._is_unlocked(entry, nxt):
                    new_unlocked[gid] = ts_wall_ns if ts_wall_ns is not None else time.time_ns()
                    newly.append(gid)
            # 去重键入有界 FIFO：超容量修剪最旧（窗口外重报不保证去重，契约 I3）。
            new_consumed = OrderedDict(self._consumed)
            new_consumed[dedup_key] = True
            new_consumed.move_to_end(dedup_key)
            while len(new_consumed) > DEDUP_WINDOW:
                new_consumed.popitem(last=False)
            newly.extend(self._eval_meta_locked(set(newly), new_unlocked))
            # 候选状态先落盘，成功才整体提交（契约 I4/P0-2）。落盘失败时 self.* 尚未
            # 改动→自动回滚，不留半提交态；异常上抛由门面隔离（契约 I1）。
            self._save_state_locked(new_progress, new_unlocked, new_consumed)
            self._progress = new_progress
            self._unlocked = new_unlocked
            self._consumed = new_consumed
        self._notify(newly)
        return True

    def get_view(self) -> list[dict]:
        """注册目录 + 进度合并视图（RPC 用）。

        隐藏成就未解锁时遮蔽标题/描述/提示，也不下发聚合方式、目标与进度——只遮蔽
        文案等于把条件写在进度条上。数值再做一次输出兜底，保证 RPC 永远是标准 JSON。
        """
        with self._lock:
            view = []
            for gid, entry in self._entries.items():
                unlocked = gid in self._unlocked
                hidden = entry["hidden"]
                masked = hidden and not unlocked
                rule = entry["rule"]
                progress = self._progress.get(gid, self._init_progress(entry))
                if not (progress is None or _finite_or_int(progress)):
                    progress = None
                ts = self._unlocked.get(gid)
                if ts is not None and not _finite_or_int(ts):
                    ts = None
                item = {
                    "global_id": gid,
                    "mod_id": entry["global_id"].split(".", 1)[0],
                    "category": entry["category"],
                    "hidden": hidden,
                    "unlocked": unlocked,
                    "unlocked_ts": ts,
                    "agg": None if masked else rule["agg"],
                    "target": None if masked else rule.get("target"),
                    "progress": None if masked else progress,
                    "title": "" if masked else entry["title"],
                    "description": "" if masked else entry["description"],
                    "hint": "" if masked else entry.get("hint", ""),
                    "tier": "hidden" if hidden else entry.get("tier", "normal"),
                }
                view.append(item)
            return view


# ---------- 事实门面（logger 式跨切面，尽力而为） ----------

_engine: AchievementEngine | None = None
_warn_lock = Lock()
_last_warn_ts = 0.0
_warn_suppressed = 0
_WARN_INTERVAL_S = 60.0


def bind_engine(engine: AchievementEngine | None) -> None:
    global _engine
    _engine = engine


def _warn_throttled(msg: str) -> None:
    global _last_warn_ts, _warn_suppressed
    now = time.monotonic()
    with _warn_lock:
        if now - _last_warn_ts >= _WARN_INTERVAL_S:
            suffix = f"（此前抑制 {_warn_suppressed} 条）" if _warn_suppressed else ""
            logger.log(msg + suffix, "WARNING")
            _last_warn_ts = now
            _warn_suppressed = 0
        else:
            _warn_suppressed += 1


def report_fact(kind: str, dedup_key: str, payload: dict,
                ts_wall_ns: int | None = None) -> bool:
    """上报事实；未绑定引擎无操作，任何异常吞 + 限频 WARNING，永不抛（契约 I1）。"""
    engine = _engine
    if engine is None:
        return False
    try:
        return engine.process_fact(kind, dedup_key, payload, ts_wall_ns)
    except Exception as exc:  # noqa: BLE001 —— 旁路功能不得拖垮运行链路
        _warn_throttled(f"[achievement] 事实处理失败（未提交）: {exc!r}")
        return False