# -*- coding: utf-8 -*-
"""PR 准入机检：分支命名（硬拦）、体积（只警告）、门禁自伤（硬拦）。

口径来自 [ADR-0028](../docs/adr/0028-arch-global-分支模型定为单主干与短命主题分支.md) 决策 5：

- **命名**：头分支必须匹配 `type/slug`（`feat` / `fix` / `chore` / `hotfix` / `release`，
  slug 只用 `a-z0-9.-`）；`dependabot/**` 是机器生成分支，豁免。这一条零歧义，硬拦。
- **体积**：超过 `SIZE_LINE_SOFT` 行或 `SIZE_FILE_SOFT` 文件**只警告**。本仓库自己的收口 PR
  就会越线，硬门槛只会逼出碎 PR 与「藏进顺手格式化」两类规避。
- **门禁自伤**：同一个 PR 既改门禁定义（`.github/workflows/**` 含锁文件、或本检查器）、
  又在测试上做减法时硬拦并要求人工复核——它挡的是「为变绿改门禁」。只改门禁定义不做
  减法时给警告，提示合入前需人工复核。

测试减法的判据：

- **收集范围**按两侧各自的 pytest 配置判定：合并基侧用合并基的 `testpaths` / `python_files`，
  PR 快照侧用快照的配置。一个文件只有在某一侧「会被 pytest 收集」时才计入该侧的用例数——
  把测试改成普通辅助文件、移出收集目录，都会被算成真减法。
- **收集配置本身变化**（PR 改 `testpaths` / `python_files`）时，两侧各按自己的配置对全集计数，
  「删掉一个收集根」不会被两侧并集掩盖。
- **前后内容**用合并基与 head 对比，不拿 base 尖端：主干在同文件新增用例不得算成 PR 删除。
- **重命名**解析 numstat 的 `old => new`（含花括号形式），旧路径取合并基内容、新路径取 head 内容。
- **AST 比数**，不用文本匹配：文本匹配会把「测试夹具里引用了 skip 字面量」当成真的新增
  跳过（本条机检首次上线时就被自己的夹具误伤过一次）。

**信任边界**：CI 里执行的是从 base ref 取出的检查器副本（见 branch-conventions.yml），但
**工作流定义本身仍来自 PR 合并快照**——`pull_request` 事件语义如此，PR 可以改工作流把本
job 换成空跑。因此本闸对恶意 PR 是提示而非硬约束；门禁文件改动的实际控制是人工复核
（CONTRIBUTING 与 ADR-0028）。替代路径 `pull_request_target` 自 2026-12-02 起对公开仓库
默认封禁，组织级 required workflows 仅 Enterprise Cloud 可用，本仓库当前都用不了。

用法（CI 与本地同一条命令）：

    python tools/pr_conventions.py --head-ref "$PR_HEAD_REF"

`--head-ref` 缺省读 `GITHUB_HEAD_REF`；`--base` 与 `--head` 缺省为 `origin/master` 与 `HEAD`；
`--repo` 缺省为本文件所在仓库（CI 跑 base 副本时显式指向 PR 检出目录）。
命名不合规或门禁自伤时退出码 1，体积越线与门禁定义改动只打印警告、退出码 0。
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

TYPE_PREFIXES = ("feat", "fix", "chore", "hotfix", "release")
NAME_PATTERN = re.compile(r"^(?:%s)/[a-z0-9][a-z0-9.\\-]*$" % "|".join(TYPE_PREFIXES))
EXEMPT_PREFIXES = ("dependabot/",)

SIZE_LINE_SOFT = 400
SIZE_FILE_SOFT = 20

WORKFLOW_DIR = ".github/workflows/"
CHECKER_PATH = "tools/pr_conventions.py"

DEFAULT_TEST_ROOTS = ("tests",)
DEFAULT_TEST_PATTERNS = ("test_*.py", "*_test.py")


@dataclass(frozen=True)
class Diff:
    """一次 PR 的净增量：文件数、增删行数与改动路径对（旧路径, 新路径）。"""

    files: int
    added: int
    removed: int
    changes: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class TestMetrics:
    """一个测试文件的 AST 计数：用例数与带 skip 的用例数。"""

    tests: int
    skipped: int


def name_problems(head_ref: str) -> list[str]:
    """返回头分支命名的问题清单；空清单表示合规。"""
    ref = head_ref.strip()
    if not ref:
        return ["头分支名为空，无法校验命名"]
    if ref.startswith(EXEMPT_PREFIXES):
        return []
    if NAME_PATTERN.match(ref):
        return []
    return [
        f"头分支 `{ref}` 不符合 `type/slug` 命名：类型取 {'/'.join(TYPE_PREFIXES)}，"
        "slug 全小写、只用 a-z0-9.-（如 `fix/settlement-rounding`）"
    ]


def size_warnings(diff: Diff) -> list[str]:
    """体积软线：越线只提示，不阻断。"""
    out: list[str] = []
    if diff.files > SIZE_FILE_SOFT:
        out.append(f"改动 {diff.files} 个文件，超过软线 {SIZE_FILE_SOFT}；请说明为何不能拆")
    if diff.added + diff.removed > SIZE_LINE_SOFT:
        out.append(
            f"改动 {diff.added + diff.removed} 行（+{diff.added}/-{diff.removed}），"
            f"超过软线 {SIZE_LINE_SOFT}；请说明为何不能拆"
        )
    return out


def _skip_decorated(decorators: list[ast.expr]) -> bool:
    for node in decorators:
        try:
            text = ast.unparse(node)
        except Exception:  # pragma: no cover - unparse 失败按未跳过处理
            continue
        if "mark.skip" in text or text.endswith("skip") or text.endswith("skipif"):
            return True
    return False


def test_metrics(source: str) -> TestMetrics:
    """解析测试源码，数「以 test_ 开头的函数」与「带 skip / skipif / pytestmark 的用例」。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return TestMetrics(tests=0, skipped=0)

    module_skip = False
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "pytestmark":
                    try:
                        module_skip = "skip" in ast.unparse(node.value)
                    except Exception:  # pragma: no cover
                        module_skip = False
    tests = skipped = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            tests += 1
            if module_skip or _skip_decorated(list(node.decorator_list)):
                skipped += 1
    return TestMetrics(tests=tests, skipped=skipped)


