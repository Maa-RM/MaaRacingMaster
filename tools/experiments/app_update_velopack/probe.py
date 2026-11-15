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
import traceback
import uuid
import zipfile
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
OUT = HERE / "_out"
EXE = OUT / "publish" / "MaaRM.UpdateProbe.exe"

SHELL_EXE = "MaaRacingMaster.Shell.exe"
# 提权子进程用的随包真实 Python：商店版解释器（WindowsApps）无法提权启动。
# 运行时缓存默认取当前检出的 build/runtime-cache，跨检出复用时用环境变量覆盖。
RUNTIME_PYTHON = Path(os.environ.get(
    "MAARM_RUNTIME_SOURCE", str(REPO / "build" / "runtime-cache"))) / "python.exe"


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


# ---------------------------------------------------------------------------
# 受管真实壳整链（P0）：标准布局 live 副本上的提权启动、升级提交与证据采集。
# 提权说明：壳与生成入口均为 requireAdministrator，因此整链探针需在提权上下文运行；
# 非提权运行时会经 UAC 自提权重启自身一次（唯一一次系统交互）。
# ---------------------------------------------------------------------------

class _ShellExecuteInfoW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HANDLE),
        ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
    ]


def _is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _elevated_interpreter() -> str:
    override = os.environ.get("MAARM_ELEVATED_PYTHON")
    if override and Path(override).is_file():
        return override
    exe = sys.executable
    if "WindowsApps" not in exe and Path(exe).is_file():
        return exe
    if RUNTIME_PYTHON.is_file():
        return str(RUNTIME_PYTHON)
    return exe


def _run_elevated_child(result_path: Path, action_args: list[str]) -> None:
    """以 runas 经 cmd 启动提权子进程并等待结束；UAC 由用户确认一次。

    子进程解释器用随包真实 Python（商店版无法提权）；stdout/stderr 重定向到
    日志文件，子进程秒退时可用于定位（cmd.exe 是系统原生程序，提权最稳）。
    """
    log_path = result_path.with_suffix(".launch.log")
    interpreter = _elevated_interpreter()
    script = Path(__file__).resolve()
    sei = _ShellExecuteInfoW()
    sei.cbSize = ctypes.sizeof(_ShellExecuteInfoW)
    sei.fMask = 0x40  # SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = os.environ.get("ComSpec", "cmd.exe")
    sei.lpParameters = (f'/c ""{interpreter}" -B -X utf8 "{script}" '
                        f'{" ".join(action_args)} '
                        f'--elevated-child --result "{result_path}" '
                        f'> "{log_path}" 2>&1"')
    sei.lpDirectory = str(HERE)
    sei.nShow = 0  # SW_HIDE：子进程控制台不可见——控制台窗口被用户关闭会连带杀掉整棵进程树
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)) or not sei.hProcess:
        code = ctypes.windll.kernel32.GetLastError()
        raise OSError(f"UAC 提权启动失败：hInstApp={sei.hInstApp & 0xFFFFFFFF} "
                      f"GetLastError={code}（UAC 被取消也会落到这里）")
    ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, 45 * 60 * 1000)
    ctypes.windll.kernel32.CloseHandle(sei.hProcess)


def _process_snapshot(path_prefix: str) -> list[dict]:
    """按可执行文件路径前缀抓进程拓扑（elevated 下可见提权进程）。"""
    p = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
             "$p = $env:MAARM_SNAPSHOT_PATTERN; "
             "Get-CimInstance Win32_Process | "
             "Where-Object { $_.ExecutablePath -like ($p + '*') } | "
             "Select-Object ProcessId,ParentProcessId,Name,ExecutablePath | "
             "ConvertTo-Json -Compress"],
            timeout=60, expected=None,
            env_extra={"MAARM_SNAPSHOT_PATTERN": path_prefix})
    text = p.stdout.strip()
    if not text:
        return []
    data = json.loads(text)
    return [data] if isinstance(data, dict) else data


def _shell_running() -> bool:
    p = run(["tasklist", "/FI", f"IMAGENAME eq {SHELL_EXE}", "/FO", "CSV", "/NH"],
            timeout=15, expected=None)
    return SHELL_EXE in p.stdout


def _tail(path: Path, limit: int) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except Exception as exc:
        return f"<unreadable: {exc!r}>"


def _case_env(case: Path, tag: str) -> tuple[dict, Path, Path]:
    """独立用户数据目录 + 版本报告文件；logs/UDF/报告全部落在隔离目录内。"""
    userdata = case / ("userdata-" + tag)
    userdata.mkdir(parents=True, exist_ok=True)
    report = case / ("report-" + tag + ".json")
    return ({"APPDATA": str(userdata), "MAARM_PROBE_REPORT": str(report)},
            userdata, report)


def _wait_report(report: Path, seconds: float) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if report.is_file():
            try:
                return json.loads(report.read_text(encoding="utf-8"))
            except Exception:
                pass
        time.sleep(0.3)
    raise AssertionError(f"版本报告未出现: {report}")


