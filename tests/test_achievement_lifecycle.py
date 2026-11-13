# -*- coding: utf-8 -*-
"""成就生命周期：剥离（A5，新进程重载）与定义变更（A6：增删条目 / 重装 / ID 不复用）。

契约见 core/achievement.py 模块 docstring。这里**不声明任何业务成就**：夹具目录是临时
沙盘，只验证「删目录 + 新进程 → 成就不再列出、进度作为非活跃数据保留」。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from maaracing_master.core import achievement as a

REPO = Path(__file__).resolve().parent.parent

# 子进程脚本：只走「扫描 → 注册目录 → 构造引擎 → 导出视图」，不构造任何模块实例。
_SCAN_SCRIPT = """
import json, sys, types
from pathlib import Path

sys.path.insert(0, sys.argv[3])
from maaracing_master.core import achievement as a, registry as reg

root, store = Path(sys.argv[1]), Path(sys.argv[2])
a.register_core_catalog()
for d in (sorted(root.iterdir()) if root.is_dir() else []):
    if (d / "achievements.json").is_file():
        reg._register_achievements(d.name, d,
                                   types.SimpleNamespace(ACHIEVEMENTS="achievements.json"))
eng = a.AchievementEngine(store_path=store)
view = eng.get_view()
print(json.dumps({
    "mods": sorted(a.ACHIEVEMENT_REGISTRY),
    "view": [v["global_id"] for v in view],
    "unlocked": [v["global_id"] for v in view if v["unlocked"]],
}))
"""


def _run_fresh_process(plugin_root: Path, store: Path) -> dict:
    """起一个全新 Python 进程跑扫描 + 引擎（模拟 sidecar 重启）。"""
    proc = subprocess.run(
        [sys.executable, "-c", _SCAN_SCRIPT, str(plugin_root), str(store), str(REPO)],
        cwd=str(REPO), capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _fixture_plugin(root: Path) -> Path:
    plugin = root / "fixture"
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "achievements.json").write_text(json.dumps({
        "schema_version": 1,
        "achievements": [{
            "id": "first", "title": "夹具成就", "description": "只为验证生命周期",
            "rule": {"fact": "fixture.run", "agg": "count", "target": 1},
        }],
    }, ensure_ascii=False), encoding="utf-8")
    return plugin


@pytest.fixture()
def isolated_registry():
    saved = dict(a.ACHIEVEMENT_REGISTRY)
    a.ACHIEVEMENT_REGISTRY.clear()
    try:
        yield a.ACHIEVEMENT_REGISTRY
    finally:
        a.ACHIEVEMENT_REGISTRY.clear()
        a.ACHIEVEMENT_REGISTRY.update(saved)


# ---------- A5 剥离：删目录 + 新进程 ----------

def test_a5_uninstall_hides_achievements_and_keeps_progress(tmp_path):
    plugin_root = tmp_path / "plugins"
    plugin = _fixture_plugin(plugin_root)
    store = tmp_path / "achievements.json"
    store.write_text(json.dumps({
        "store_schema_version": a.ACHIEVEMENT_STORE_SCHEMA_VERSION,
        "progress": {"fixture.first": 1},
        "unlocked": {"fixture.first": 1700000000000000000},
        "consumed_keys": ["fixture::run::1"],
    }, ensure_ascii=False), encoding="utf-8")

    before = _run_fresh_process(plugin_root, store)
    assert "fixture" in before["mods"]
    assert "fixture.first" in before["view"]
    assert before["unlocked"] == ["fixture.first"]
    assert "core.first_start" in before["view"]

    # 删目录 = 卸载；新进程重载后该插件的成就消失，其它来源不受影响
    for child in sorted(plugin.iterdir()):
        child.unlink()
    plugin.rmdir()

    after = _run_fresh_process(plugin_root, store)
    assert "fixture" not in after["mods"]
    assert "fixture.first" not in after["view"]
    assert "core.first_start" in after["view"]           # 核心成就照常
    assert after["unlocked"] == []                        # 非活跃数据不展示

    # 进度本身保留（版本回退/重装后还在），只是不展示
    kept = json.loads(store.read_text(encoding="utf-8"))
    assert kept["progress"]["fixture.first"] == 1
    assert "fixture.first" in kept["unlocked"]


# ---------- A6 定义变更 ----------

def _engine(tmp_path, *entries, mod_id="demo"):
    norm, _ = a.validate_catalog(
        {"schema_version": a.ACHIEVEMENT_CATALOG_SCHEMA_VERSION,
         "achievements": list(entries)}, mod_id)
    return a.AchievementEngine(store_path=tmp_path / "ach.json", registry={mod_id: norm})


def _entry(local_id, fact="demo.run", **over):
    base = {"id": local_id, "title": f"标题{local_id}", "description": "描述",
            "rule": {"fact": fact, "agg": "count", "target": 1}}
    base.update(over)
    return base


def test_a6_new_entry_starts_from_zero(tmp_path):
    eng = _engine(tmp_path, _entry("old"))
    eng.process_fact("demo.run", "k1", {})
    assert eng.get_view()[0]["unlocked"] is True

    eng2 = _engine(tmp_path, _entry("old"), _entry("new", fact="demo.other"))
    view = {v["global_id"]: v for v in eng2.get_view()}
    assert view["demo.old"]["unlocked"] is True          # 已解锁不撤销
    assert view["demo.new"]["progress"] == 0             # 新增从 0 起
    assert view["demo.new"]["unlocked"] is False


def test_a6_removed_entry_hidden_but_progress_kept(tmp_path):
    _engine(tmp_path, _entry("gone")).process_fact("demo.run", "k1", {})
    eng = _engine(tmp_path)                              # 条目被删：目录为空
    assert eng.get_view() == []
    kept = json.loads((tmp_path / "ach.json").read_text(encoding="utf-8"))
    assert kept["progress"]["demo.gone"] == 1            # 归档不展示，但没被清掉
    assert "demo.gone" in kept["unlocked"]


def test_a6_reinstall_keeps_unlocked_state(tmp_path):
    _engine(tmp_path, _entry("keep")).process_fact("demo.run", "k1", {})
    _engine(tmp_path)                                    # 中间卸载一轮
    eng = _engine(tmp_path, _entry("keep"))              # 重装
    view = eng.get_view()[0]
    assert view["unlocked"] is True and view["progress"] == 1


def test_a6_new_id_does_not_inherit_old_progress(tmp_path):
    """ID 不复用：新 ID 即使规则相同也从 0 起，旧 ID 的进度不迁移。"""
    _engine(tmp_path, _entry("v1")).process_fact("demo.run", "k1", {})
    eng = _engine(tmp_path, _entry("v2"))                # 不兼容变更 → 新 ID
    view = eng.get_view()[0]
    assert view["global_id"] == "demo.v2"
    assert view["progress"] == 0 and view["unlocked"] is False