def parse_numstat_path(raw: str) -> tuple[str, str]:
    """numstat 路径列 → (旧路径, 新路径)。重命名给 `a => b` 或 `pre{a => b}post` 形式。"""
    if " => " not in raw:
        return raw, raw
    m = re.match(r"^(.*?)\{(.*?) => (.*?)\}(.*)$", raw)
    if m:
        pre, old, new, post = m.groups()
        return pre + old + post, pre + new + post
    old, new = raw.split(" => ", 1)
    return old, new


def is_gate_path(path: str) -> bool:
    """门禁定义：workflow（含 actions.lock）与本检查器。"""
    return path.startswith(WORKFLOW_DIR) or path == CHECKER_PATH


def judge_self_harm(gate_paths: tuple[str, ...], removed_tests: int, added_skipped: int) -> list[str]:
    """同一 PR 既改门禁定义又在测试上做减法 → 不合规。"""
    if not gate_paths:
        return []
    out: list[str] = []
    if removed_tests > 0:
        out.append(f"删除 {removed_tests} 个测试用例，同时改了 {len(gate_paths)} 个门禁文件（workflow / 检查器 / 锁）")
    if added_skipped > 0:
        out.append(f"新增 {added_skipped} 个被跳过的用例，同时改了门禁文件")
    if out:
        out.append("门禁自伤：同一个 PR 既改门禁定义又在测试上做减法，需要人工复核并说明失败为何是预期")
    return out


def collect_diff(repo: Path, base: str, head: str) -> Diff:
    """用三点 diff（合并基）取 PR 净增量；重命名保留旧/新两侧路径。"""
    numstat = _git(repo, "diff", "--numstat", f"{base}...{head}")
    files = added = removed = 0
    changes: list[tuple[str, str]] = []
    for raw in numstat.splitlines():
        parts = raw.split("\t")
        if len(parts) < 3:
            continue
        add_s, del_s, raw_path = parts[0], parts[1], parts[2]
        files += 1
        changes.append(parse_numstat_path(raw_path))
        # 二进制文件给 "-"，按 0 行计
        added += int(add_s) if add_s.isdigit() else 0
        removed += int(del_s) if del_s.isdigit() else 0
    return Diff(files=files, added=added, removed=removed, changes=tuple(changes))


