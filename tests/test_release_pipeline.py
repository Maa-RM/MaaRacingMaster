# -*- coding: utf-8 -*-
"""受管发布脚本（vpk 打包与渠道索引）的回归锁（升级适配 P4）。

锁四件事：
1. 版本一致性：内容树版本（maaracing_master/_version.py）与 -Version 不一致即拒绝
   ——组装、CLI、索引、主程序版本必须对应同一次完整发行；
2. 同版本覆盖阻断：目标渠道索引已含同版本时拒绝重跑（不靠改已推送历史修补发布）；
3. 渠道分离：stable / preview 各自的索引文件，绝不共用同一渠道文件；
4. vpk 调用形态：正式 packId、内容树入口、渠道参数、无增量（首版不加增量复杂度）。

脚本用 -DryRun 验证调用形态（不真跑 vpk；真实链路验证需发布授权，见 P4 边界记录）。
"""

from __future__ import annotations

import re
import subprocess
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "release" / "make_update_release.ps1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or POWERSHELL is None,
    reason="发布打包脚本需要 Windows 和 PowerShell",
)


def _ps(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _put(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _stage_fixture(tmp_path: Path, version: str = "1.2.3") -> Path:
    stage = tmp_path / "stage"
    _put(stage, "MaaRacingMaster.Shell.exe", "binary")
    _put(stage, "maaracing_master/_version.py",
         f"__version__ = version = \"{version}\"\n")
    return stage


def _run(setup: str, section: str) -> subprocess.CompletedProcess:
    command = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8\n"
        "$ErrorActionPreference = 'Stop'\n"
        "$Fail = { param($m) Write-Host \"FATAL: $m\"; exit 1 }\n"
        + setup + "\n" + section
        + "\nexit 0"
    )
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )


def _section(start: str, end: str | None = None) -> str:
    source = SCRIPT.read_text(encoding="utf-8-sig")
    section = source[source.index(start):]
    return section[:section.index(end)] if end else section


def test_script_exists_with_channel_and_packid_params():
    """脚本存在且渠道/包标识参数符合契约（ValidateSet 双渠道、正式 packId 默认值）。"""
    assert SCRIPT.is_file(), "make_update_release.ps1 不存在"
    src = SCRIPT.read_text(encoding="utf-8-sig")
    assert re.search(r"ValidateSet\('stable','preview'\)\]\[string\]\$Channel", src), (
        "渠道参数必须限定 stable/preview（渠道分离的入口闸）"
    )
    assert "$PackId = 'MaaRacingMaster'" in src, "默认 packId 应为正式产品名（非实验探针名）"


def test_version_mismatch_rejected(tmp_path):
    """内容树版本与 -Version 不一致 → 拒绝（防组装/索引/主程序版本漂移）。"""
    stage = _stage_fixture(tmp_path, version="9.9.9")
    setup = (
        f"$StageDir={_ps(stage)}; $Version='1.2.3'; $Channel='stable'; "
        f"$OutDir={_ps(tmp_path / 'out')}; $VpkPath={_ps('vpk')}"
    )
    result = _run(setup, _section("# ---------- 1. 版本一致性", "# ---------- 2. 覆盖阻断"))
    assert result.returncode != 0, result.stdout + result.stderr
    assert "版本" in result.stdout + result.stderr


def test_version_match_passes(tmp_path):
    stage = _stage_fixture(tmp_path, version="1.2.3")
    setup = (
        f"$StageDir={_ps(stage)}; $Version='1.2.3'; $Channel='stable'; "
        f"$OutDir={_ps(tmp_path / 'out')}; $VpkPath={_ps('vpk')}"
    )
    result = _run(setup, _section("# ---------- 1. 版本一致性", "# ---------- 2. 覆盖阻断"))
    assert result.returncode == 0, result.stdout + result.stderr


def test_same_version_rerun_blocked(tmp_path):
    """目标渠道索引已含同版本 → 拒绝重跑（覆盖阻断；不同版本放行）。"""
    stage = _stage_fixture(tmp_path, version="1.2.3")
    out = tmp_path / "out"
    out.mkdir()
    _put(out, "Releases/stable/releases.stable.json",
         '{"Releases":[{"Version":"1.2.3"}]}')
    setup = (
        f"$StageDir={_ps(stage)}; $Version='1.2.3'; $Channel='stable'; "
        f"$OutDir={_ps(out)}; $VpkPath={_ps('vpk')}"
    )
    result = _run(setup, _section("# ---------- 2. 覆盖阻断", "# ---------- 3. vpk 调用"))
    assert result.returncode != 0, result.stdout + result.stderr
    assert "同版本" in result.stdout + result.stderr

    # 不同版本（升级目标已存在但版本不同）放行
    out2 = tmp_path / "out2"
    out2.mkdir()
    _put(out2, "Releases/stable/releases.stable.json",
         '{"Releases":[{"Version":"1.2.2"}]}')
    setup2 = (
        f"$StageDir={_ps(stage)}; $Version='1.2.3'; $Channel='stable'; "
        f"$OutDir={_ps(out2)}; $VpkPath={_ps('vpk')}"
    )
    result2 = _run(setup2, _section("# ---------- 2. 覆盖阻断", "# ---------- 3. vpk 调用"))
    assert result2.returncode == 0, result2.stdout + result2.stderr


def test_channel_index_files_are_separate(tmp_path):
    """渠道分离：索引文件按渠道命名（releases.<channel>.json），不共用。"""
    src = SCRIPT.read_text(encoding="utf-8-sig")
    body = src[src.index("param("):]
    assert "releases.$Channel" in body or "Releases/$Channel" in body, (
        "索引文件名必须按渠道参数拼接（分渠道发布）"
    )
    assert "releases.win.json" not in body, "不得回退到共用渠道文件（与渠道分离契约冲突）"


def test_dryrun_builds_expected_vpk_args(tmp_path):
    """-DryRun 真实 -File 执行：输出的 vpk 参数形态（正式 packId、内容树、渠道、主入口、
    无增量），且版本校验/覆盖阻断在真实链路中先行通过。"""
    stage = _stage_fixture(tmp_path, version="1.2.3")
    out = tmp_path / "out"
    out.mkdir()
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-File", str(SCRIPT),
         "-StageDir", str(stage), "-Version", "1.2.3",
         "-Channel", "stable", "-OutDir", str(out), "-VpkPath", "vpk", "-DryRun"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert "--packId MaaRacingMaster" in output
    assert "--packVersion 1.2.3" in output
    assert "--channel stable" in output
    assert "--mainExe MaaRacingMaster.Shell.exe" in output
    assert "--delta None" in output
    assert "--packDir" in output and str(stage) in output