def _wait_marker(current: Path, version: str, seconds: float) -> None:
    marker = current / "payload-version.txt"
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if marker.is_file() and marker.read_text(encoding="utf-8") == version:
            return
        time.sleep(0.5)
    raise AssertionError(f"磁盘标记未到 {version}: {marker}")


def _recent_wer_events() -> str:
    p = run(["powershell", "-NoProfile", "-Command",
             "Get-WinEvent -FilterHashtable @{LogName='Application'; Level=2; "
             "StartTime=(Get-Date).AddMinutes(-5)} -EA SilentlyContinue | "
             "Where-Object { $_.Message -like '*msedgewebview2*' "
             "-or $_.Message -like '*WebView2*' -or $_.Message -like '*MaaRacing*' } | "
             "Select-Object -First 5 TimeCreated,Id,ProviderName | ConvertTo-Json -Compress"],
            timeout=60, expected=None)
    return p.stdout.strip() or "(无相关错误事件)"


def _collect_live_evidence(live_root: Path, userdata: Path,
                           report: Path | None = None) -> dict:
    """双时点采证；每段独立容错，一段失败不丢其余证据。"""
    time.sleep(30)  # 侧车导入与 WebView2 初始化需要几十秒
    ev: dict = {}

    def _sec(key, fn):
        try:
            ev[key] = fn()
        except Exception as exc:
            ev[key] = f"<error: {exc!r}>"

    _sec("processes", lambda: _process_snapshot(str(live_root)))
    _sec("msedgewebview2_running", lambda: "msedgewebview2.exe" in run(
        ["tasklist", "/FI", "IMAGENAME eq msedgewebview2.exe", "/FO", "CSV", "/NH"],
        timeout=15, expected=None).stdout)
    logs = userdata / "MaaRacingMaster" / "logs"
    _sec("log_files", lambda: (sorted(p.name for p in logs.iterdir())
                               if logs.is_dir() else []))

    def _session_logs():
        # logs/ 根上同时有壳日志（shell.log 等）文件，只把目录当会话
        sessions = sorted(p.name for p in logs.iterdir() if p.is_dir()) if logs.is_dir() else []
        if not sessions:
            return {}
        session = logs / sessions[-1]
        return {p.name: p.stat().st_size for p in session.iterdir() if p.is_file()}

    _sec("session_logs", _session_logs)
    _sec("sidecar_stderr_tail", lambda: _tail(logs / "sidecar_stderr.log", 1500))
    current = live_root / "current" if (live_root / "current").is_dir() else live_root

    def _udf_dirs():
        return [p.name for p in current.glob("*.exe.WebView2")]

    _sec("webview2_udf_dirs", _udf_dirs)

    def _udf_tree():
        dirs = list(current.glob("*.exe.WebView2"))
        return ([str(p.relative_to(current)) for p in dirs[0].rglob("*")][:30]
                if dirs else [])

    _sec("webview2_udf_tree", _udf_tree)
    if report is not None:
        wv_report = Path(str(report) + ".webview2.txt")
        _sec("webview2_init_evidence",
             lambda: _tail(wv_report, 3000) if wv_report.exists() else "(未写出)")
    _sec("recent_wer_events", _recent_wer_events)
    return ev


def _wait_user_close_window(ev: dict, case: Path) -> None:
    print("GUI 窗口已打开（提权运行）。请目视确认界面与版本正常后关闭窗口；10 分钟未关将自动结束。",
          flush=True)
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        if not _shell_running():
            ev["window_close"] = "user-closed"
            break
        time.sleep(1)
    else:
        run(["taskkill", "/IM", SHELL_EXE, "/T", "/F"], timeout=30, expected=None)
        ev["window_close"] = "timeout-killed"
    # 关窗后清掉孤儿侧车（壳强杀路径不等待子进程，正是 P0 要记录的现状）
    leftover = _process_snapshot(str(case))
    for item in leftover:
        run(["taskkill", "/PID", str(item["ProcessId"]), "/T", "/F"],
            timeout=30, expected=None)
    ev["orphan_cleaned"] = [item["Name"] for item in leftover]


def _log_session_count(userdata: Path) -> int:
    logs = userdata / "MaaRacingMaster" / "logs"
    return len([p for p in logs.iterdir() if p.is_dir()]) if logs.is_dir() else 0


