"""治理机制的反例回放：把六类腐化的可机检形态逐条还原，确认能被挡住。

只跑在实验区，不接入生产测试；不依赖原始记录（用合成的报告信封）。
"""

from __future__ import annotations

import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reconcile as R  # noqa: E402

ENVELOPE = {
    "caliber_version": "sr-base-v2",
    "input": {"trace": "speedrush.jsonl", "sha256_16": "deadbeefdeadbeef"},
    "params": {"lane_w_m": 3.5, "seg_gap_s": 0.2},
    "manual_inputs": [{"name": "manual_p1", "sha256_16": "cafebabecafebabe"}],
    "implementation_sha256_16": "0123456789abcdef",
    "mode_assignment": {"r1": "program", "r2": "manual"},
    "reversals": [{"label": "r1s1", "by_theta": {"0.1": {"per_s": 2.963}}}],
}


def _report():
    return copy.deepcopy(ENVELOPE)


def test_clean_recompute_matches():
    assert R.compare(_report(), _report()) == 0


def test_transcribed_value_is_detected():
    """数字转抄：结果值被改动一个量级，对账必须精确报出路径。"""
    saved = _report()
    fresh = _report()
    fresh["reversals"][0]["by_theta"]["0.1"]["per_s"] = 29.63
    assert R.compare(saved, fresh) == 1


def test_cross_caliber_is_not_comparable():
    """定义分叉：口径版本不同，不逐项比较，直接判不可比。"""
    saved = _report()
    fresh = _report()
    fresh["caliber_version"] = "sr-base-v3"
    assert R.compare(saved, fresh) == 2


def test_param_change_is_not_comparable():
    """阈值被悄悄改动：参数表不同即判不可比。"""
    saved = _report()
    fresh = _report()
    fresh["params"]["seg_gap_s"] = 0.5
    assert R.compare(saved, fresh) == 2


def test_implementation_or_input_change_is_not_comparable():
    """同一口径下换了实现或换了输入，也不能当同一份结果比较。"""
    saved = _report()
    for key, val in (("implementation_sha256_16", "ffffffffffffffff"),
                     ("input", {"trace": "speedrush.jsonl",
                                "sha256_16": "1111111111111111"})):
        fresh = _report()
        fresh[key] = val
        assert R.compare(saved, fresh) == 2


def test_anchor_resolves_and_missing_path_fails(tmp_path):
    """结论先行：方案里的数只能是可解析的锚点，写错即失败。"""
    plan = tmp_path / "plan.md"
    plan.write_text(
        '结论：目标反转 `{{R:reversals[0].by_theta["0.1"].per_s}}`\n'
        '错误引用：`{{R:reversals[9].by_theta["0.1"].per_s}}`\n',
        encoding="utf-8")
    assert R.check_plan(str(plan), _report()) == 1


def test_bare_decimal_in_plan_is_rejected(tmp_path):
    """数字脱离来源：方案里直接写小数、不走锚点，严格模式必须拒绝。"""
    plan = tmp_path / "plan.md"
    plan.write_text("目标反转 2.65 次/秒，人工路线 0.35 次/秒。\n", encoding="utf-8")
    assert R.check_plan(str(plan), _report(), strict=True) == 1


def test_anchored_plan_passes_strict(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text('反转率 `{{R:reversals[0].by_theta["0.1"].per_s}}` 次/秒。\n',
                    encoding="utf-8")
    assert R.check_plan(str(plan), _report(), strict=True) == 0
