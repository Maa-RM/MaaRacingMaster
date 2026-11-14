"""准备可复算的隔离工具与真实壳副本，不修改正式工程。"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import urllib.request
import uuid
import zipfile

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
OUT = HERE / "_out"
VERSION = "1.2.161"
SDK_SHA256 = "d57275692b299751a12ee3e18283b5f7fe3e55339f96b1623345da095dc484ed"


def sdk() -> None:
    OUT.mkdir(exist_ok=True)
    archive = OUT / "sdk.nupkg"
    if not archive.is_file():
        url = f"https://api.nuget.org/v3-flatcontainer/velopack/{VERSION}/velopack.{VERSION}.nupkg"
        with urllib.request.urlopen(url, timeout=30) as response:
            archive.write_bytes(response.read())
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == SDK_SHA256
    (OUT / "sdk").mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        for name in ("Velopack.dll", "Velopack.xml"):
            (OUT / "sdk" / name).write_bytes(z.read("lib/net8.0/" + name))


def publish(project: Path, target: Path) -> None:
    cmd = ["dotnet", "publish", str(project), "-c", "Release", "-o", str(target),
           "--configfile", str(HERE / "NuGet.Config"), "--self-contained", "false", "--nologo"]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    (OUT / (target.name + ".log")).write_text(p.stdout + p.stderr, encoding="utf-8")
    assert p.returncode == 0, p.stdout + p.stderr
    print("PUBLISH_OK", target.name)


def tool() -> None:
    cli = OUT / "cli"
    if not (cli / "vpk.exe").is_file():
        cmd = ["dotnet", "tool", "install", "vpk", "--version", VERSION,
               "--tool-path", str(cli), "--configfile", str(HERE / "NuGet.Config"),
               "--disable-parallel"]
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=150,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        (OUT / "tool-install.log").write_text(p.stdout + p.stderr, encoding="utf-8")
        assert p.returncode == 0, p.stdout + p.stderr
    listed = subprocess.run(["dotnet", "tool", "list", "--tool-path", str(cli)],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=15,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    assert listed.returncode == 0 and VERSION in listed.stdout, listed.stdout + listed.stderr
    print("TOOL_OK", VERSION)


def real_shell() -> None:
    copy = OUT / "shell-copy"
    project = copy / "apps" / "MaaRacingMaster.Shell"
    project.mkdir(parents=True, exist_ok=True)
    for src in (REPO / "apps" / "MaaRacingMaster.Shell").iterdir():
        if src.is_file() and src.suffix in (".cs", ".csproj", ".xaml", ".manifest"):
            shutil.copy2(src, project / src.name)
    (copy / "assets").mkdir(exist_ok=True)
    shutil.copy2(REPO / "assets/icon.ico", copy / "assets/icon.ico")
    csproj = project / "MaaRacingMaster.Shell.csproj"
    text = csproj.read_text(encoding="utf-8")
    ref = ('<ItemGroup><Reference Include="Velopack"><HintPath>'
           + str(OUT / "sdk/Velopack.dll")
           + '</HintPath></Reference></ItemGroup>')
    csproj.write_text(text.replace("</Project>", ref + "\n</Project>"), encoding="utf-8")
    program = project / "Program.cs"
    text = program.read_text(encoding="utf-8")
    needle = "private static void Main(string[] args)\n    {"
    assert needle in text
    injected = (needle + "\n        Velopack.VelopackApp.Build().SetAutoApplyOnStartup(false).Run();"
                '\n        if (args.Contains("--update-adapter-probe")) '
                '{ Console.WriteLine("REAL_SHELL_PRE_UI_OK"); return; }')
    program.write_text(text.replace(needle, injected, 1), encoding="utf-8")
    publish(csproj, OUT / "real-shell-publish")


def pack_real_shell() -> None:
    feed = OUT / ("real-shell-feed-" + uuid.uuid4().hex[:8])
    cmd = [str(OUT / "cli/vpk.exe"), "pack", "--packId", "MaaRM.RealShellProbe",
           "--packTitle", "MaaRMRealShellProbe", "--packVersion", "0.1.0",
           "--runtime", "win-x64", "--packDir", str(OUT / "real-shell-publish"),
           "--mainExe", "MaaRacingMaster.Shell.exe", "--outputDir", str(feed),
           "--noInst", "--delta", "None"]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    (OUT / "real-shell-pack.log").write_text(p.stdout + p.stderr, encoding="utf-8")
    assert p.returncode == 0, p.stdout + p.stderr
    (OUT / "real-shell-feed.txt").write_text(feed.name, encoding="utf-8")
    print("REAL_SHELL_PACK_OK")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["sdk", "tool", "probe", "real-shell", "pack-real-shell"])
    args = parser.parse_args()
    sdk()
    if args.action == "tool":
        tool()
    elif args.action == "probe":
        publish(HERE / "Probe/HookProbe.csproj", OUT / "publish")
    elif args.action == "real-shell":
        real_shell()
    elif args.action == "pack-real-shell":
        pack_real_shell()


if __name__ == "__main__":
    main()