def _p0_tail_chain() -> dict:
    """P0 收尾：①壳崩溃遗留侧车后再升级 ②旧应用根毒化（环境变量覆盖内容根）。"""
    info = json.loads((OUT / "managed-case.json").read_text(encoding="utf-8"))
    live = Path(info["live"])
    feed = Path(info["feed"])
    case = Path(info["case"])
    current = live / "current"
    helper = OUT / "update-helper" / "MaaRM.UpdateProbe.exe"
    target = info["versions"][1]
    ev: dict = {"case": info["case"]}

    # ---- 用例①：壳崩溃遗留侧车后再升级 ----
    env_o, userdata_o, _ = _case_env(case, "orphan")
    subprocess.Popen([str(live / info["stub"])], cwd=str(live),
                     env={**os.environ, **env_o},
                     creationflags=0x00000008)
    procs = []
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        procs = _process_snapshot(str(live))
        if any(p["Name"] == "python.exe" for p in procs):
            break
        time.sleep(1)
    else:
        raise AssertionError("侧车 90 秒内未启动")
    ev["orphan_sidecar_spawned"] = True
    shells = [p for p in procs if p["Name"] == SHELL_EXE]
    assert shells, "壳进程未找到"
    # 模拟崩溃：只杀壳不杀树 → 提权侧车成为孤儿
    run(["taskkill", "/PID", str(shells[0]["ProcessId"]), "/F"],
        timeout=15, expected=None)
    time.sleep(2)
    orphan = [p for p in _process_snapshot(str(live)) if p["Name"] == "python.exe"]
    ev["orphan_count_after_crash"] = len(orphan)
    # 侧车主循环读 stdin（sidecar.py:762），父死即 EOF 自退——孤儿不成立是正面证据
    ev["orphan_verdict"] = ("无孤儿：侧车父死即自退（stdin EOF）"
                            if not orphan else "存在孤儿，继续观察升级行为")

    d = run([str(helper), "--locator-root", str(live), "--download-local", str(feed)],
            timeout=600, env_extra=env_o)
    assert "UPDATE_DOWNLOADED" in d.stdout, d.stdout + d.stderr
    ev["orphan_download"] = "ok"
    a = run([str(helper), "--locator-root", str(live), "--apply-local", str(feed)],
            timeout=600, expected=None, env_extra=env_o)
    ev["orphan_apply_exit"] = a.returncode
    ev["orphan_apply_stdout"] = (a.stdout or "")[:300]
    try:
        _wait_marker(current, target, 600)
        ev["orphan_disk_after"] = target
    except AssertionError as exc:
        ev["orphan_disk_after"] = f"未替换: {exc}"
    orphan_after = [p for p in _process_snapshot(str(live)) if p["Name"] == "python.exe"]
    ev["orphan_count_after_apply"] = len(orphan_after)
    # 重启出来的 GUI（新版本）给用户看一眼后关闭
    _wait_user_close_window(ev, case)

    # ---- 用例②：旧应用根毒化 ----
    decoy = case / "旧版根目录"
    (decoy / "runtime" / "python").mkdir(parents=True, exist_ok=True)
    (decoy / "pyproject.toml").write_text("", encoding="utf-8")
    env_p, userdata_p, _ = _case_env(case, "poison")
    env_p["MaaRM_APP_ROOT"] = str(decoy)
    err_log = case / "poison-shell-stderr.txt"
    logs_before = _log_session_count(userdata_p)
    with open(err_log, "w", encoding="utf-8") as err_f:
        proc = subprocess.Popen([str(live / info["stub"])], cwd=str(live),
                                stdout=err_f, stderr=err_f,
                                env={**os.environ, **env_p},
                                creationflags=subprocess.CREATE_NO_WINDOW)
        ev["poison_pid"] = proc.pid
        time.sleep(20)
        procs_p = _process_snapshot(str(case))
        ev["poison_processes"] = procs_p
        ev["poison_python_from_decoy"] = [str(p.get("ExecutablePath")) for p in procs_p
                                          if "旧版根目录" in str(p.get("ExecutablePath", ""))]
        ev["poison_session_logs_delta"] = _log_session_count(userdata_p) - logs_before
        ev["poison_stderr_tail"] = _tail(err_log, 2000)
    _wait_user_close_window(ev, case)
    ev["poison_verdict"] = (
        "环境变量覆盖内容根成立：无侧车进程、无新会话日志（前端落旧根不可用）"
        if not ev["poison_python_from_decoy"] and ev["poison_session_logs_delta"] == 0
        else "行为与预期不同，见证据")
    return ev