def _pytest_config(repo: Path, ref: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """读某一侧快照的 pytest 收集配置（testpaths / python_files）。"""
    text = _git_optional(repo, "show", f"{ref}:pyproject.toml")
    if not text:
        return (), ()
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return (), ()
    opts = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
    roots = tuple(str(r).strip().rstrip("/") for r in opts.get("testpaths", []) if str(r).strip())
    patterns = tuple(str(p).strip() for p in opts.get("python_files", []) if str(p).strip())
    return roots, patterns


def collection_config(repo: Path, ref: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """某一侧快照的 pytest 收集配置（testpaths / python_files），读不到走默认。"""
    roots, patterns = _pytest_config(repo, ref)
    return (
        roots or DEFAULT_TEST_ROOTS,
        patterns or DEFAULT_TEST_PATTERNS,
    )


def _collects(path: str, roots: tuple[str, ...], patterns: tuple[str, ...]) -> bool:
    """该路径在给定配置下是否会被 pytest 收集。"""
    if not path.endswith(".py"):
        return False
    if not any(path.startswith(root + "/") for root in roots):
        return False
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def _collection_totals(
    repo: Path, ref: str, roots: tuple[str, ...], patterns: tuple[str, ...]
) -> TestMetrics:
    """某一侧全集：按该侧配置列出被收集的测试文件并计数。"""
    tests = skipped = 0
    for path in _git(repo, "ls-tree", "-r", "--name-only", ref).splitlines():
        if not _collects(path, roots, patterns):
            continue
        m = test_metrics(_git_optional(repo, "show", f"{ref}:{path}"))
        tests += m.tests
        skipped += m.skipped
    return TestMetrics(tests=tests, skipped=skipped)


def test_subtraction(
    repo: Path, base: str, head: str, changes: tuple[tuple[str, str], ...]
) -> tuple[int, int]:
    """返回（被删的用例数，被新增跳过的用例数），按 AST 计数、不匹配文本。

    - 前内容取合并基、后内容取 head，两侧各自用自己那份 pytest 配置判断「会不会被收集」；
      测试移出收集范围（改成辅助文件、挪出收集目录）计为真减法。
    - 收集配置本身变化时对两侧全集计数，避免「删收集根」被并集掩盖。
    """
    merge_base = _git(repo, "merge-base", base, head).strip()
    base_cfg = collection_config(repo, merge_base)
    head_cfg = collection_config(repo, head)

    if base_cfg != head_cfg:
        before = _collection_totals(repo, merge_base, *base_cfg)
        after = _collection_totals(repo, head, *head_cfg)
        return max(0, before.tests - after.tests), max(0, after.skipped - before.skipped)

    removed = added_skip = 0
    for old, new in changes:
        before = (
            test_metrics(_git_optional(repo, "show", f"{merge_base}:{old}"))
            if _collects(old, *base_cfg)
            else TestMetrics(tests=0, skipped=0)
        )
        after = (
            test_metrics(_git_optional(repo, "show", f"{head}:{new}"))
            if _collects(new, *head_cfg)
            else TestMetrics(tests=0, skipped=0)
        )
        removed += max(0, before.tests - after.tests)
        added_skip += max(0, after.skipped - before.skipped)
    return removed, added_skip


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{proc.stderr.strip()}")
    return proc.stdout


def _git_optional(repo: Path, *args: str) -> str:
    """新增或删除的文件在某一侧取不到，按空文件处理。"""
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout if proc.returncode == 0 else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PR 准入机检（命名 / 体积 / 门禁自伤）")
    parser.add_argument("--head-ref", default=os.environ.get("GITHUB_HEAD_REF", ""), help="PR 头分支名")
    parser.add_argument("--base", default="origin/master", help="基线 ref（默认 origin/master）")
    parser.add_argument("--head", default="HEAD", help="头 ref（默认 HEAD）")
    parser.add_argument("--repo", default=str(REPO_ROOT), help="仓库根（默认本文件所在仓库）")
    args = parser.parse_args(argv)

    repo = Path(args.repo)
    hard: list[str] = list(name_problems(args.head_ref))
    diff = collect_diff(repo, args.base, args.head)
    gate_paths = tuple(new for old, new in diff.changes if is_gate_path(old) or is_gate_path(new))
    removed_tests, added_skip = test_subtraction(repo, args.base, args.head, diff.changes)
    hard.extend(judge_self_harm(gate_paths, removed_tests, added_skip))

    for warn in size_warnings(diff):
        print(f"::warning title=PR 规模软线::{warn}")
        print(f"[规模] {warn}")
    if gate_paths:
        warn = f"本 PR 修改门禁定义（{', '.join(gate_paths)}），合入前需人工复核（ADR-0028 / CONTRIBUTING）"
        print(f"::warning title=门禁定义改动::{warn}")
        print(f"[门禁] {warn}")

    print(f"[净增量] {diff.files} 文件 +{diff.added}/-{diff.removed}")
    if hard:
        for msg in hard:
            print(f"::error title=PR 准入::{msg}")
            print(f"[不合规] {msg}")
        return 1
    print("[通过] 命名合规，未检出「门禁自伤」")
    return 0


if __name__ == "__main__":
    sys.exit(main())