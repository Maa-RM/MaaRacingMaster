# -*- coding: utf-8 -*-
"""插件双根扫描：用户数据目录插件根与安装目录内置插件根并行加载。

- 用户插件根 = user_data_dir()/plugins（升级不触碰，用户复制的插件住这里）；
  内置根 = maaracing_master/plugins（随包发行，受管升级时整体替换）；
- 同 ID 冲突以内置为准（随包版本与产品契约绑定），用户根副本跳过并记 WARNING；
- 用户插件按独立目录整包装载（包内相对导入可用），无 __init__.py 也接受；
- 单插件加载失败不阻断其它插件（与内置根同一容错口径）。

不重载注册表模块（其它测试持有 MODULE_REGISTRY 同一对象）：直接清空再重扫，
teardown 恢复真实环境后重扫还原。
"""

from __future__ import annotations

import sys
import textwrap

import pytest

from maaracing_master.core import registry as reg

_MANIFEST_TEMPLATE = textwrap.dedent(
    """\
    # -*- coding: utf-8 -*-
    from maaracing_master.core.base import ActivityModule

    ID = "{mod_id}"
    MODULE_CLASS = "manifest.PluginClass"

    class PluginClass(ActivityModule):
        NAME = "{mod_id}"
        STAGE_ORDER = ()
        REQUIRES = frozenset()
        REQUIRES_GAMEPAD_EXCLUSIVE = False
    """
)


def _make_plugin(plugins_dir, mod_id: str, manifest_text: str | None = None):
    """在指定插件根下造一个最小可注册插件目录，返回目录路径。"""
    d = plugins_dir / mod_id
    d.mkdir(parents=True)
    text = manifest_text if manifest_text is not None else _MANIFEST_TEMPLATE.format(mod_id=mod_id)
    (d / "manifest.py").write_text(text, encoding="utf-8")
    return d


@pytest.fixture
def rescan(monkeypatch, tmp_path):
    """APPDATA 指向临时目录后清空重扫；teardown 恢复环境并重扫还原。

    返回 (registry_module, user_plugins_dir)，后者尚不存在（用例自行创建）。
    """
    monkeypatch.setenv("APPDATA", str(tmp_path))

    def _rescan():
        reg.MODULE_REGISTRY.clear()
        reg._PLUGIN_DIRS.clear()
        reg._PLUGIN_VALIDITY.clear()
        reg._discover_plugins()

    _rescan()
    yield reg, tmp_path / "MaaRacingMaster" / "plugins"
    monkeypatch.undo()
    _rescan()


def test_user_plugin_registered(rescan):
    reg, user_root = rescan
    _make_plugin(user_root, "userplug_a")
    reg._discover_plugins()

    assert "userplug_a" in reg.MODULE_REGISTRY
    assert reg.get_plugin_dir("userplug_a") == user_root / "userplug_a"
    # 内置插件不受影响
    assert "infinite_drive" in reg.MODULE_REGISTRY


def test_user_plugin_without_init_py(rescan):
    """用户插件目录没有 __init__.py 也能整包装载（用户手工拷贝的常态形态）。"""
    reg, user_root = rescan
    d = _make_plugin(user_root, "userplug_b")
    assert not (d / "__init__.py").exists()

    reg._discover_plugins()
    assert "userplug_b" in reg.MODULE_REGISTRY


def test_user_plugin_relative_import(rescan):
    """MODULE_CLASS 指向插件内子模块（相对导入形态）时也能定位。"""
    reg, user_root = rescan
    d = _make_plugin(
        user_root,
        "userplug_c",
        textwrap.dedent(
            """\
            # -*- coding: utf-8 -*-
            ID = "userplug_c"
            MODULE_CLASS = "module.PluginClass"
            """
        ),
    )
    (d / "module.py").write_text(
        textwrap.dedent(
            """\
            # -*- coding: utf-8 -*-
            from maaracing_master.core.base import ActivityModule

            class PluginClass(ActivityModule):
                NAME = "userplug_c"
                STAGE_ORDER = ()
                REQUIRES = frozenset()
                REQUIRES_GAMEPAD_EXCLUSIVE = False
            """
        ),
        encoding="utf-8",
    )

    reg._discover_plugins()
    assert "userplug_c" in reg.MODULE_REGISTRY


def test_builtin_wins_same_id(rescan):
    """用户根出现与内置同 ID 的插件：内置版本为准，用户副本不注册。"""
    reg, user_root = rescan
    _make_plugin(user_root, "infinite_drive")  # 与内置插件同 ID
    reg._discover_plugins()

    assert "infinite_drive" in reg.MODULE_REGISTRY
    # 注册的物理目录必须仍是内置根
    builtin_root = reg._PLUGINS_ROOT
    assert reg.get_plugin_dir("infinite_drive").is_relative_to(builtin_root)