def _managed_update_chain() -> dict:
    """受管真实壳整链：壳保持最小注入，下载/提交由独立更新助手驱动。"""
    info = json.loads((OUT / "managed-case.json").read_text(encoding="utf-8"))
    live = Path(info["live"])
    feed = Path(info["feed"])
    case = Path(info["case"])
    current = live / "current"
    shell_exe = current / SHELL_EXE
    # 助手从受管树外以显式定位器驱动（受管树内运行次要 exe 会触发默认定位器挂起）
    helper = OUT / "update-helper" / "MaaRM.UpdateProbe.exe"
    env, userdata, report = _case_env(case, "update")
    profile = userdata / "profile.json"
    profile.write_text('{"keep":"用户配置"}', encoding="utf-8")
    user_before = profile.read_bytes()
    ev: dict = {"case": info["case"], "live": info["live"]}

    # 1 生成入口两跳探针启动（最小壳早退；证据为退出码）
    r1 = run([str(live / info["stub"]), "--update-adapter-probe"],
             timeout=60, expected=None, env_extra=env)
    ev["stub_probe_exit"] = r1.returncode
    assert r1.returncode == 0, f"入口探针启动退出码异常: {r1.returncode} {r1.stdout} {r1.stderr}"

    # 2 版本与内容根报告（助手树外直启，显式定位器读受管根）
    run([str(helper), "--locator-root", str(live)], timeout=60, expected=None, env_extra=env)
    first = _wait_report(report, 20)
    ev["helper_report"] = first
    assert first["version"] == "0.1.0", first
    assert (current / "payload-version.txt").read_text(encoding="utf-8") == "0.1.0"

    # 3 下载到本地（不动磁盘内容）
    downloaded = run([str(helper), "--locator-root", str(live), "--download-local", str(feed)],
                     timeout=600, env_extra=env)
    assert "UPDATE_DOWNLOADED" in downloaded.stdout, downloaded.stdout + downloaded.stderr
    ev["download"] = "ok"

    # 4 普通启动（真实入口链）不提交待更新包
    r4 = run([str(live / info["stub"]), "--update-adapter-probe"],
             timeout=60, expected=None, env_extra=env)
    ev["stub_probe_exit_second"] = r4.returncode
    assert r4.returncode == 0, r4.returncode
    assert (current / "payload-version.txt").read_text(encoding="utf-8") == "0.1.0", \
        "普通启动擅自提交了待更新包"
    ev["pending_not_applied_on_ordinary_start"] = True

    # 5 提交：助手树外发起，壳退出后由更新器整树替换并自动重启完整 GUI（最小注入形态）
    result = run([str(helper), "--locator-root", str(live), "--apply-local", str(feed)],
                 timeout=600, expected=None, env_extra=env)
    assert "UPDATE_QUEUED" in result.stdout, result.stdout + result.stderr
    ev["apply_requested"] = True

    # 6 磁盘替换 + 包外用户数据不变
    _wait_marker(current, "0.2.0", 600)
    ev["disk_replaced"] = "0.2.0"
    assert profile.read_bytes() == user_before, "包外用户数据被改动"
    ev["outside_user_data"] = "unchanged"

    # 7 自动重启的新版壳 GUI：以 EBWebView 结构出现作为 WebView2 环境创建成功的判据
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if (current / "MaaRacingMaster.Shell.exe.WebView2" / "EBWebView").is_dir():
            ev["gui_webview2_environment"] = "created"
            break
        time.sleep(1)
    else:
        ev["gui_webview2_environment"] = "未出现（白窗形态）"
    time.sleep(5)
    ev.update(_collect_live_evidence(live, userdata))

    # 8 用户目视确认后关闭窗口
    _wait_user_close_window(ev, case)

    # 9 升级后入口探针 + 助手报告（新版上下文版本应为 0.2.0）
    run([str(shell_exe), "--update-adapter-probe"], timeout=60, expected=None, env_extra=env)
    run([str(helper), "--locator-root", str(live)], timeout=60, expected=None, env_extra=env)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if json.loads(report.read_text(encoding="utf-8"))["version"] == "0.2.0":
                ev["post_update_report"] = "0.2.0"
                break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        ev["post_update_report"] = "未确认"
    ev["post_update_entry"] = "ok"
    return ev


def _launch_and_collect(target: Path, live_root: Path, userdata: Path,
                        report: Path, env: dict, label: str,
                        detach: bool = True, report_required: bool = True) -> dict:
    """启动一次 GUI 并采证。detach=True 用裸启动（无任何标准句柄，与日常/生产
    启动同形态）；detach=False 用文件重定向（已知会诱发 WebView2 初始化挂起，
    仅作对照用）。report_required=False 用于未插仪器的构建（无版本报告可等）。
    关窗后清孤儿。"""
    ev: dict = {"label": label, "target": str(target), "detach": detach}
    flags = 0x00000008 if detach else subprocess.CREATE_NO_WINDOW  # DETACHED_PROCESS
    kwargs: dict = {}
    err_log = report.parent / (report.name + f".{label}.shell-stderr.txt")
    err_f = None
    if not detach:
        err_f = open(err_log, "w", encoding="utf-8")
        kwargs = {"stdout": err_f, "stderr": err_f}
    try:
        proc = subprocess.Popen([str(target)], cwd=str(target.parent),
                                env={**os.environ, **env},
                                creationflags=flags, **kwargs)
        ev["pid"] = proc.pid
        if report_required:
            ev["report"] = _wait_report(report, 60)
        else:
            time.sleep(10)
        ev["evidence_25s"] = _collect_live_evidence(live_root, userdata)
        ev["evidence_60s"] = _collect_live_evidence(live_root, userdata)
        _wait_user_close_window(ev, report.parent)
    finally:
        if err_f is not None:
            err_f.close()
    if err_f is not None:
        ev["shell_stderr"] = _tail(err_log, 3000)
    return ev


