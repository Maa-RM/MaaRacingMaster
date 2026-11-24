# -*- coding: utf-8 -*-
"""PR 准入机检：分支命名（硬拦）、体积（只警告）、门禁自伤（硬拦）。

口径来自 [ADR-0028](../docs/adr/0028-arch-global-分支模型定为单主干与短命主题分支.md) 决策 5：

- **命名**：头分支必须匹配 `type/slug`（`feat` / `fix` / `chore` / `hotfix` / `release`，
  slug 只用 `a-z0-9.-`）；`dependabot/**` 是机器生成分支，豁免。这一条零歧义，硬拦。
- **体积**：超过 `SIZE_LINE_SOFT` 行或 `SIZE_FILE_SOFT` 文件**只警告**。本仓库自己的收口 PR
  就会越线，硬门槛只会逼出碎 PR 与「藏进顺手格式化」两类规避。
- **门禁自伤**：同一个 PR 既改 `.github/workflows/**`、又在测试上做减法（删用例、新增
  skip）时判定为自伤，硬拦并要求人工复核——它挡的是「为变绿改门禁」。

用法（CI 与本地同一条命令）：

    python tools/pr_conventions.py --head-ref "$PR_HEAD_REF"

`--head-ref` 缺省读 `GITHUB_HEAD_REF`；`--base` 与 `--head` 缺省为 `origin/master` 与 `HEAD`。
命名不合规或门禁自伤时退出码 1，体积越线只打印警告、退出码 0。
"""
from __future__ import annotations

import argparse
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
_TEST_DEF = re.compile(r"\b(?:async\s+)?def\s+test_")
_SKIP_ADDED = re.compile(r"pytest\.mark\.skip|\.skip\(|skipif\(")


@dataclass(frozen=True)
class Diff:
    """一次 PR 的净增量：文件数、增删行数、改动路径与零上下文补丁。"""

    files: int
    added: int
    removed: int
    paths: tuple[str, ...]
    patch: str


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


def self_harm_problems(diff: Diff) -> list[str]:
    """门禁自伤：同改 workflow 定义 + 在测试上做减法。"""
    touched_workflow = [p for p in diff.paths if p.startswith(WORKFLOW_DIR)]
    if not touched_workflow:
        return []

    removed_defs = sum(1 for line in diff.patch.splitlines() if line.startswith("-") and "+++" not in line and _TEST_DEF.search(line))
    added_defs = sum(1 for line in diff.patch.splitlines() if line.startswith("+") and "+++" not in line and _TEST_DEF.search(line))
    added_skips = sum(1 for line in diff.patch.splitlines() if line.startswith("+") and "+++" not in line and _SKIP_ADDED.search(line))

    out: list[str] = []
    if removed_defs > added_defs:
        out.append(f"删除 {removed_defs - added_defs} 个测试用例，同时改了 {len(touched_workflow)} 个 workflow 文件")
    if added_skips:
        out.append(f"新增 {added_skips} 处 skip/skipif，同时改了 workflow 文件")
    if out:
        out.append("门禁自伤：同一个 PR 既改门禁定义又在测试上做减法，需要人工复核并说明失败为何是预期")
    return out


def collect_diff(repo: Path, base: str, head: str) -> Diff:
    """用三点 diff（合并基）取 PR 净增量。"""
    numstat = _git(repo, "diff", "--numstat", f"{base}...{head}")
    patch = _git(repo, "diff", "-U0", f"{base}...{head}")
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
    return Diff(files=files, added=added, removed=removed, paths=tuple(paths), patch=patch)


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PR 准入机检（命名 / 体积 / 门禁自伤）")
    parser.add_argument("--head-ref", default=os.environ.get("GITHUB_HEAD_REF", ""), help="PR 头分支名")
    parser.add_argument("--base", default="origin/master", help="基线 ref（默认 origin/master）")
    parser.add_argument("--head", default="HEAD", help="头 ref（默认 HEAD）")
    parser.add_argument("--repo", default=str(REPO_ROOT), help="仓库根（默认本文件所在仓库）")
    args = parser.parse_args(argv)

    hard: list[str] = list(name_problems(args.head_ref))
    diff = collect_diff(Path(args.repo), args.base, args.head)
    hard.extend(self_harm_problems(diff))

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
