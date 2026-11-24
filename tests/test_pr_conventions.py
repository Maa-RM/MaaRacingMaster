# -*- coding: utf-8 -*-
"""PR 准入机检的判据守卫：命名正负例、体积软线、门禁自伤、AST 计数与快照口径。

判据口径见 [ADR-0028](../docs/adr/0028-arch-global-分支模型定为单主干与短命主题分支.md)，
实现见 `tools/pr_conventions.py`。纯判定函数不跑 git；快照口径（合并基 / testpaths /
重命名）用临时仓库 fixture 验证。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.pr_conventions import (  # noqa: E402
    CHECKER_PATH,
    Diff,
    is_gate_path,
    judge_self_harm,
    name_problems,
    parse_numstat_path,
    size_warnings,
    test_metrics as count_metrics,
    collection_config as read_config,
    test_subtraction as count_subtraction,
)

WORKFLOW = ".github/workflows/test.yml"

PYPROJECT = (
    "[tool.pytest.ini_options]\n"
    'testpaths = ["tests", "plugin/tests"]\n'
    'python_files = ["test_*.py", "*_test.py"]\n'
)
TWO_TESTS = "def test_a():\n    assert True\n\ndef test_b():\n    assert True\n"
ONE_TEST = "def test_a():\n    assert True\n"


def _diff(paths=("src/a.py",), added=10, removed=2):
    return Diff(files=len(paths), added=added, removed=removed, changes=tuple((p, p) for p in paths))


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=guard@example.invalid",
            "-c",
            "user.name=guard",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


def _write(repo: Path, rel: str, text: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _commit(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """base 快照：pyproject 两个收集根 + 根测试与插件测试各 2 用例。"""
    _git(tmp_path, "init", "-q", "-b", "master")
    _write(tmp_path, "pyproject.toml", PYPROJECT)
    _write(tmp_path, "tests/test_a.py", TWO_TESTS)
    _write(tmp_path, "plugin/tests/test_p.py", TWO_TESTS)
    _commit(tmp_path, "base")
    return tmp_path


def _branch(repo: Path, name: str = "topic") -> None:
    _git(repo, "checkout", "-q", "-b", name)


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


def test_gate_covers_checker_and_lock_not_src() -> None:
    assert is_gate_path(CHECKER_PATH)
    assert is_gate_path(".github/workflows/actions.lock")
    assert not is_gate_path("src/a.py")
    assert not is_gate_path("tests/test_pr_conventions.py")


def test_self_harm_flags_checker_plus_deleted_test() -> None:
    # 改弱检查器 + 删用例与改 workflow 同罪：检查器本身就是门禁定义
    problems = judge_self_harm((CHECKER_PATH,), removed_tests=1, added_skipped=0)
    assert any("门禁自伤" in p for p in problems)


@pytest.mark.parametrize(
    ("raw", "old", "new"),
    [
        ("tests/test_a.py", "tests/test_a.py", "tests/test_a.py"),
        ("tests/old.py => tests/new.py", "tests/old.py", "tests/new.py"),
        ("tests/{old => new}.py", "tests/old.py", "tests/new.py"),
        ("{a => b}/test_x.py", "a/test_x.py", "b/test_x.py"),
    ],
)
def test_parse_numstat_path(raw: str, old: str, new: str) -> None:
    assert parse_numstat_path(raw) == (old, new)


def test_subtraction_uses_merge_base_not_base_tip(repo: Path) -> None:
    # 回归：主干在 PR 分叉后往同文件加用例，不得算成 PR 删除
    _branch(repo)
    _write(repo, "tests/test_a.py", TWO_TESTS.replace("assert True", "assert 1 == 1"))
    _commit(repo, "topic touches same file")
    _git(repo, "checkout", "-q", "master")
    _write(repo, "tests/test_a.py", TWO_TESTS + "\ndef test_c():\n    assert True\n")
    _commit(repo, "master adds a test")
    removed, added_skip = count_subtraction(repo, "master", "topic", (("tests/test_a.py", "tests/test_a.py"),))
    assert (removed, added_skip) == (0, 0)


def test_subtraction_covers_plugin_testpaths(repo: Path) -> None:
    # 回归：pytest 收集根含插件测试目录，只扫 tests/ 会漏
    _branch(repo)
    _write(repo, "plugin/tests/test_p.py", ONE_TEST)
    _commit(repo, "drop plugin test")
    removed, _ = count_subtraction(repo, "master", "topic", (("plugin/tests/test_p.py", "plugin/tests/test_p.py"),))
    assert removed == 1


def test_subtraction_counts_new_skip_in_plugin_dir(repo: Path) -> None:
    _branch(repo)
    _write(repo, "plugin/tests/test_p.py", "import pytest\n\npytestmark = pytest.mark.skip(reason='x')\n" + TWO_TESTS)
    _commit(repo, "skip plugin tests")
    _, added_skip = count_subtraction(repo, "master", "topic", (("plugin/tests/test_p.py", "plugin/tests/test_p.py"),))
    assert added_skip == 2


def test_subtraction_sees_rename_with_deletion(repo: Path) -> None:
    # 回归：numstat 的 old => new 路径若不解析，改名伴随的删用例完全漏检
    _branch(repo)
    _git(repo, "mv", "tests/test_a.py", "tests/test_renamed.py")
    _write(repo, "tests/test_renamed.py", ONE_TEST)
    _commit(repo, "rename and drop one test")
    removed, _ = count_subtraction(repo, "master", "topic", (("tests/test_a.py", "tests/test_renamed.py"),))
    assert removed == 1


def test_subtraction_silent_on_pure_rename(repo: Path) -> None:
    _branch(repo)
    _git(repo, "mv", "tests/test_a.py", "tests/test_renamed.py")
    _commit(repo, "pure rename")
    removed, added_skip = count_subtraction(repo, "master", "topic", (("tests/test_a.py", "tests/test_renamed.py"),))
    assert (removed, added_skip) == (0, 0)


def test_subtraction_counts_test_moved_out_of_collection(repo: Path) -> None:
    # 回归：测试改成普通辅助文件后 pytest 不再收集，旧实现仍按两侧源码计数、报 0 减法
    _branch(repo)
    (repo / "tests/helpers").mkdir(exist_ok=True)
    _git(repo, "mv", "tests/test_a.py", "tests/helpers/check_a.py")
    _commit(repo, "move out of collection scope")
    removed, _ = count_subtraction(repo, "master", "topic", (("tests/test_a.py", "tests/helpers/check_a.py"),))
    assert removed == 2


def test_subtraction_counts_helper_moved_into_collection(repo: Path) -> None:
    _write(repo, "tests/helpers/check_b.py", "import pytest\n\npytestmark = pytest.mark.skip(reason='x')\n" + TWO_TESTS)
    _commit(repo, "base has non-collected helper")
    _branch(repo)
    _git(repo, "mv", "tests/helpers/check_b.py", "tests/test_b.py")
    _commit(repo, "move into collection scope")
    removed, added_skip = count_subtraction(repo, "master", "topic", (("tests/helpers/check_b.py", "tests/test_b.py"),))
    assert (removed, added_skip) == (0, 2)


def test_subtraction_sees_collection_root_dropped(repo: Path) -> None:
    # 回归：只改收集配置、不改测试文件——旧实现按并集看文件，删收集根完全漏检
    _branch(repo)
    _write(repo, "pyproject.toml", PYPROJECT.replace(', "plugin/tests"', ""))
    _commit(repo, "drop plugin test root")
    removed, _ = count_subtraction(repo, "master", "topic", (("pyproject.toml", "pyproject.toml"),))
    assert removed == 2


def test_subtraction_sees_collection_root_added_with_skips(repo: Path) -> None:
    repo_extra = "extra/tests/test_e.py"
    _write(repo, repo_extra, "import pytest\n\npytestmark = pytest.mark.skip(reason='x')\n" + TWO_TESTS)
    _commit(repo, "base has non-collected extra tests")
    _branch(repo)
    _write(repo, "pyproject.toml", PYPROJECT.replace('"plugin/tests"', '"plugin/tests", "extra/tests"'))
    _commit(repo, "add extra root")
    _, added_skip = count_subtraction(repo, "master", "topic", (("pyproject.toml", "pyproject.toml"),))
    assert added_skip == 2


def test_config_fallback_without_pyproject(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "master")
    _write(tmp_path, "tests/test_a.py", ONE_TEST)
    _commit(tmp_path, "no pyproject")
    roots, patterns = read_config(tmp_path, "master")
    assert roots == ("tests",)
    assert "test_*.py" in patterns