def _managed_launch_chain(poison: bool, via_stub: bool) -> dict:
    info = json.loads((OUT / "managed-case.json").read_text(encoding="utf-8"))
    live = Path(info["live"])
    case = Path(info["case"])
    ev: dict = {"case": info["case"], "poison": poison, "via_stub": via_stub}

    if poison:
        decoy = case / "旧版根目录"
        (decoy / "runtime" / "python").mkdir(parents=True, exist_ok=True)
        (decoy / "pyproject.toml").write_text("", encoding="utf-8")
        env1, userdata1, report1 = _case_env(case, "poison")
        env1["MaaRM_APP_ROOT"] = str(decoy)
        target = (live / info["stub"]) if via_stub else (live / "current" / SHELL_EXE)
        ev["exp_poison"] = _launch_and_collect(target, live, userdata1, report1,
                                               env1, "poison")
        return ev

    # 判定轮：只变「构建形态 × 路径」，全部真实 APPDATA + 裸启动（与日常同款）。
    # 已证：句柄重定向与裸启动都挂、深短路径都挂、探针/非探针构建都挂；
    # 而日常 Debug 构建（依赖框架）今天 14:50 WebView2 仍成功。
    # exp1（dbg_bin）已由上一轮人工确认：探针父进程无害，日常 Debug 构建渲染正常。
    # 本轮只测自包含发布构建（已插仪器）：短路径 → 深路径。
    real_ud = Path(os.environ["APPDATA"]) / "MaaRacingMaster"
    # 短路径对照夹具：落在检出所在盘符根（短路径语义与检出位置无关，不写死盘符）
    short_root = Path(REPO.anchor) / "mrt_short_test"
    if short_root.exists():
        shutil.rmtree(short_root)
    shutil.copytree(OUT / "managed-stage", short_root)
    try:
        plans = [
            ("pub_short", short_root / SHELL_EXE, short_root),
            ("pub_deep", live / "current" / SHELL_EXE, live),
        ]
        for label, target, root in plans:
            report = case / f"report-{label}.json"
            env = {"MAARM_PROBE_REPORT": str(report)}
            ev[f"exp_{label}"] = _launch_and_collect(
                target, root, real_ud, report, env, label)
    finally:
        shutil.rmtree(short_root, ignore_errors=True)
    return ev


def _formal_update_chain() -> dict:
    """P1 正式壳整链：发布正式工程（无任何实验注入）组装的受管副本上走完整升级。

    正式壳没有探针参数早退——本链以「完整 GUI 启动 → 等侧车就绪 → 采证 → 收尾杀树」
    替代仪器化副本的探针启动；P1 验证点：受管内容根识别（shell.log 决策行）、
    WebView2 数据目录落用户数据根（不再是 exe 旁）、侧车会话日志（P0 遗留握手证据）、
    普通启动不提交待更新包、整树替换与自动重启、包外用户数据不动。
    """
    info = json.loads((OUT / "formal-case.json").read_text(encoding="utf-8"))
    live = Path(info["live"])
    feed = Path(info["feed"])
    case = Path(info["case"])
    current = live / "current"
    helper = OUT / "update-helper" / "MaaRM.UpdateProbe.exe"
    ev: dict = {"case": info["case"], "shell": "formal"}
    env, userdata, report = _case_env(case, "formal")
    profile = userdata / "profile.json"
    profile.write_text('{"keep":"用户配置"}', encoding="utf-8")
    user_before = profile.read_bytes()
    shell_log = userdata / "MaaRacingMaster" / "logs" / "shell.log"
    udf_root = userdata / "MaaRacingMaster" / "webview2"

    def _wait_udf(seconds: int) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if (udf_root / "EBWebView").is_dir():
                return True
            time.sleep(1)
        return False

    def _gui_session(tag: str) -> dict:
        """完整启动 GUI（stub 两跳，DETACHED）→ 等侧车 → 采证 → 杀整树收尾。"""
        out: dict = {"tag": tag}
        logs_before = _log_session_count(userdata)
        subprocess.Popen([str(live / info["stub"])], cwd=str(live),
                         env={**os.environ, **env},
                         creationflags=0x00000008)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            procs = _process_snapshot(str(live))
            if any(p["Name"] == "python.exe" for p in procs):
                break
            time.sleep(1)
        else:
            raise AssertionError(f"{tag}: 侧车 120 秒内未启动")
        out["sidecar_started"] = True
        out["webview2_udf_in_userdata"] = _wait_udf(60)
        out["udf_exe_adjacent"] = (current / (SHELL_EXE + ".WebView2")).is_dir()
        out["session_logs_delta"] = _log_session_count(userdata) - logs_before
        out["shell_log_tail"] = _tail(shell_log, 2500)
        run(["taskkill", "/IM", SHELL_EXE, "/T", "/F"], timeout=30, expected=None)
        time.sleep(3)
        out["python_after_kill"] = len(
            [p for p in _process_snapshot(str(live)) if p["Name"] == "python.exe"])
        return out

    # 1 首次完整启动（0.1.0）：受管解析、UDF 迁移、侧车就绪
    ev["first_launch"] = _gui_session("first")

    # 2 助手版本报告（树外显式定位器）
    run([str(helper), "--locator-root", str(live)], timeout=60, expected=None, env_extra=env)
    first = _wait_report(report, 20)
    ev["helper_report"] = first["version"]
    assert first["version"] == "0.1.0", first

    # 3 下载
    downloaded = run([str(helper), "--locator-root", str(live), "--download-local", str(feed)],
                     timeout=600, env_extra=env)
    assert "UPDATE_DOWNLOADED" in downloaded.stdout, downloaded.stdout + downloaded.stderr
    ev["download"] = "ok"

    # 4 普通完整启动不提交待更新包
    ev["second_launch"] = _gui_session("second")
    ev["pending_not_applied"] = (current / "payload-version.txt").read_text(
        encoding="utf-8") == "0.1.0"

    # 5 提交 → 整树替换 + 自动重启新版完整 GUI
    result = run([str(helper), "--locator-root", str(live), "--apply-local", str(feed)],
                 timeout=600, expected=None, env_extra=env)
    assert "UPDATE_QUEUED" in result.stdout, result.stdout + result.stderr
    ev["apply_requested"] = True
    _wait_marker(current, info["versions"][1], 600)
    ev["disk_replaced"] = info["versions"][1]
    assert profile.read_bytes() == user_before, "包外用户数据被改动"
    ev["outside_user_data"] = "unchanged"

    # 6 升级后自动重启的 GUI：侧车 + 用户数据根 UDF + 壳日志决策行 + 双时点采证
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if any(p["Name"] == "python.exe" for p in _process_snapshot(str(live))):
            break
        time.sleep(1)
    else:
        raise AssertionError("升级后自动重启的 GUI 侧车未启动")
    ev["post_update_udf_in_userdata"] = _wait_udf(60)
    ev["post_update_shell_log_tail"] = _tail(shell_log, 2500)
    ev.update(_collect_live_evidence(live, userdata))

    # 7 用户目视确认后关闭窗口（P0 遗留项：重启窗口人工目视）
    _wait_user_close_window(ev, case)

    # 8 升级后版本（助手树外报告）
    run([str(helper), "--locator-root", str(live)], timeout=60, expected=None, env_extra=env)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if json.loads(report.read_text(encoding="utf-8"))["version"] == info["versions"][1]:
                ev["post_update_report"] = info["versions"][1]
                break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        ev["post_update_report"] = "未确认"
    ev["post_update_entry"] = "ok"
    return ev


