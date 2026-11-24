# -*- coding: utf-8 -*-
"""PR 准入机检的判据守卫：命名正负例、体积软线、门禁自伤与 AST 计数。

判据口径见 [ADR-0028](../docs/adr/0028-arch-global-分支模型定为单主干与短命主题分支.md)，
实现见 `tools/pr_conventions.py`。这里只验证判定函数，不跑 git。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.pr_conventions import (  # noqa: E402
    Diff,
    judge_self_harm,
    name_problems,
    size_warnings,
    test_metrics as count_metrics,
)

WORKFLOW = ".github/workflows/test.yml"


def _diff(paths=("src/a.py",), added=10, removed=2):
    return Diff(files=len(paths), added=added, removed=removed, paths=tuple(paths))


@pytest.mark.parametrize(
    "ref",
    [
        "feat/branch-conventions",
        "fix/settlement-rounding",
        "chore/repo-branch-model",
        "hotfix/launcher-crash",
        "release/v0.32.2-dev.2",
        "chore/actions-bump-pkgtest",
    ],
)
def test_naming_accepts_conventional_branches(ref: str) -> None:
    assert name_problems(ref) == []


@pytest.mark.parametrize(
    "ref",
    [
        "dev/d542Bb",
        "feat/Foo",
        "feat/with_underscore",
        "features/new-thing",
        "feat/",
        "feat/-leading",
        "master",
    ],
)
def test_naming_rejects_other_shapes(ref: str) -> None:
    assert name_problems(ref), f"{ref} 应被判不合规"


def test_naming_exempts_dependabot() -> None:
    assert name_problems("dependabot/github_actions/github-actions-2027506fc5") == []


def test_empty_head_ref_is_rejected() -> None:
    assert name_problems("") != []


def test_size_warnings_silent_under_soft_lines() -> None:
    assert size_warnings(_diff(paths=("a.py", "b.py"), added=300, removed=50)) == []


def test_size_warnings_on_lines_and_files() -> None:
    warns = size_warnings(_diff(paths=tuple(f"f{i}.py" for i in range(25)), added=400, removed=100))
    assert len(warns) == 2
    assert any("行" in w for w in warns) and any("文件" in w for w in warns)


def test_metrics_counts_plain_tests() -> None:
    src = "def test_a():\n    assert True\n\ndef helper():\n    pass\n"
    m = count_metrics(src)
    assert (m.tests, m.skipped) == (1, 0)


def test_metrics_counts_skip_decorators() -> None:
    src = (
        "import pytest\n\n"
        "@pytest.mark.skip(reason='x')\n"
        "def test_a():\n    assert True\n\n"
        "@pytest.mark.skipif(condition, reason='y')\n"
        "def test_b():\n    assert True\n\n"
        "def test_c():\n    assert True\n"
    )
    m = count_metrics(src)
    assert (m.tests, m.skipped) == (3, 2)


def test_metrics_counts_module_pytestmark() -> None:
    src = "import pytest\n\npytestmark = pytest.mark.skip(reason='whole module')\n\ndef test_a():\n    assert True\n"
    m = count_metrics(src)
    assert (m.tests, m.skipped) == (1, 1)


def test_metrics_ignores_skip_text_inside_literals() -> None:
    # 回归：本条机检首次上线时被自己的夹具误伤——字符串里出现 skip 字面量不等于新增跳过
    src = 'PATCH = "@pytest.mark.skipif(cond, reason=1)\\n-def test_old():\\n+ pass\\n"\n\ndef test_a():\n    assert True\n'
    m = count_metrics(src)
    assert (m.tests, m.skipped) == (1, 0)


def test_metrics_tolerates_syntax_error() -> None:
    metrics = count_metrics("def broken(:\n")
    assert metrics.tests == 0


def test_self_harm_ignores_test_only_pr() -> None:
    assert judge_self_harm((), removed_tests=3, added_skipped=2) == []


def test_self_harm_flags_workflow_plus_deleted_test() -> None:
    problems = judge_self_harm((WORKFLOW,), removed_tests=1, added_skipped=0)
    assert any("删除 1 个测试用例" in p for p in problems)


def test_self_harm_flags_workflow_plus_new_skip() -> None:
    problems = judge_self_harm((WORKFLOW,), removed_tests=0, added_skipped=2)
    assert any("2 个被跳过的用例" in p for p in problems)


def test_self_harm_quiet_without_subtraction() -> None:
    assert judge_self_harm((WORKFLOW,), removed_tests=0, added_skipped=0) == []

