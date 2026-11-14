"""外部更新器契约验证；只运行实验输出中的隔离应用。"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
OUT = HERE / "_out"
EXE = OUT / "publish" / "MaaRM.UpdateProbe.exe"


def run(args: list[str], *, timeout: int = 45, expected: int | None = 0,
        env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, DOTNET_CLI_TELEMETRY_OPTOUT="1", NO_COLOR="1")
    env.update(env_extra or {})
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout, env=env,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if expected is not None and result.returncode != expected:
        raise AssertionError(f"exit {result.returncode}: {args}\n{result.stdout}\n{result.stderr}")
    return result


def hooks() -> None:
    normal = run([str(EXE), "--normal-probe", "中文 路径"])
    assert "APP_START " in normal.stdout, normal.stdout + normal.stderr
    for hook in ("install", "updated", "obsolete", "uninstall"):
        result = run([str(EXE), f"--veloapp-{hook}", "0.1.0"])
        assert "APP_START " not in result.stdout, (
            f"更新钩子 {hook} 进入正常启动路径，可能触发单实例窗口或启动游戏后端："
            + result.stdout + result.stderr)
    print(json.dumps({"check": "sdk-startup-hooks", "normal_launch": "preserved",
                      "fast_hooks": "exit-before-app-start"}, ensure_ascii=False))


def update(*, nested: bool = False) -> None:
    """真实工具与真实 SDK 在隔离便携副本上完成全量更新。"""
    case = OUT / ("case-" + uuid.uuid4().hex[:8])
    feed, live = case / "feed", case / "live"
    case.mkdir(parents=True)
    cli = OUT / "cli" / "vpk.exe"
    publish = EXE.parent
    if nested:
        publish = case / "stage"
        shutil.copytree(EXE.parent, publish / "app")
    main_exe = "app/" + EXE.name if nested else EXE.name
    for version in ("0.1.0", "0.2.0"):
        (publish / "payload-version.txt").write_text(version, encoding="utf-8")
        cmd = [str(cli), "pack", "--packId", "MaaRM.UpdateProbe",
               "--packTitle", "MaaRMUpdateProbe", "--packVersion", version,
               "--runtime", "win-x64", "--packDir", str(publish),
               "--mainExe", main_exe, "--outputDir", str(feed),
               "--noInst", "--delta", "None"]
        packed = run(cmd, timeout=90, expected=None)
        (case / ("pack-" + version + ".log")).write_text(
            packed.stdout + packed.stderr, encoding="utf-8")
        assert packed.returncode == 0, packed.stdout + packed.stderr
        if version == "0.1.0":
            archives = list(feed.glob("*Portable.zip"))
            assert len(archives) == 1, [str(p) for p in feed.iterdir()]
            with zipfile.ZipFile(archives[0]) as z:
                for member in z.infolist():
                    dst = live / member.filename
                    assert dst.resolve().is_relative_to(live.resolve())
                    if member.is_dir():
                        dst.mkdir(parents=True, exist_ok=True)
                    else:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        dst.write_bytes(z.read(member))
    current = live / "current"
    installed = current / main_exe
    assert (current / "payload-version.txt").read_text() == "0.1.0"
    user_file = case / "user-data" / "profile.json"
    user_file.parent.mkdir()
    user_file.write_text('{"keep":"用户配置"}', encoding="utf-8")
    user_before = user_file.read_bytes()
    launch_report = case / "launch-report.json"
    env_extra = {"MAARM_PROBE_REPORT": str(launch_report)}
    run([str(live / "MaaRMUpdateProbe.exe"), "--stub-probe"], env_extra=env_extra)
    deadline = time.monotonic() + 10
    while not launch_report.is_file() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert json.loads(launch_report.read_text())['version'] == "0.1.0"
    downloaded = run([str(installed), "--download-local", str(feed)], env_extra=env_extra)
    assert "UPDATE_DOWNLOADED" in downloaded.stdout, downloaded.stdout + downloaded.stderr
    run([str(installed), "--normal-probe"], env_extra=env_extra)
    assert (current / "payload-version.txt").read_text() == "0.1.0", "普通启动擅自提交了待更新包"
    result = run([str(installed), "--apply-local", str(feed)], timeout=35, env_extra=env_extra)
    assert "UPDATE_QUEUED" in result.stdout, result.stdout + result.stderr
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        marker = current / "payload-version.txt"
        if marker.is_file() and marker.read_text() == "0.2.0":
            break
        time.sleep(0.2)
    else:
        raise AssertionError(f"更新未完成，检查隔离目录 {case}")
    assert user_file.read_bytes() == user_before
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if json.loads(launch_report.read_text())["version"] == "0.2.0":
            break
        time.sleep(0.1)
    else:
        raise AssertionError("新版没有由更新器自动重启")
    normal = run([str(installed), "--normal-probe"])
    assert "APP_START " in normal.stdout
    report = {"check": "portable-full-update", "from": "0.1.0", "to": "0.2.0",
              "marker": "verified-on-disk", "outside-user-data": "unchanged",
              "new-executable": "automatically-restarted", "case": case.name,
              "layout": "nested-app-directory" if nested else "root-main",
              "pending-update": "not-applied-on-ordinary-startup",
              "scope": "isolated SDK probe; not production GUI/UAC"}
    (case / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


def real_shell() -> None:
    """用 dotnet 宿主检查真实 WinUI 程序的最早入口；不声称已验证 UAC。"""
    assembly = OUT / "real-shell-publish/MaaRacingMaster.Shell.dll"
    normal = run(["dotnet", str(assembly), "--update-adapter-probe"])
    assert "REAL_SHELL_PRE_UI_OK" in normal.stdout, normal.stdout + normal.stderr
    for hook in ("install", "updated", "obsolete", "uninstall"):
        result = run(["dotnet", str(assembly), f"--veloapp-{hook}", "0.1.0",
                      "--update-adapter-probe"])
        assert "REAL_SHELL_PRE_UI_OK" not in result.stdout, result.stdout + result.stderr
    print(json.dumps({"check": "real-winui-entry-hooks", "normal-entry": "preserved",
                      "hooks": "exit-before-single-instance-and-UI",
                      "launch-mode": "dotnet host; UAC not exercised"}))


def manifest_level(exe: Path) -> str:
    """读取 PE 内嵌清单，不启动程序也不申请提权。"""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
    kernel.LoadLibraryExW.restype = wintypes.HMODULE
    kernel.FindResourceW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p]
    kernel.FindResourceW.restype = ctypes.c_void_p
    kernel.LoadResource.argtypes = [wintypes.HMODULE, ctypes.c_void_p]
    kernel.LoadResource.restype = ctypes.c_void_p
    kernel.LockResource.argtypes = [ctypes.c_void_p]
    kernel.LockResource.restype = ctypes.c_void_p
    kernel.SizeofResource.argtypes = [wintypes.HMODULE, ctypes.c_void_p]
    kernel.SizeofResource.restype = wintypes.DWORD
    kernel.FreeLibrary.argtypes = [wintypes.HMODULE]
    module = kernel.LoadLibraryExW(str(exe), None, 0x02 | 0x20)
    if not module:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        resource = kernel.FindResourceW(module, 1, 24)
        if not resource:
            raise ctypes.WinError(ctypes.get_last_error())
        size = kernel.SizeofResource(module, resource)
        address = kernel.LockResource(kernel.LoadResource(module, resource))
        document = ET.fromstring(ctypes.string_at(address, size).rstrip(b"\x00"))
        node = document.find(".//{urn:schemas-microsoft-com:asm.v3}requestedExecutionLevel")
        assert node is not None
        return node.attrib["level"]
    finally:
        kernel.FreeLibrary(module)


def manifests() -> None:
    feed = OUT / (OUT / "real-shell-feed.txt").read_text(encoding="utf-8")
    assert feed.resolve().is_relative_to(OUT.resolve())
    archive = next(feed.glob("*Portable.zip"))
    with zipfile.ZipFile(archive) as z:
        stub = OUT / "real-shell-generated-stub.exe"
        stub.write_bytes(z.read("MaaRMRealShellProbe.exe"))
    main = OUT / "real-shell-publish/MaaRacingMaster.Shell.exe"
    actual = {"shell": manifest_level(main), "generated_stub": manifest_level(stub)}
    assert actual == {"shell": "requireAdministrator", "generated_stub": "requireAdministrator"}, actual
    print(json.dumps({"check": "elevation-manifests", **actual,
                      "scope": "PE resources only; actual UAC not exercised"}))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["hooks", "update", "nested-update", "real-shell", "manifests"])
    args = parser.parse_args()
    if args.action == "hooks":
        hooks()
    elif args.action == "real-shell":
        real_shell()
    elif args.action == "manifests":
        manifests()
    else:
        update(nested=args.action == "nested-update")
    return 0


if __name__ == "__main__":
    sys.exit(main())