def _webview_for_udf(udf_root: Path) -> list[dict]:
    """抓命令行携带本隔离用户数据目录的浏览器进程（归属判定）——全机按进程名命中
    会混入其它应用的浏览器实例，不能证明本次页面已渲染（评审回归）。"""
    p = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
             "$p = $env:MAARM_UDF_PATTERN; "
             "Get-CimInstance Win32_Process | "
             "Where-Object { $_.Name -eq 'msedgewebview2.exe' -and "
             "$_.CommandLine -like ('*' + $p + '*') } | "
             "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
            timeout=60, expected=None,
            env_extra={"MAARM_UDF_PATTERN": str(udf_root)})
    text = p.stdout.strip()
    if not text:
        return []
    data = json.loads(text)
    return [data] if isinstance(data, dict) else data


def _dev_smoke_chain(args) -> dict:
    """dev 启动回归烟测（P1 非受管口径）：工作树 Debug 壳 + MaaRM_APP_ROOT 指向主线检出
    （工作树无 .venv，借主线运行时与前端），用户数据隔离到临时目录。
    验收断言（评审回归：记录式取证不构成通过判据）：
    ①本次启动的侧车（启动前基线差分归属 PID，排除探针自身——探针与侧车共用主线解释器）
      就绪并全程存活；
    ②本壳的浏览器进程存在且命令行携带本隔离用户数据目录（归属判定，非全机名命中）；
    ③非受管解析决策行落壳日志、界面隐私模式无回落、exe 旁无浏览器数据目录；
    ④用户关窗（壳进程消失即视为关闭，此字段是推断不是人工确认记录）后，本次后端在
      宽限窗内退出（OnClosed 优雅关停 / 输入管道 EOF 自退）。
    页面桥业务请求强断言不做：RPC 通道由壳持有，黑盒无法注入；就绪代理 = 归属浏览器
    进程 + 决策行 + 无错误日志，验证边界如实记录于此。"""
    shell = Path(args.shell)
    app_root = Path(args.dev_root)
    assert shell.is_file(), f"壳不存在: {shell}"
    assert (app_root / ".venv").is_dir(), f"主线检出缺 .venv: {app_root}"
    case = OUT / ("devsmoke-" + uuid.uuid4().hex[:8])
    case.mkdir(parents=True)
    userdata = case / "userdata"
    userdata.mkdir()
    env = {"APPDATA": str(userdata), "MaaRM_APP_ROOT": str(app_root)}
    ev: dict = {"case": str(case), "shell": str(shell), "dev_root": str(app_root)}
    py_prefix = str(app_root / ".venv")
    baseline_pids = {p["ProcessId"] for p in _process_snapshot(py_prefix)}
    baseline_pids.add(os.getpid())  # 探针自身共用主线解释器前缀，必须排除
    subprocess.Popen([str(shell)], cwd=str(shell.parent),
                     env={**os.environ, **env}, creationflags=0x00000008)

    def _new_python_pids() -> list[int]:
        return [p["ProcessId"] for p in _process_snapshot(py_prefix)
                if p["ProcessId"] not in baseline_pids]

    def _sidecar_alive() -> bool:
        return sidecar_pid in {p["ProcessId"] for p in _process_snapshot(py_prefix)}

    deadline = time.monotonic() + 120
    sidecar_pid = None
    while time.monotonic() < deadline:
        new_pids = _new_python_pids()
        if new_pids:
            sidecar_pid = new_pids[0]
            break
        time.sleep(1)
    else:
        raise AssertionError("侧车 120 秒内未启动（本次启动无新增解释器前缀进程）")
    ev["sidecar_pid"] = sidecar_pid
    ev["sidecar_started"] = True

    ev["sidecar_alive_before_wait"] = _sidecar_alive()
    assert ev["sidecar_alive_before_wait"], "本次侧车启动后即消失"

    udf_root = userdata / "MaaRacingMaster" / "webview2"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if _webview_for_udf(udf_root):
            break
        time.sleep(1)
    # InPrivate 不持久站点数据，EBWebView 是否出现只记录不作为渲染证据
    ev["webview2_ebwebview_dir"] = (udf_root / "EBWebView").is_dir()
    ev["webview_for_udf"] = bool(_webview_for_udf(udf_root))
    assert ev["webview_for_udf"], "60 秒内未见携带本隔离用户数据目录的浏览器进程"
    ev["udf_exe_adjacent"] = (shell.parent / (SHELL_EXE + ".WebView2")).is_dir()
    assert not ev["udf_exe_adjacent"], "壳 exe 旁不得出现浏览器数据目录"

    shell_log = _tail(userdata / "MaaRacingMaster" / "logs" / "shell.log", 2500)
    ev["shell_log_tail"] = shell_log
    assert "[approot]" in shell_log, "壳日志缺非受管解析决策行"
    assert "隐私模式初始化失败" not in shell_log, "界面隐私模式初始化回落（不应发生）"

    ev.update(_collect_live_evidence(shell.parent, userdata))
    assert _sidecar_alive(), "取证阶段本次侧车消失"

    _wait_user_close_window(ev, case)
    # 关窗终局（评审回归：后端退出必须纳入验收判据，不因仍存活而静默通过）
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and _sidecar_alive():
        time.sleep(1)
    ev["sidecar_exit_after_close"] = not _sidecar_alive()
    assert ev["sidecar_exit_after_close"], "窗口关闭后 30 秒内本次后端未退出"
    return ev


