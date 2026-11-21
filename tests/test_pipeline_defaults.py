# -*- coding: utf-8 -*-
"""共享 pipeline 默认值（default_pipeline.json）经加载边界的回归锁。

maafw 5.12.3 没有 DefaultPipelineMgr——顶层 ``Default`` 键会被当普通节点注册、
裸节点照吃协议缺省四件套（pre/post_delay 各 200ms、rate_limit 1000ms、timeout
20s，2026-11-21 实证）。套用因此收在 core/nav_graph 的合并 post 边界，以 Python
侧完成。本测试用真实框架 Resource + 仓库真源默认值文件走完整加载，锁三件事：

1. 缺省节点吃到共享默认值（不再是协议陷阱值）；
2. 显式节点一个字段都不被覆盖；
3. ``Default`` 键不残留成节点。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip("maa")

from maa.resource import Resource  # noqa: E402

from maaracing_master.core.nav_graph import (  # noqa: E402
    CORE_RES_DIR,
    DEFAULT_PIPELINE_FILENAME,
    _post_pipeline_merged,
)

DEFAULTS_PATH = CORE_RES_DIR / "pipeline" / DEFAULT_PIPELINE_FILENAME


def _load_defaults_fields() -> dict:
    doc = json.loads(DEFAULTS_PATH.read_text(encoding="utf-8-sig"))
    fields = doc["Default"]
    assert fields, "默认值文件必须提供顶层 Default 字段表"
    return fields


def test_shared_defaults_applied_at_load_boundary(tmp_path: Path) -> None:
    """裸节点吃默认值、显式节点不被覆盖、Default 键不成节点——经真实加载边界。"""
    fields = _load_defaults_fields()
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy2(DEFAULTS_PATH, src / DEFAULT_PIPELINE_FILENAME)
    (src / "nodes.json").write_text(json.dumps({
        "t.bare": {"recognition": {"type": "DirectHit"},
                   "action": {"type": "DoNothing"}},
        "t.explicit": {"recognition": {"type": "DirectHit"},
                       "action": {"type": "DoNothing"},
                       "pre_delay": 0, "post_delay": 0,
                       "rate_limit": 0, "timeout": 400},
    }, ensure_ascii=False), encoding="utf-8")

    res = Resource()
    assert _post_pipeline_merged(res, [src], "测试"), "合并 post 应成功"

    bare = res.get_node_data("t.bare")
    assert bare is not None
    for key, val in fields.items():
        assert bare[key] == val, f"裸节点缺省字段 {key} 应吃共享默认值 {val}"

    explicit = res.get_node_data("t.explicit")
    assert explicit is not None
    assert explicit["timeout"] == 400 and explicit["rate_limit"] == 0
    assert explicit["pre_delay"] == 0 and explicit["post_delay"] == 0

    assert res.get_node_data("Default") is None, "Default 键不得残留为节点"


def test_no_defaults_file_keeps_fast_path(tmp_path: Path) -> None:
    """无默认值文件的单目录 post 照旧直传（启动链等自有目录不受影响）。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "nodes.json").write_text(json.dumps({
        "t.x": {"recognition": {"type": "DirectHit"},
                "action": {"type": "DoNothing"}},
    }), encoding="utf-8")

    res = Resource()
    assert _post_pipeline_merged(res, [src], "测试")
    assert res.get_node_data("t.x") is not None


def test_duplicate_defaults_files_rejected(tmp_path: Path) -> None:
    """两份默认值文件 = 双真源，加载边界必须拒绝而不是悄悄二选一。"""
    a = tmp_path / "a"
    b = tmp_path / "b"
    for d in (a, b):
        d.mkdir()
        shutil.copy2(DEFAULTS_PATH, d / DEFAULT_PIPELINE_FILENAME)
        (d / "nodes.json").write_text(json.dumps({
            f"t.{d.name}": {"recognition": {"type": "DirectHit"},
                            "action": {"type": "DoNothing"}},
        }), encoding="utf-8")

    res = Resource()
    with pytest.raises(ValueError, match="default_pipeline.json 双真源"):
        _post_pipeline_merged(res, [a, b], "测试")
