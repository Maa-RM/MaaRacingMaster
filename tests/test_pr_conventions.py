# -*- coding: utf-8 -*-
"""PR 准入机检的判据守卫：命名正负例、体积软线、门禁自伤。

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
    name_problems,
    self_harm_problems,
    size_warnings,
)

WORKFLOW = ".github/workflows/test.yml"
TEST_FILE = "tests/test_demo.py"


def _diff(paths=("src/a.py",), added=10, removed=2, patch=""):
    return Diff(files=len(paths), added=added, removed=removed, paths=tuple(paths), patch=patch)


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


def test_self_harm_ignores_test_only_pr() -> None:
    patch = "-def test_old():\n-    assert fail\n"
    assert self_harm_problems(_diff(paths=(TEST_FILE,), added=0, removed=2, patch=patch)) == []


def test_self_harm_flags_workflow_plus_deleted_test() -> None:
    patch = "@@ -1,5 +1,2 @@\n-def test_old():\n-    assert True\n-def test_other():\n-    assert True\n+def test_new():\n+    assert True\n"
    problems = self_harm_problems(_diff(paths=(WORKFLOW, TEST_FILE), added=2, removed=5, patch=patch))
    assert any("删除 1 个测试用例" in p for p in problems)


def test_self_harm_flags_workflow_plus_added_skip() -> None:
    patch = "@@ -1,2 +1,3 @@\n+@pytest.mark.skipif(not sys.platform.startswith('win'), reason='windows only')\n def test_x():\n     assert True\n"
    problems = self_harm_problems(_diff(paths=(WORKFLOW, TEST_FILE), added=1, removed=0, patch=patch))
    assert any("skip" in p for p in problems)


def test_self_harm_quiet_when_tests_grow_alongside_workflow() -> None:
    patch = "@@ -1,1 +1,2 @@\n+def test_new_case():\n+    assert True\n"
    assert self_harm_problems(_diff(paths=(WORKFLOW, TEST_FILE), added=2, removed=0, patch=patch)) == []