def _p3_chain() -> dict:
    """P3 整链：真实按钮驱动全链——用户在关于页完成检查/下载/重启升级，
    探针只负责启动、等磁盘替换与采证。白窗终验：壳内 UpdateManager + 新前端
    在受管树内的渲染（用户目视 + EBWebView 客观代理）。"""
    info = json.loads((OUT / "formal-case.json").read_text(encoding="utf-8"))
    live = Path(info["live"])
    case = Path(info["case"])
    current = live / "current"
    helper = OUT / "update-helper" / "MaaRM.UpdateProbe.exe"
    target = info["versions"][1]
    ev: dict = {"case": info["case"], "shell": "formal-p3", "driven_by": "real-buttons"}
    env, userdata, report = _case_env(case, "p3")
    profile = userdata / "profile.json"
    profile.write_text('{"keep":"用户配置"}', encoding="utf-8")
    user_before = profile.read_bytes()
    shell_log = userdata / "MaaRacingMaster" / "logs" / "shell.log"
    udf_root = userdata / "MaaRacingMaster" / "webview2"

    # 1 启动 0.1.0（stub 两跳，DETACHED）
    subprocess.Popen([str(live / info["stub"])], cwd=str(live),
                     env={**os.environ, **env}, creationflags=0x00000008)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if any(p["Name"] == "python.exe" for p in _process_snapshot(str(live))):
            break
        time.sleep(1)
    else:
        raise AssertionError("侧车 120 秒内未启动")
    ev["first_sidecar"] = True
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if (udf_root / "EBWebView").is_dir():
            break
        time.sleep(1)
    ev["first_udf_created"] = (udf_root / "EBWebView").is_dir()
    ev["udf_exe_adjacent"] = (current / (SHELL_EXE + ".WebView2")).is_dir()
    ev["source_file"] = _tail(current / "update-source.txt", 300) \
        if (current / "update-source.txt").is_file() else "(缺)"
    ev["first_shell_log_tail"] = _tail(shell_log, 1200)

    # 2 等用户在关于页完成 检查 → 下载 → 重启升级；以磁盘替换为完成判据
    print("等待用户在窗口中完成升级操作（磁盘替换为判据，15 分钟上限）……", flush=True)
    _wait_marker(current, target, 900)
    ev["disk_replaced"] = target
    assert profile.read_bytes() == user_before, "包外用户数据被改动"
    ev["outside_user_data"] = "unchanged"

    # 3 重启的新版采证（白窗终验：EBWebView + msedgewebview2 + 壳日志）
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if any(p["Name"] == "python.exe" for p in _process_snapshot(str(live))):
            break
        time.sleep(1)
    else:
        raise AssertionError("升级后 GUI 未自动重启（侧车未出现）")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if (udf_root / "EBWebView").is_dir():
            break
        time.sleep(1)
    ev["post_udf_created"] = (udf_root / "EBWebView").is_dir()
    ev["post_shell_log_tail"] = _tail(shell_log, 2500)
    ev.update(_collect_live_evidence(live, userdata))
    run([str(helper), "--locator-root", str(live)], timeout=60, expected=None, env_extra=env)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if json.loads(report.read_text(encoding="utf-8"))["version"] == target:
                ev["post_update_report"] = target
                break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        ev["post_update_report"] = "未确认"

    # 4 用户目视新版后关闭
    _wait_user_close_window(ev, case)
    return ev


