import json
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "apps" / "MaaRacingMaster.Shell" / "tools" / "gui-debug.cjs"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher")
def test_dev_shell_help_lists_opt_in_gui_debug():
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "dev-shell.cmd", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "--gui-debug" in result.stdout
    assert "--no-launch" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="GUI tool uses Node.js")
def test_gui_debug_reports_unavailable_local_endpoint():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        result = subprocess.run(
            ["node", str(TOOL), "check", str(port)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
    assert result.returncode == 1, result.stderr
    assert "CDP" in json.loads(result.stderr)["error"]
