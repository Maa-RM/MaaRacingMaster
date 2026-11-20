# -*- coding: utf-8 -*-
"""活动模块注册表：扫描插件根下的 */manifest.py 发现插件，并提供查询与创建。

- 双插件根并行扫描：内置根 = maaracing_master/plugins（随包发行，
  受管升级时整体替换）；用户根 = 用户数据根/plugins（升级不触碰，用户复制的插件
  住这里，跨升级保留）。内置根先扫，用户根后扫；
- 同 ID 冲突以内置为准（随包版本与产品契约绑定），用户根副本跳过并记 WARNING；
- 用户插件按独立目录整包装载（包内相对导入可用，无 __init__.py 也接受），
  装载进合成顶层包，与内置包完全隔离；
- `plugins/_archive/` 是归档区，不是插件：已退役模块按目录位置隔离，不注册、不列出
  （有效期只表达活动窗口，归档由位置表达，两者互不替代；见 `_archive/__init__.py`）；
- manifest 契约：ID（唯一标识）+ MODULE_CLASS（定位模块类）；
  NAME / STAGE_ORDER / REQUIRES / REQUIRES_GAMEPAD_EXCLUSIVE / REQUIRED_ASSETS /
  HAS_DASHBOARD / DASHBOARD_CONTRACT 从模块类读取（单一来源）；
- 可选的活动有效期：manifest 里的 VALID_FROM / VALID_UNTIL（ISO 8601，两端均含，
  未声明 = 永久有效）。过期模块照常注册（GUI 仍需列出并置灰），只是不再自动选中，
  显式选择必须带 force；
- 单插件加载失败（如缺依赖）仅记 WARNING 跳过，不阻断整体；
- 剥离 = 删目录，安装 = 丢入自包含目录，GUI 列表自动随之变化。
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import types
from datetime import datetime
from pathlib import Path

from maaracing_master.core import achievement
from maaracing_master.core.base import ActivityContext, ActivityModule, declares_dashboard
from maaracing_master.core.logger import logger
from maaracing_master.core.module_validity import in_window, is_expired, parse_endpoint
from maaracing_master.core.paths import user_data_dir

MODULE_REGISTRY: dict[str, type[ActivityModule]] = {}

# 内置插件根目录：maaracing_master/plugins（随包发行，受管升级时整体替换）
_PLUGINS_ROOT = Path(__file__).resolve().parent.parent / "plugins"

# 用户插件装载用的合成顶层包名：所有用户插件目录以 <合成包>.<目录名> 装入，
# 与内置包（maaracing_master.plugins.*）完全隔离，不占用任何真实顶层模块名
_USER_PLUGIN_PACKAGE = "maarm_user_plugins"

# 已注册插件的物理目录（mod_id → plugins/<name>/），供资源存在性校验定位
_PLUGIN_DIRS: dict[str, Path] = {}

# 已注册插件的导入包名（mod_id → <pkg>）：用于按 manifest 声明加载插件自有能力，
# core 只认通用声明字段，不硬编码任何模块名或实现路径。
_PLUGIN_PACKAGES: dict[str, str] = {}

# 已注册插件声明的有效期（mod_id → (VALID_FROM, VALID_UNTIL)），未声明的一端为 None
_PLUGIN_VALIDITY: dict[str, tuple[datetime | None, datetime | None]] = {}


def _parse_validity(raw, plugin: str, field: str) -> datetime | None:
    """解析有效期端点；非法值记 WARNING 并按「未声明」处理（不阻断插件加载）。"""
    dt, err = parse_endpoint(raw, field)
    if err:
        logger.log(f"[registry] 插件 {plugin}: {err}，按未声明处理", "WARNING")
    return dt


def _register_achievements(mod_id: str, plugin_dir: Path, manifest) -> None:
    """读 manifest 的 ACHIEVEMENTS 指针，加载 catalog 入成就声明索引（尽力而为）。

    成就注册与模块注册解耦：本函数任何失败只记 WARNING，不撤销已成功的模块注册。
    路径闸：仅插件目录内的相对 JSON，resolve 后仍须落在插件目录内（防穿越）。
    """
    try:
        rel = getattr(manifest, "ACHIEVEMENTS", None)
        if rel is None:
            return
        if mod_id == achievement.CORE_MOD_ID:
            # 保留命名空间：core 成就目录由 sidecar 从包内资源注册，插件不得占用。
            logger.log(f"[registry] 插件 {mod_id}: mod_id 为成就保留命名空间，跳过成就声明", "WARNING")
            return
        if not isinstance(rel, str) or not rel:
            logger.log(f"[registry] 插件 {mod_id} ACHIEVEMENTS 声明非法，跳过成就", "WARNING")
            return
        rel_path = Path(rel)
        if rel_path.is_absolute() or ".." in rel_path.parts:
            logger.log(f"[registry] 插件 {mod_id} ACHIEVEMENTS 须为插件内相对路径，跳过", "WARNING")
            return
        path = (plugin_dir / rel_path).resolve()
        if not path.is_relative_to(plugin_dir.resolve()):
            logger.log(f"[registry] 插件 {mod_id} ACHIEVEMENTS 路径越界，跳过", "WARNING")
            return
        if not path.is_file():
            logger.log(f"[registry] 插件 {mod_id} 成就目录缺失: {rel}，跳过", "WARNING")
            return
        raw = json.loads(path.read_text(encoding="utf-8"))
        normalized, warnings = achievement.register_catalog(mod_id, raw)
        for w in warnings:
            logger.log(f"[registry] 插件 {mod_id} 成就: {w}", "WARNING")
        if normalized is None:
            logger.log(f"[registry] 插件 {mod_id} 成就目录不可用，跳过", "WARNING")
        else:
            logger.log(f"[registry] 已注册插件成就: {mod_id}", "DEBUG")
    except Exception as exc:  # noqa: BLE001 —— 成就旁路，不阻断模块注册
        logger.log(f"[registry] 插件 {mod_id} 成就注册失败，跳过: {exc!r}", "WARNING")


def _plugin_entries(root: Path) -> list[Path]:
    """插件根下参与注册的目录：含 manifest.py 的子目录（按名排序，确定性）。"""
    if not root.is_dir():
        return []
    return [e for e in sorted(root.iterdir()) if e.is_dir() and (e / "manifest.py").exists()]


def _ensure_user_plugin_pkg(entry: Path) -> str:
    """把用户插件目录包装成可导入的包（<合成包>.<目录名>），返回包名。

    有 __init__.py 按常规包装载并执行；没有则造命名空间式包（用户手工拷贝的常态形态）。
    子模块（manifest / MODULE_CLASS 指向的模块）经包的 __path__ 在插件目录内定位，
    包内相对导入可用；与内置包完全隔离。
    """
    parent = sys.modules.get(_USER_PLUGIN_PACKAGE)
    if parent is None:
        parent = types.ModuleType(_USER_PLUGIN_PACKAGE)
        parent.__path__ = []  # 合成顶层包裹层：子包一律按目录显式装载
        sys.modules[_USER_PLUGIN_PACKAGE] = parent
    pkg_name = f"{_USER_PLUGIN_PACKAGE}.{entry.name}"
    if pkg_name in sys.modules:
        return pkg_name
    init = entry / "__init__.py"
    if init.is_file():
        spec = importlib.util.spec_from_file_location(
            pkg_name, init, submodule_search_locations=[str(entry)])
        assert spec is not None and spec.loader is not None
        pkg = importlib.util.module_from_spec(spec)
        sys.modules[pkg_name] = pkg
        spec.loader.exec_module(pkg)
    else:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(entry)]
        sys.modules[pkg_name] = pkg
    return pkg_name


def _register_entry(entry: Path, pkg: str, root_label: str) -> None:
    """注册单个插件目录（读 manifest → 定位模块类 → 入索引）。加载失败只记 WARNING。"""
    try:
        manifest = importlib.import_module(f"{pkg}.manifest")
        mod_id = getattr(manifest, "ID", None)
        cls_ref = getattr(manifest, "MODULE_CLASS", None)
        if not mod_id or not cls_ref:
            return
        if mod_id in MODULE_REGISTRY:
            # 同 ID 以先扫到的为准：内置根在前，随包版本与产品契约绑定；
            # 用户根的副本视为过时残留，跳过并提示
            if root_label == "用户根":
                logger.log(
                    f"[registry] 用户插件 {entry.name} 与内置插件同 ID（{mod_id}），以内置版本为准，跳过",
                    "WARNING")
            return
        path, _, attr = cls_ref.rpartition(".")
        mod = importlib.import_module(f"{pkg}.{path}" if path else pkg)
        cls = getattr(mod, attr)
        if not (isinstance(cls, type) and issubclass(cls, ActivityModule)):
            logger.log(f"[registry] 插件 {entry.name}: {cls_ref} 非 ActivityModule 子类，跳过", "WARNING")
            return
        MODULE_REGISTRY[mod_id] = cls
        _PLUGIN_DIRS[mod_id] = entry
        _PLUGIN_PACKAGES[mod_id] = pkg
        _PLUGIN_VALIDITY[mod_id] = (
            _parse_validity(getattr(manifest, "VALID_FROM", None), entry.name, "VALID_FROM"),
            _parse_validity(getattr(manifest, "VALID_UNTIL", None), entry.name, "VALID_UNTIL"),
        )
        logger.log(f"[registry] 已注册插件模块: {mod_id}（{root_label}: {entry}）", "DEBUG")
        _register_achievements(mod_id, entry, manifest)
    except Exception as exc:  # noqa: BLE001 —— 缺依赖等按插件隔离，不影响其它插件
        logger.log(f"[registry] 插件 {entry.name} 加载失败，跳过: {exc!r}", "WARNING")


def _discover_plugins() -> None:
    """扫描两个插件根并注册模块类（幂等、容错）：内置根在前，用户根在后。"""
    for entry in _plugin_entries(_PLUGINS_ROOT):
        _register_entry(entry, f"maaracing_master.plugins.{entry.name}", "内置根")
    user_root = user_data_dir() / "plugins"
    for entry in _plugin_entries(user_root):
        # 包包裹载（执行用户 __init__.py）与注册同属单插件容错边界：
        # 初始化缺依赖/语法错误只跳过自身，不阻断扫描（评审回归：曾冒泡到模块级
        # 导入，单个坏插件拖垮整个后端启动）
        try:
            pkg_name = _ensure_user_plugin_pkg(entry)
        except Exception as exc:  # noqa: BLE001 —— 缺依赖等按插件隔离
            logger.log(f"[registry] 用户插件 {entry.name} 包初始化失败，跳过: {exc!r}", "WARNING")
            # 本次导入留下的所有模块（父包 + 已执行的子模块）都不得残留：
            # 只清父包的话，同进程修复重扫会从缓存读到旧子模块内容
            stale_prefix = f"{_USER_PLUGIN_PACKAGE}.{entry.name}"
            for stale in [k for k in sys.modules if k == stale_prefix or k.startswith(stale_prefix + ".")]:
                sys.modules.pop(stale, None)
            continue
        _register_entry(entry, pkg_name, "用户根")


_discover_plugins()


def plugin_declared_ref(module_id: str, field: str) -> str | None:
    """读插件 manifest 的通用能力声明（如 ASSET_INSTALLER），未声明返回 None。"""
    pkg = _PLUGIN_PACKAGES.get(module_id)
    if not pkg:
        return None
    manifest = importlib.import_module(f"{pkg}.manifest")
    value = getattr(manifest, field, None)
    return value if isinstance(value, str) and value else None


def load_plugin_ref(module_id: str, ref: str):
    """按 "子模块.属性" 从已注册插件包加载声明的对象；非法引用抛 ValueError。"""
    pkg = _PLUGIN_PACKAGES.get(module_id)
    if not pkg or not ref or any(ch in ref for ch in "/\\:"):
        raise ValueError(f"非法插件能力引用: {module_id}/{ref}")
    path, _, attr = ref.rpartition(".")
    if not attr:
        raise ValueError(f"非法插件能力引用: {module_id}/{ref}")
    module = importlib.import_module(f"{pkg}.{path}" if path else pkg)
    return getattr(module, attr)


def module_validity(module_id: str) -> tuple[datetime | None, datetime | None]:
    """模块声明的有效期两端（未声明的一端为 None），模块不存在时抛出 KeyError(module_id)"""
    if module_id not in MODULE_REGISTRY:
        raise KeyError(module_id)
    return _PLUGIN_VALIDITY.get(module_id, (None, None))


def module_expired(module_id: str, now: datetime | None = None) -> bool:
    """模块是否已过有效期（未声明 VALID_UNTIL = 永不过期）。"""
    _, until = module_validity(module_id)
    return is_expired(until, now)


def module_available(module_id: str, now: datetime | None = None) -> bool:
    """模块当前是否处于有效期内（未声明的一端不构成限制）。"""
    start, until = module_validity(module_id)
    return in_window(start, until, now)


def first_available_module_id(now: datetime | None = None) -> str | None:
    """注册顺序里第一个仍在有效期内的模块 id；全部不可用时返回 None。"""
    for mid in MODULE_REGISTRY:
        if module_available(mid, now):
            return mid
    return None


def get_module_info(module_id: str) -> dict:
    """获取模块元信息（声明有效期、过期判定与看板资格），模块不存在时抛出 KeyError(module_id)"""
    cls = MODULE_REGISTRY[module_id]
    valid_from, valid_until = module_validity(module_id)
    return {
        "id": module_id,
        "name": cls.NAME,
        "stages": cls.STAGE_ORDER,
        "requires": sorted(cls.REQUIRES),
        "requires_gamepad_exclusive": cls.REQUIRES_GAMEPAD_EXCLUSIVE,
        "required_assets": tuple(getattr(cls, "REQUIRED_ASSETS", ())),
        # 资格位：显式 HAS_DASHBOARD / 新契约（版本 > 0）。
        "has_dashboard": declares_dashboard(cls),
        "valid_from": valid_from.isoformat() if valid_from else None,
        "valid_until": valid_until.isoformat() if valid_until else None,
        "expired": module_expired(module_id),
    }


def get_plugin_dir(module_id: str) -> Path:
    """获取插件物理目录，模块不存在时抛出 KeyError(module_id)"""
    return _PLUGIN_DIRS[module_id]


def check_required_assets(module_id: str) -> list[str]:
    """检查插件 REQUIRED_ASSETS 声明的资源是否齐全，返回缺失项的完整路径列表。

    双根下按注册的物理目录定位（内置插件在内置根、用户插件在用户根），缺失项报绝对路径。
    """
    plugin_dir = get_plugin_dir(module_id)
    missing: list[str] = []
    for rel in get_module_info(module_id)["required_assets"]:
        if not (plugin_dir / rel).is_file():
            missing.append(str(plugin_dir / rel))
    return missing


def create_module(module_id: str, ctx: ActivityContext | None) -> ActivityModule:
    """创建模块实例，模块不存在时抛出 KeyError(module_id)

    ctx=None 为离线形态（只读默认配置，不启动运行时）；是否支持由模块自身声明。
    """
    return MODULE_REGISTRY[module_id](ctx)