def _elevated_action(args: argparse.Namespace, chain) -> int:
    result_path = Path(args.result) if args.result else (OUT / "managed-result.json")
    if args.elevated_child:
        try:
            ev = chain()
        except Exception as exc:
            ev = {"error": f"{type(exc).__name__}: {exc}",
                  "trace": traceback.format_exc()}
        result_path.write_text(json.dumps(ev, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        print(json.dumps(ev, ensure_ascii=False))
        return 0
    if _is_elevated():
        ev = chain()
        result_path.write_text(json.dumps(ev, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        print(json.dumps(ev, ensure_ascii=False))
        return 0
    print("需要提权上下文：将弹出一次 UAC 确认（唯一一次系统交互）……", flush=True)
    started = time.time()
    _run_elevated_child(result_path, [args.action]
                        + (["--poison"] if args.poison else [])
                        + (["--via-stub"] if args.via_stub else [])
                        + (["--shell", args.shell] if args.shell else [])
                        + (["--dev-root", args.dev_root] if args.dev_root else []))
    fresh = result_path.exists() and result_path.stat().st_mtime >= started - 2
    try:
        if not fresh:
            raise FileNotFoundError(result_path)
        print(result_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log = result_path.with_suffix(".launch.log")
        detail = log.read_text(encoding="utf-8", errors="replace")[-2000:] \
            if log.exists() else "(无启动日志)"
        print(f"提权子进程未写出新结果（可能被关闭控制台/杀进程终止）：{result_path}\n"
              f"启动日志尾部：\n{detail}")
        return 1
    return 0


def managed_manifests() -> None:
    feed = OUT / (OUT / "managed-feed.txt").read_text(encoding="utf-8")
    archive = next(feed.glob("*Portable.zip"))
    main = OUT / "managed-publish/MaaRacingMaster.Shell.exe"
    with zipfile.ZipFile(archive) as z:
        stub = OUT / "managed-generated-stub.exe"
        stub.write_bytes(z.read("MaaRMRealShellProbe.exe"))
    actual = {"shell": manifest_level(main), "generated_stub": manifest_level(stub)}
    assert actual == {"shell": "requireAdministrator", "generated_stub": "requireAdministrator"}, actual
    print(json.dumps({"check": "managed-elevation-manifests", **actual,
                      "scope": "PE resources only; actual UAC exercised by managed-update"}))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["hooks", "update", "nested-update",
                                           "real-shell", "manifests",
                                           "managed-update", "managed-launch",
                                           "managed-manifests", "fd-launch",
                                           "targets-launch", "p0-tail",
                                           "formal-update", "dev-smoke", "p3-chain"])
    parser.add_argument("--poison", action="store_true")
    parser.add_argument("--via-stub", action="store_true")
    parser.add_argument("--shell", default="")
    parser.add_argument("--dev-root", default="")
    parser.add_argument("--result", default="")
    parser.add_argument("--elevated-child", action="store_true")
    args = parser.parse_args()
    if args.action == "hooks":
        hooks()
    elif args.action == "real-shell":
        real_shell()
    elif args.action == "manifests":
        manifests()
    elif args.action == "managed-manifests":
        managed_manifests()
    elif args.action == "managed-update":
        return _elevated_action(args, _managed_update_chain)
    elif args.action == "managed-launch":
        return _elevated_action(args, lambda: _managed_launch_chain(args.poison, args.via_stub))
    elif args.action == "targets-launch":
        def chain():
            plans = json.loads((OUT / "launch-targets.json").read_text(encoding="utf-8"))
            out = {}
            real_ud = Path(os.environ["APPDATA"]) / "MaaRacingMaster"
            for p in plans:
                target = Path(p["exe"])
                out[p["label"]] = _launch_and_collect(
                    target, target.parent, real_ud,
                    OUT / f"report-{p['label']}.json", {}, p["label"],
                    detach=True, report_required=False)
            return out
        return _elevated_action(args, chain)
    elif args.action == "fd-launch":
        def chain():
            target = OUT / "managed-publish-fd" / SHELL_EXE
            assert target.is_file(), "先运行 prepare.py publish-fd"
            report = OUT / "report-fd.json"
            env = {"MAARM_PROBE_REPORT": str(report)}
            return _launch_and_collect(target, target.parent,
                                       Path(os.environ["APPDATA"]) / "MaaRacingMaster",
                                       report, env, "fd")
        return _elevated_action(args, chain)
    elif args.action == "p0-tail":
        return _elevated_action(args, _p0_tail_chain)
    elif args.action == "formal-update":
        return _elevated_action(args, _formal_update_chain)
    elif args.action == "dev-smoke":
        return _elevated_action(args, lambda: _dev_smoke_chain(args))
    elif args.action == "p3-chain":
        return _elevated_action(args, _p3_chain)
    else:
        update(nested=args.action == "nested-update")
    return 0


if __name__ == "__main__":
    sys.exit(main())
