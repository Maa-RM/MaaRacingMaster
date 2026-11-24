# -*- coding: utf-8 -*-
"""PR 准入机检：分支命名（硬拦）、体积（只警告）、门禁自伤（硬拦）。

口径来自 [ADR-0028](../docs/adr/0028-arch-global-分支模型定为单主干与短命主题分支.md) 决策 5：

- **命名**：头分支必须匹配 `type/slug`（`feat` / `fix` / `chore` / `hotfix` / `release`，
  slug 只用 `a-z0-9.-`）；`dependabot/**` 是机器生成分支，豁免。这一条零歧义，硬拦。
- **体积**：超过 `SIZE_LINE_SOFT` 行或 `SIZE_FILE_SOFT` 文件**只警告**。本仓库自己的收口 PR
  就会越线，硬门槛只会逼出碎 PR 与「藏进顺手格式化」两类规避。
- **门禁自伤**：同一个 PR 既改 `.github/workflows/**`、又在测试上做减法时硬拦并要求人工
  复核——它挡的是「为变绿改门禁」。

测试减法用 **AST 比数**（base 与 head 各解析一次 `tests/**` 的改动文件），不用文本匹配：
文本匹配会把「测试夹具里引用了 skip 字面量」当成真的新增跳过（本条机检首次上线时就被
自己的夹具误伤过一次）。

用法（CI 与本地同一条命令）：

    python tools/pr_conventions.py --head-ref "$PR_HEAD_REF"

`--head-ref` 缺省读 `GITHUB_HEAD_REF`；`--base` 与 `--head` 缺省为 `origin/master` 与 `HEAD`。
命名不合规或门禁自伤时退出码 1，体积越线只打印警告、退出码 0。
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

TYPE_PREFIXES = ("feat", "fix", "chore", "hotfix", "release")
NAME_PATTERN = re.compile(r"^(?:%s)/[a-z0-9][a-z0-9.\\-]*$" % "|".join(TYPE_PREFIXES))
EXEMPT_PREFIXES = ("dependabot/",)

SIZE_LINE_SOFT = 400
SIZE_FILE_SOFT = 20

WORKFLOW_DIR = ".github/workflows/"
TESTS_DIR = "tests/"


@dataclass(frozen=True)
class Diff:
    """一次 PR 的净增量：文件数、增删行数与改动路径。"""

    files: int
    added: int
    removed: int
    paths: tuple[str, ...]


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


def judge_self_harm(workflow_paths: tuple[str, ...], removed_tests: int, added_skipped: int) -> list[str]:
    """同一 PR 既改门禁定义又在测试上做减法 → 不合规。"""
    if not workflow_paths:
        return []
    out: list[str] = []
    if removed_tests > 0:
        out.append(f"删除 {removed_tests} 个测试用例，同时改了 {len(workflow_paths)} 个 workflow 文件")
    if added_skipped > 0:
        out.append(f"新增 {added_skipped} 个被跳过的用例，同时改了 workflow 文件")
    if out:
        out.append("门禁自伤：同一个 PR 既改门禁定义又在测试上做减法，需要人工复核并说明失败为何是预期")
    return out


def collect_diff(repo: Path, base: str, head: str) -> Diff:
    """用三点 diff（合并基）取 PR 净增量。"""
    numstat = _git(repo, "diff", "--numstat", f"{base}...{head}")
    files = added = removed = 0
    paths: list[str] = []
    for raw in numstat.splitlines():
        parts = raw.split("\t")
        if len(parts) < 3:
            continue
        add_s, del_s, path = parts[0], parts[1], parts[2]
        files += 1
        paths.append(path)
        # 二进制文件给 "-"，按 0 行计
        added += int(add_s) if add_s.isdigit() else 0
        removed += int(del_s) if del_s.isdigit() else 0
    return Diff(files=files, added=added, removed=removed, paths=tuple(paths))


def test_subtraction(repo: Path, base: str, head: str, paths: tuple[str, ...]) -> tuple[int, int]:
    """返回（被删的用例数，被新增跳过的用例数），按 AST 计数、不匹配文本。"""
    removed = added_skip = 0
    for path in paths:
        if not path.startswith(TESTS_DIR) or not path.endswith(".py"):
            continue
        before = test_metrics(_git_optional(repo, "show", f"{base}:{path}"))
        after = test_metrics(_git_optional(repo, "show", f"{head}:{path}"))
        removed += max(0, before.tests - after.tests)
        added_skip += max(0, after.skipped - before.skipped)
    return removed, added_skip


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
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
        ["git", *args],
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
    workflow_paths = tuple(p for p in diff.paths if p.startswith(WORKFLOW_DIR))
    removed_tests, added_skip = test_subtraction(repo, args.base, args.head, diff.paths)
    hard.extend(judge_self_harm(workflow_paths, removed_tests, added_skip))

    for warn in size_warnings(diff):
        print(f"::warning title=PR 规模软线::{warn}")
        print(f"[规模] {warn}")

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
