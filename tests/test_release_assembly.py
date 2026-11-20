# -*- coding: utf-8 -*-
"""执行装配脚本的实际阶段，验证内容筛选、归档和失败退出。

阶段边界沿用脚本已有的章节注释；只替换下载、编译之前已经准备好的目录，
不模拟文件筛选、压缩命令或体积判断。测试同时保留正常输入，防止裁剪误伤。
"""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest


REPO = Path(__file__).resolve().parent.parent
ASSEMBLE = REPO / "scripts" / "release" / "assemble.ps1"
SEVEN_Z = REPO / "scripts" / "release" / "tools" / "7za.exe"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or POWERSHELL is None,
    reason="发布装配阶段需要 Windows 和 PowerShell",
)


def _ps(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _section(start: str, end: str | None = None) -> str:
    source = ASSEMBLE.read_text(encoding="utf-8-sig")
    section = source[source.index(start):]
    return section[:section.index(end)] if end else section


def _run(setup: str, section: str) -> subprocess.CompletedProcess:
    command = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8\n"
        "$ErrorActionPreference = 'Stop'\n"
        "$errors = [System.Collections.Generic.List[string]]::new()\n"
        "$Fail = { param($m) Write-Host $m; exit 1 }\n"
        + setup + "\n" + section
        + "\nexit 0"
    )
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )


def _put(root: Path, rel: str, content: str = "fixture") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_source_copy_excludes_development_material_and_keeps_runtime(tmp_path):
    source, stage = tmp_path / "source", tmp_path / "stage"
    keep = [
        "pyproject.toml", "LICENSE", "THIRD_PARTY_LICENSES.md",
        "assets/icon.ico", "assets/maarm_icon.png",
        "apps/MaaRacingMaster.Shell/frontend/index.html",
        "maaracing_master/core/controller.py",
        "maaracing_master/core/resources/cover_default.jpg",
        "maaracing_master/plugins/alpha/manifest.py",
        "maaracing_master/plugins/alpha/resources/models/README.md",
        "maaracing_master/plugins/alpha/resources/models/LICENSE.txt",
        "maaracing_master/plugins/alpha/resources/pipeline/main.json",
    ]
    exclude = [
        "apps/MaaRacingMaster.Shell/frontend/README.md",
        "maaracing_master/core/CODE_WIKI.md",
        "maaracing_master/plugins/alpha/CODE_WIKI.md",
        "maaracing_master/plugins/alpha/RULES.md",
        "maaracing_master/plugins/alpha/tools/training/train.py",
        "maaracing_master/plugins/alpha/tests/test_strategy.py",
        "maaracing_master/plugins/alpha/data/labels.json",
        "maaracing_master/plugins/_archive/old/module.py",
    ]
    for rel in keep + exclude:
        _put(source, rel)
    stage.mkdir()
    result = _run(
        f"$RepoRoot={_ps(source)}; $StageRoot={_ps(stage)}; $WithModels=$false",
        _section("# ---------- 3. whitelist", "# ---------- 3.5"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not [rel for rel in exclude if (stage / rel).exists()]
    assert all((stage / rel).is_file() for rel in keep)
    assert all((source / rel).is_file() for rel in exclude), "不能修改源码树"


def test_dependency_pruning_keeps_runtime_bindings_and_models(tmp_path):
    packages = tmp_path / "packages"
    keep = [
        "numpy/__init__.py", "numpy/_pytesttester.py", "numpy/ctypeslib/__init__.py",
        "numpy/_typing/__init__.py", "numpy/typing/__init__.py",
        "shapely/__init__.py", "mpmath/__init__.py",
        "onnxruntime/capi/onnxruntime_pybind11_state.pyd",
        "rapidocr/models/det.onnx", "numpy-2.4.6.dist-info/RECORD",
    ]
    exclude = [
        "numpy/_core/tests/test_core.py", "numpy/lib/tests/test_lib.py",
        "numpy/random/tests/test_random.py", "shapely/tests/test_geometry.py",
        "mpmath/tests/test_math.py", "colorama/tests/test_ansi.py",
        "certifi/tests/test_certify.py", "onnxruntime/tools/convert.py",
        "onnxruntime/transformers/models/stable_diffusion/engine.py",
        "onnxruntime/quantization/quantize.py",
    ]
    for rel in keep + exclude:
        _put(packages, rel)
    result = _run(
        f"$rtDir={_ps(tmp_path)}; $RemoveNumpyDev=$true; $RemoveOrtOffline=$true",
        _section("# ---------- 2.60", "# ---------- 2.61")
        + _section("# ---------- 2.66", "# ---------- 2.67"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not [rel for rel in exclude if (packages / rel).exists()]
    assert all((packages / rel).is_file() for rel in keep)


def test_import_self_check_does_not_generate_bytecode(tmp_path):
    for name in ("maa", "onnxruntime", "cv2", "numpy", "rapidocr", "windows_capture", "vgamepad"):
        _put(tmp_path, f"{name}.py", "")
    _put(tmp_path, "maaracing_master/__init__.py",
         "import sys\nsys.dont_write_bytecode=True\n__version__='fixture'\n")
    _put(tmp_path, "maaracing_master/_version.py", "__version__='fixture'\n")
    result = _run(
        f"$rtDir={_ps(Path(sys.executable).parent)}; $StageRoot={_ps(tmp_path)}",
        _section("# ---------- 5. import self-check", "# 自检通过且"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not list(tmp_path.rglob("*.pyc")), "导入自检不应改变待归档目录"


def test_zip_contains_no_build_cache_and_preserves_payload(tmp_path):
    stage = tmp_path / "fixture"
    _put(stage, "payload.txt")
    unwanted = [
        "runtime/python/.cache-fingerprint", "app/.cache-fingerprint",
        "runtime/python/packages/alpha/__pycache__/module.cpython-311.pyc",
        "maaracing_master/stale.pyc", "maaracing_master/stale.pyo",
    ]
    for rel in unwanted:
        _put(stage, rel)
    result = _run(
        f"$StageRoot={_ps(stage)}; $OutRoot={_ps(tmp_path)}; $Name='fixture'",
        _section("# ---------- 7. zip", "# ---------- 7.5"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with zipfile.ZipFile(tmp_path / "fixture.zip") as archive:
        names = {entry.filename.removeprefix("./") for entry in archive.infolist()}
        assert "payload.txt" in names
        assert not names.intersection(unwanted)
    assert (tmp_path / "fixture.zip.sha256").is_file()


@pytest.mark.parametrize("total_mib,zip_mib,should_fail", [
    (1, 1, False), (474, 1, True), (1, 205, True),
])
def test_size_gate_blocks_growth_and_accepts_smaller_packages(tmp_path, total_mib, zip_mib, should_fail):
    stage = tmp_path / "fixture"
    payload = _put(stage, "runtime/payload")
    archive = _put(tmp_path, "fixture.zip")
    for path, size in ((payload, total_mib), (archive, zip_mib)):
        with path.open("r+b") as stream:
            stream.truncate(size * 1024 * 1024)
    result = _run(
        f"$StageRoot={_ps(stage)}; $OutRoot={_ps(tmp_path)}; $zip={_ps(archive)}; "
        f"$sevenzFile={_ps(tmp_path / 'fixture.7z')}; $Configuration='Release'; "
        "$DisableReleaseOptimizations=$false; $Version='fixture'; $excludedIds=@()",
        _section("# ---------- 8. Release Size Gate"),
    )
    assert (result.returncode != 0) == should_fail, result.stdout + result.stderr
    assert (tmp_path / "release-size-report.json").is_file()


@pytest.mark.parametrize("runtime_mib,should_fail", [(455, False), (480, True)])
def test_offline_models_do_not_hide_or_create_size_regression(tmp_path, runtime_mib, should_fail):
    stage = tmp_path / "fixture"
    payload = _put(stage, "runtime/payload")
    model = _put(stage, "maaracing_master/plugins/speedrush/resources/models/depth/model.onnx")
    for path, size in ((payload, runtime_mib), (model, 20)):
        with path.open("r+b") as stream:
            stream.truncate(size * 1024 * 1024)
    archive = tmp_path / "fixture.zip"
    with zipfile.ZipFile(archive, "w") as packed:
        packed.write(model, "./" + model.relative_to(stage).as_posix())
    result = _run(
        f"$StageRoot={_ps(stage)}; $OutRoot={_ps(tmp_path)}; $zip={_ps(archive)}; "
        f"$sevenzFile={_ps(tmp_path / 'fixture.7z')}; $Configuration='Release'; "
        "$WithModels=$true; $DisableReleaseOptimizations=$false; $Version='fixture'; $excludedIds=@()",
        _section("# ---------- 8. Release Size Gate"),
    )
    assert (result.returncode != 0) == should_fail, result.stdout + result.stderr


@pytest.mark.parametrize("compressor_exit", [0, 2])
def test_partial_7z_is_rejected_even_when_file_exists(tmp_path, compressor_exit):
    if not SEVEN_Z.is_file():
        pytest.skip("缺少仓库随附的 7-Zip 工具")
    stage = tmp_path / "fixture"
    _put(stage, "payload.txt")
    compressor = _put(tmp_path, "compressor.cmd",
        '@echo off\nif "%~1"=="a" (\n'
        f' >"{tmp_path / "fixture.7z"}" echo partial\n'
        f' exit /b {compressor_exit}\n)\n"{SEVEN_Z}" %*\nexit /b %errorlevel%\n')
    result = _run(
        f"$StageRoot={_ps(stage)}; $OutRoot={_ps(tmp_path)}; $Name='fixture'; "
        f"$SevenZ=$true; $SevenZPath={_ps(compressor)}",
        _section("# ---------- 7.5", "# ---------- 8."),
    )
    assert (tmp_path / "fixture.7z").exists(), "失败场景必须实际留下不完整归档"
    assert result.returncode != 0, result.stdout + result.stderr
    assert not (tmp_path / "fixture.7z.sha256").exists()


def test_valid_7z_is_created_and_checksummed(tmp_path):
    if not SEVEN_Z.is_file():
        pytest.skip("缺少仓库随附的 7-Zip 工具")
    stage = tmp_path / "fixture"
    _put(stage, "payload.txt")
    result = _run(
        f"$StageRoot={_ps(stage)}; $OutRoot={_ps(tmp_path)}; $Name='fixture'; "
        f"$SevenZ=$true; $SevenZPath={_ps(SEVEN_Z)}",
        _section("# ---------- 7.5", "# ---------- 8."),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "fixture.7z.sha256").is_file()
    checked = subprocess.run(
        [str(SEVEN_Z), "t", str(tmp_path / "fixture.7z")],
        capture_output=True, timeout=30,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


@pytest.mark.local_asset
def test_pruned_cached_runtime_passes_real_behavior_smoke(tmp_path):
    cached = REPO / "build" / "runtime-cache"
    if not (cached / "python.exe").is_file():
        pytest.skip("缺少已组装的 Windows Python runtime 缓存")
    runtime = tmp_path / "runtime" / "python"
    shutil.copytree(cached, runtime)
    before = sum(path.stat().st_size for path in runtime.rglob("*") if path.is_file())
    result = _run(
        f"$RepoRoot={_ps(REPO)}; $StageRoot={_ps(tmp_path)}; $rtDir={_ps(runtime)}; "
        "$RemoveNumpyDev=$true; $RemoveOrtOffline=$true; $WithModels=$false; $Version='0.0.0'",
        _section("# 统一清理 pip 生成", "# ---------- 2. dotnet publish")
        + _section("# ---------- 2.60", "# ---------- 2.61")
        + _section("# ---------- 2.66", "# ---------- 2.67")
        + _section("# ---------- 3. whitelist", "# ---------- 3.5")
        + _section("# ---------- 4. _version.py", "# 自检通过且")
        + f"\n& $py -B {_ps(REPO / 'scripts/release/smoke_runtime.py')}\n"
        + "if ($LASTEXITCODE -ne 0) { throw 'runtime smoke failed' }\n"
        + _section("# ---------- 6. errors", "# ---------- 7. zip"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[smoke] ALL PASS" in result.stdout
    assert not list(runtime.rglob("*.pyc"))
    after = sum(path.stat().st_size for path in runtime.rglob("*") if path.is_file())
    print(f"\nruntime removed: {(before - after) / 1024 / 1024:.2f} MiB")
    print("\n".join(line for line in result.stdout.splitlines() if line.startswith("[smoke]")))


# ---------- 受管模式 ----------

def _publish_fixture(publish: Path) -> None:
    """预置 GUI 发布产物与缓存指纹（指纹段对空源码树算出的固定值），使 -SkipPublish 跳过真实编译。"""
    for name in ("MaaRacingMaster.Shell.exe", "mscordbi.dll", "onnxruntime.dll"):
        _put(publish, name, "binary")
    _put(publish, ".cache-fingerprint", "publish/")


def _managed_setup(tmp_path: Path, managed: bool,
                   update_source_cnb: str = "", update_source_github: str = "") -> str:
    source = tmp_path / "src"
    source.mkdir(parents=True, exist_ok=True)
    _put(source, "apps/MaaRacingMaster.Shell/MaaRacingMaster.Shell.csproj", "fixture")
    # Launcher 目录存在但源缺失：Push-Location 成功后走「源缺失」确定性报错分支
    # （真实编译分支依赖 MSVC 环境，夹具不触发；$VcVarsAll 指向不存在路径以固定分支）
    (source / "apps" / "MaaRacingMaster.Launcher").mkdir(parents=True)
    # GUI 发布夹具放脚本派生路径（$RepoRoot\build\publish-cache——脚本节内会重新赋值该变量）
    _publish_fixture(source / "build" / "publish-cache")
    stage = tmp_path / "stage"
    stage.mkdir(parents=True, exist_ok=True)
    setup = (
        f"$RepoRoot={_ps(source)}; $StageRoot={_ps(stage)}; "
        f"$HostPython={_ps(sys.executable)}; "
        f"$SkipPublish=$true; $pubFinger='publish/'; "
        f"$Managed={'$true' if managed else '$false'}; "
        f"$UpdateSourceCnb={_ps(update_source_cnb)}; $UpdateSourceGithub={_ps(update_source_github)}; "
        f"$VcVarsAll={_ps(tmp_path / 'no-msvc')}; "

        # $appDir 与缓存指纹函数的派生在脚本 param/指纹区（本节提取范围之外），
        # 夹具按同一规则补齐；指纹函数与 fixture 的 .cache-fingerprint 值一致 → -SkipPublish 跳过真实编译
        f"$appDir={_ps(stage)}; if (-not $Managed) {{ $appDir = Join-Path $StageRoot 'app' }}; "
        "$CacheFPWrite = { param($dir,$finger) Set-Content -Path (Join-Path $dir '.cache-fingerprint') -Value $finger -Encoding ascii }; "
        "$CacheFPMatch = { param($dir,$finger) $f = Join-Path $dir '.cache-fingerprint'; if (Test-Path $f) { return ((Get-Content $f -Raw).Trim() -eq $finger) }; return $false }; "
        f"$errors=[System.Collections.Generic.List[string]]::new()"
    )
    return setup, stage


def test_managed_layout_puts_gui_at_content_root(tmp_path):
    """受管模式：GUI 发布产物落内容根（壳 exe 在根），不再有 app/ 子目录。"""
    setup, stage = _managed_setup(tmp_path, managed=True)
    result = _run(
        setup,
        _section("# ---------- 2. dotnet publish", "# ---------- 2.5"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (stage / "MaaRacingMaster.Shell.exe").is_file()
    assert (stage / "mscordbi.dll").is_file()
    assert not (stage / "app").exists(), "受管模式不得再建 app/ 子目录"


def test_legacy_layout_keeps_app_dir(tmp_path):
    """旧 ZIP 模式回归锁：GUI 仍落 app/ 子目录，根目录不放壳 exe。"""
    setup, stage = _managed_setup(tmp_path, managed=False)
    result = _run(
        setup,
        _section("# ---------- 2. dotnet publish", "# ---------- 2.5"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (stage / "app" / "MaaRacingMaster.Shell.exe").is_file()
    assert not (stage / "MaaRacingMaster.Shell.exe").exists()


def test_managed_mode_writes_named_source_lines(tmp_path):
    """受管模式：两个命名下载地址由脚本统一产出命名行，指向同一发行货架；
    行为锁——真实执行 assemble 的 2.6 节并读产物，删掉生产者即红。"""
    setup, stage = _managed_setup(
        tmp_path, managed=True,
        update_source_cnb="https://cnb.example/feed",
        update_source_github="https://github.example/feed",
    )
    result = _run(setup, _section("# ---------- 2.6 根入口", "# ---------- 3. whitelist"))
    assert result.returncode == 0, result.stdout + result.stderr
    lines = (stage / "update-source.txt").read_text(encoding="utf-8").strip().splitlines()
    assert lines == [
        "cnb|https://cnb.example/feed",
        "github|https://github.example/feed",
    ], "命名地址未按 cnb|/github| 格式写入"


def test_managed_mode_requires_both_named_sources(tmp_path):
    """缺任一命名地址 → 报错且不写文件：不合规配置属发行事故，不得流出；
    客户端无单源兼容模式，残缺包只会诚实报「更新源未配置」。"""
    setup, stage = _managed_setup(
        tmp_path / "missing", managed=True,
        update_source_cnb="https://cnb.example/feed",
    )
    result = _run(
        setup,
        _section("# ---------- 2.6 根入口", "# ---------- 3. whitelist")
        + '\n$errors | ForEach-Object { Write-Host "ERR: $_" }',
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "同时提供" in result.stdout + result.stderr
    assert not (stage / "update-source.txt").exists(), "缺地址时不得产出随包来源配置"


def test_managed_mode_skips_launcher_and_reports_missing(tmp_path):
    """受管模式不编译 Launcher（入口由 SDK CLI 生成）：缺壳在 stage 时报错、不产根入口。"""
    setup, stage = _managed_setup(tmp_path, managed=True)
    # 故意不放壳 exe：受管入口校验必须报错（防内容树缺真实壳静默通过）
    (stage / "MaaRacingMaster.Shell.exe").unlink(missing_ok=True)
    result = _run(
        setup,
        _section("# ---------- 2.6 根入口", "# ---------- 3. whitelist")
        + '\n$errors | ForEach-Object { Write-Host "ERR: $_" }',
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "受管内容树缺少真实壳" in result.stdout + result.stderr
    assert not (stage / "MaaRacingMaster.exe").exists(), "受管模式不得产出根入口（SDK CLI 职责）"


def test_legacy_launcher_reports_missing_source(tmp_path):
    """旧 ZIP 模式回归锁：Launcher 源缺失时显式报错（原逻辑保留）。"""
    setup, stage = _managed_setup(tmp_path, managed=False)
    result = _run(
        setup,
        _section("# ---------- 2.6 根入口", "# ---------- 3. whitelist")
        + '\n$errors | ForEach-Object { Write-Host "ERR: $_" }',
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "missing launcher source" in result.stdout + result.stderr


def test_full_script_parses_without_errors():
    """全脚本语法解析锁：阶段抽取执行绕过了 param 参数列表，这里锁整个脚本能被
    PowerShell 解析器接受（历史回归：新增参数漏逗号导致所有打包入口在解析期即失效）。"""
    command = (
        "$tokens=$null; $parseErrors=$null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"{_ps(ASSEMBLE)}, [ref]$tokens, [ref]$parseErrors) | Out-Null; "
        "if ($parseErrors.Count -gt 0) { "
        "$parseErrors | ForEach-Object { Write-Host (\"PARSE: {0} (line {1})\" -f $_.Message, $_.Extent.StartLineNumber) }; "
        "exit 1 }; exit 0"
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
