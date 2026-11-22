# -*- coding: utf-8 -*-
"""Pipeline 校验器的识别引用、动作和纯锚点契约。"""
from __future__ import annotations

from tools.pipeline import truth as ct


def test_and_or_refs_collected_across_forms():
    """and_or_refs 必须同时吃 v2 嵌套、v1 平铺、以及内联子识别里再套一层 And/Or。"""
    v2 = {"recognition": {"type": "Or", "param": {"any_of": [
        "sig.a",
        {"recognition": {"type": "And", "param": {"all_of": ["sig.b"]}}}]}}}
    v1 = {"recognition": "Or", "any_of": ["sig.c"]}
    assert ct.and_or_refs(v2) == ["sig.a", "sig.b"]
    assert ct.and_or_refs(v1) == ["sig.c"]
    assert ct.and_or_refs({"recognition": "DirectHit"}) == []
    # 内联 Custom 参数不是引用，不得被误收
    inline = {"recognition": {"type": "Or", "param": {"any_of": [
        {"recognition": {"type": "Custom", "param": {
            "custom_recognition": "MaaRM_Template",
            "custom_recognition_param": {"templates": ["a.png"]}}}}]}}}
    assert ct.and_or_refs(inline) == []


def test_and_or_dangling_ref_is_error():
    """And/Or 子项写错门牌必须在这里红——框架不拦，运行期只静默失败。"""
    fake = {"入口": {"recognition": {"type": "Or", "param": {"any_of": ["不存在的节点"]}},
                     "next": ["尾"]},
            "尾": {"attach": {"_dwell": True}}}
    errors, _ = ct.validate_graph(fake)
    assert any("And/Or 子项悬空引用" in e for e in errors), errors


def test_anchor_ref_inside_and_or_is_error():
    """`[Anchor]` 出现在 And/Or 子项 = 按字面节点名解析的死引用，必须报并说明原因。"""
    fake = {"设锚": {"recognition": "DirectHit", "anchor": "回", "next": ["借锚"]},
            "借锚": {"recognition": {"type": "Or", "param": {"any_of": ["[Anchor]回"]}}}}
    errors, _ = ct.validate_graph(fake)
    assert any("And/Or 子项悬空引用" in e and "[Anchor]" in e for e in errors), errors


def test_builtin_input_action_is_error():
    """内置输入 action 打在我们的占位控制器上=假成功，v1/v2 两形态都要拦。"""
    errors = ct.action_checks({
        "点一下": {"action": "Click"},
        "按键": {"action": {"type": "ClickKey", "param": {"key_list": [27]}}},
    })
    assert len(errors) == 2, errors
    assert all("假成功" in e for e in errors), errors
    # Custom 与无副作用的内置类型不受影响
    assert ct.action_checks({"正常": {"action": {"type": "Custom", "param": {}}},
                             "停手": {"action": "StopTask"},
                             "什么都不做": {}}) == []


def test_anchor_only_needs_no_route_but_needs_a_referrer():
    """纯锚点：零路由不报「不可达/无出口」，但没人消费就是孤儿真源。

    消费通道两种：图内按名引用；模块按节点名直驱（attach._module_driven 显式声明
    ——图级引用分析看不见模块侧的 post_task 调用，豁免必须落在真源声明上）。"""
    anchored = {"入口": {"attach": {"_entry": True}, "next": ["公共.聊天框"]},
                "公共.聊天框": {"attach": {"_anchor_only": True},
                                 "recognition": "DirectHit"}}
    errors, warns = ct.validate_graph(anchored)
    assert errors == [], errors
    assert warns == [], warns

    orphan = {"入口": {"attach": {"_entry": True}, "next": ["尾"]},
              "尾": {"attach": {"_dwell": True}},
              "公共.聊天框": {"attach": {"_anchor_only": True}, "recognition": "DirectHit"}}
    errors, _ = ct.validate_graph(orphan)
    assert any("孤儿真源" in e for e in errors), errors

    # 模块直驱通道：声明 _module_driven 即视为已被消费
    driven = {"入口": {"attach": {"_entry": True}, "next": ["尾"]},
              "尾": {"attach": {"_dwell": True}},
              "公共.聊天框": {"attach": {"_anchor_only": True, "_module_driven": True},
                               "recognition": "DirectHit"}}
    errors, _ = ct.validate_graph(driven)
    assert errors == [], errors


def test_anchor_only_must_stay_route_free():
    """纯锚点带 next 就不是纯锚点了——搬进通用层时会点名业务层，当场拦。"""
    fake = {"入口": {"attach": {"_entry": True}, "next": ["公共.聊天框"]},
            "公共.聊天框": {"attach": {"_anchor_only": True},
                             "recognition": "DirectHit", "next": ["尾"]},
            "尾": {"attach": {"_dwell": True}}}
    errors, _ = ct.validate_graph(fake)
    assert any("纯锚点必须零路由" in e for e in errors), errors