def test_user_plugin_load_failure_isolated(rescan):
    """用户插件清单损坏只跳过自身，其余插件照常注册。"""
    reg, user_root = rescan
    _make_plugin(user_root, "userplug_broken", manifest_text="ID = 'userplug_broken'\nraise RuntimeError('坏插件')\n")
    _make_plugin(user_root, "userplug_ok")
    reg._discover_plugins()

    assert "userplug_broken" not in reg.MODULE_REGISTRY
    assert "userplug_ok" in reg.MODULE_REGISTRY


def test_user_plugin_broken_init_isolated_and_cache_cleaned(rescan):
    """包初始化失败（__init__.py 抛异常/语法错误）与注册同一容错边界：
    只跳过自身，不阻断扫描（更不阻断后端启动）；失败的导入缓存必须清理，
    修复后的插件下次扫描可重新装载。"""
    reg, user_root = rescan
    d = _make_plugin(user_root, "userplug_badinit")
    (d / "__init__.py").write_text("raise RuntimeError('包初始化即炸')\n", encoding="utf-8")
    _make_plugin(user_root, "userplug_ok")

    reg._discover_plugins()  # 不得抛异常冒泡出扫描

    assert "userplug_badinit" not in reg.MODULE_REGISTRY
    assert "userplug_ok" in reg.MODULE_REGISTRY
    broken_pkg = f"{reg._USER_PLUGIN_PACKAGE}.userplug_badinit"
    assert broken_pkg not in sys.modules, "失败插件的半成品模块不得残留导入缓存"

    # 用户修复插件后（去掉坏 __init__）：下次扫描可正常装载
    (d / "__init__.py").unlink()
    reg._discover_plugins()
    assert "userplug_badinit" in reg.MODULE_REGISTRY


def test_user_plugin_failed_init_clears_submodule_cache(rescan):
    """包初始化先导入辅助模块再失败：失败插件本次导入的全部子模块都不得残留导入缓存
    （只清父包不够——同进程修复重扫会读到旧辅助模块），修复后重扫装载新内容。"""
    reg, user_root = rescan
    d = _make_plugin(user_root, "userplug_submod")
    (d / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    (d / "__init__.py").write_text(
        "from . import helper\nraise RuntimeError('导入辅助模块后炸')\n", encoding="utf-8")

    reg._discover_plugins()  # 不抛异常冒泡
    assert "userplug_submod" not in reg.MODULE_REGISTRY

    pkg_prefix = f"{reg._USER_PLUGIN_PACKAGE}.userplug_submod"
    leaked = [k for k in sys.modules if k == pkg_prefix or k.startswith(pkg_prefix + ".")]
    assert leaked == [], f"失败插件的模块残留导入缓存: {leaked}"

    # 用户修复插件（辅助模块内容变更）：同一进程重扫装载新内容，不读到旧缓存
    (d / "__init__.py").write_text("from . import helper\n", encoding="utf-8")
    (d / "helper.py").write_text("VALUE = 2\n", encoding="utf-8")
    reg._discover_plugins()
    assert "userplug_submod" in reg.MODULE_REGISTRY
    helper = sys.modules[f"{reg._USER_PLUGIN_PACKAGE}.userplug_submod.helper"]
    assert helper.VALUE == 2, "重扫不得复用失败轮残留的旧辅助模块"


def test_user_root_missing_is_fine(rescan):
    """用户数据目录还没有插件根（首次启动常态）：扫描不报错，内置照常。"""
    reg, user_root = rescan
    assert not user_root.exists()

    reg._discover_plugins()
    assert "infinite_drive" in reg.MODULE_REGISTRY


def test_user_plugin_assets_checked_at_user_root(rescan):
    """REQUIRED_ASSETS 资源存在性校验定位到用户插件目录（双根之一）。"""
    reg, user_root = rescan
    _make_plugin(
        user_root,
        "userplug_assets",
        textwrap.dedent(
            """\
            # -*- coding: utf-8 -*-
            from maaracing_master.core.base import ActivityModule

            ID = "userplug_assets"
            MODULE_CLASS = "manifest.PluginClass"

            class PluginClass(ActivityModule):
                NAME = "userplug_assets"
                STAGE_ORDER = ()
                REQUIRES = frozenset()
                REQUIRES_GAMEPAD_EXCLUSIVE = False
                REQUIRED_ASSETS = ("resources/model.bin",)
            """
        ),
    )
    reg._discover_plugins()

    missing = reg.check_required_assets("userplug_assets")
    assert len(missing) == 1
    # 缺失项按声明的物理位置报：用户插件目录内
    assert str(user_root / "userplug_assets" / "resources" / "model.bin") in missing[0]
