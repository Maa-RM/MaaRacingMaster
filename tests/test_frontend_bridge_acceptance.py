# -*- coding: utf-8 -*-
"""前端页面装载验收：用桥桩驱动真实的 index.html + 全部脚本，在无头浏览器里跑四种场景。

为什么要有这一层：其余前端测试大多是源码文本锁，只能证明「接线还在」，证明不了
「页面真的能走完」。这里用一份临时副本 + `window.chrome.webview` 桥桩，在真实
渲染路径上验证四件事：

1. normal    —— 正常回包时预热走完、遮罩收起、通知宿主落完成标记；
2. noreply   —— 后端完全不回包时，总时限兜底放出界面，且不写完成标记；
3. latepopup —— 弹窗回包晚于总时限时，迟到弹窗会自行关掉，不会留在界面上；
4. slowdash  —— 看板查询慢于轮询周期时，同一看板复用同一个在途请求，内容最终落地。

浏览器用本机已有的 Edge/Chrome（Windows）或 CI runner 自带的 Chrome/Chromium；
都没有时本地跳过、CI 直接失败（不再静默跳过）。不引入任何第三方依赖。
"""
from __future__ import annotations

import html as html_lib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FRONTEND = REPO / "apps" / "MaaRacingMaster.Shell" / "frontend"
IS_CI = os.environ.get("CI") == "true"

# 场景脚本：驱动桥桩与真实页面；结果写到 <pre id="probe-out">。
_STUB = r"""
<script>
(function () {
  // 无头 + 虚拟时间下不保证产生合成帧，rAF 可能不回调；预热的分帧等待改用定时器
  // 等价驱动（16ms/帧），否则页面会在第一步永远等不到下一帧。
  window.requestAnimationFrame = function (cb) {
    return setTimeout(function () { cb(performance.now()); }, 16);
  };
  window.cancelAnimationFrame = function (id) { clearTimeout(id); };
  window.__logs = [];
  ['error', 'warn'].forEach(function (level) {
    var orig = console[level];
    console[level] = function () {
      try {
        window.__logs.push(level + ': ' + Array.prototype.map.call(arguments, String).join(' '));
      } catch (e) {}
      return orig.apply(console, arguments);
    };
  });
  var scen = window.__SCEN || 'normal';
  window.__sent = [];
  window.__dashCalls = 0;
  window.__native = null;
  function reply(msg, data, delay) {
    setTimeout(function () {
      if (window.__native) {
        window.__native({ data: { type: 'response', callId: msg.callId, ok: true, data: data } });
      }
    }, delay || 0);
  }
  var payloads = {
    get_initial_state: {
      version: '9.9.9',
      modules: [{ id: 'demo', name: '演示模块', kind: 'module', has_dashboard: true }],
      queue: { order: ['demo'], enabled: { demo: true } },
      breakpoints: {},
      module_assets: {},
      cover_default: '',
      is_running: false,
      model_ok: true
    },
    get_status: { is_running: false, model_ok: true },
    fetch_logs: { lines: [] },
    get_registry_optimizations: { items: [] },
    get_optional_dependencies: { items: [] },
    webview2_status: { installed: '157.0.0.0', running: '157.0.0.0' },
    get_debug_state: {},
    get_achievements: {
      state: 'ready',
      frozen_reason: null,
      achievements: [{
        global_id: 'demo.first', mod_id: 'demo', title: '演示成就',
        desc: '用于页面装载验收', tier: 'normal', unlocked: true
      }]
    },
    update_status: { revision: 1, phase: 'idle', source: 'cnb', channel: 'stable', auto_check: true },
    fetch_announcement: { level: 'none' }
  };
  window.chrome = {
    webview: {
      addEventListener: function (type, fn) { if (type === 'message') window.__native = fn; },
      postMessage: function (msg) {
        window.__sent.push(msg);
        if (!msg || msg.type !== 'call') return;
        if (scen === 'noreply') return;
        var method = msg.method;
        if (method === 'get_module_dashboard') {
          window.__dashCalls += 1;
          reply(msg, { subtitle: '演示看板', blocks: [{ type: 'text', text: '看板已就绪' }] },
                scen === 'slowdash' ? 5000 : 0);
          return;
        }
        if (method === 'get_data_usage') {
          reply(msg, { root: 'C:/stub', total: 10, dirs: [{ key: 'logs', label: '日志', bytes: 10 }] },
                scen === 'latepopup' ? 13000 : 0);
          return;
        }
        reply(msg, payloads[method] || {}, 0);
      }
    }
  };
})();
</script>
"""

_PROBE = r"""
<script>
(function () {
  var start = performance.now();
  var hiddenAt = 0;
  var watch = setInterval(function () {
    var o = document.getElementById('boot-overlay');
    var hidden = !o || o.hidden === true || getComputedStyle(o).display === 'none';
    if (hidden && !hiddenAt) hiddenAt = Math.round(performance.now() - start);
  }, 100);
  setTimeout(function () {
    clearInterval(watch);
    var o = document.getElementById('boot-overlay');
    var body = document.getElementById('dash-body');
    var step = document.querySelector('.boot-step');
    var out = {
      hiddenAt: hiddenAt,
      hidden: !o || o.hidden === true,
      display: o ? getComputedStyle(o).display : 'none',
      step: step ? step.textContent : '',
      modals: document.querySelectorAll('.maarm-modal-overlay').length,
      warmupDone: (window.__sent || []).some(function (m) { return m.type === 'warmup-done'; }),
      dashCalls: window.__dashCalls || 0,
      dashLoaded: body ? (body.dataset.loadedFor || '') : '',
      logs: (window.__logs || []).slice(-12)
    };
    var pre = document.createElement('pre');
    pre.id = 'probe-out';
    pre.textContent = JSON.stringify(out);
    document.body.appendChild(pre);
  }, 15000);
})();
</script>
"""


def _find_browser() -> Path | None:
    candidates = []
    if sys.platform == "win32":
        for base in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"), os.environ.get("LOCALAPPDATA")):
            if not base:
                continue
            candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
            candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    for name in ("msedge", "chrome", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for path in candidates:
        if path.is_file():
            return path
    return None


BROWSER = _find_browser()
if BROWSER is None and IS_CI:
    pytest.fail("CI 环境没有可用的 Chromium 内核浏览器，页面装载验收不能被静默跳过", pytrace=False)


def _prepare(tmp_path: Path, scenario: str) -> Path:
    root = tmp_path / scenario
    shutil.copytree(FRONTEND, root, ignore=shutil.ignore_patterns(".mimosa"))
    index = root / "index.html"
    text = index.read_text(encoding="utf-8")
    head = "<script>window.__SCEN=" + json.dumps(scenario) + ";</script>\n" + _STUB
    text = text.replace("<head>", "<head>\n" + head, 1)
    text = text.replace("</body>", _PROBE + "\n</body>", 1)
    index.write_text(text, encoding="utf-8")
    return index


def _run(tmp_path: Path, scenario: str) -> dict:
    index = _prepare(tmp_path, scenario)
    profile = tmp_path / ("profile-" + scenario)
    url = index.as_uri() + "?warmup=1"
    proc = subprocess.run(
        [
            str(BROWSER), "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--disable-extensions", "--disable-background-networking",
            "--no-sandbox", "--disable-dev-shm-usage",
            "--user-data-dir=" + str(profile),
            "--virtual-time-budget=16000", "--dump-dom", url,
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    match = re.search(r'<pre id="probe-out">(.*?)</pre>', proc.stdout, re.S)
    assert match, "页面没有写出验收结果（浏览器输出末尾）：\n" + proc.stdout[-2000:] + "\n" + proc.stderr[-1000:]
    return json.loads(html_lib.unescape(match.group(1)))


@pytest.mark.skipif(BROWSER is None, reason="未找到 Chromium 内核浏览器（Windows 的 Edge/Chrome 或 Linux 的 chrome/chromium）")
def test_normal_startup_completes_and_marks_done(tmp_path):
    out = _run(tmp_path, "normal")
    assert out["hidden"] is True, "正常回包时遮罩必须收起"
    assert out["step"] == "完成", f"正常回包应走完预热，实际步骤文案：{out['step']}；日志：{out.get('logs')}"
    assert out["warmupDone"] is True, "预热完成必须通知宿主落标记"
    assert out["modals"] == 0, "预热收尾后不应留下自己打开的弹窗"
    assert out["hiddenAt"] < 10000, f"正常启动不应拖到接近总时限：{out['hiddenAt']}ms"


@pytest.mark.skipif(BROWSER is None, reason="未找到 Chromium 内核浏览器")
def test_no_reply_releases_ui_without_marking_done(tmp_path):
    out = _run(tmp_path, "noreply")
    assert out["hidden"] is True, "后端不回包时也必须按总时限放出界面"
    assert out["step"] == "启动较慢，先进入界面", f"兜底文案不对：{out['step']}"
    assert out["warmupDone"] is False, "没画完的预热不得写完成标记"
    assert out["hiddenAt"] >= 10000, f"兜底应接近 12 秒时限才放行，实际 {out['hiddenAt']}ms"


@pytest.mark.skipif(BROWSER is None, reason="未找到 Chromium 内核浏览器")
def test_late_dialog_closes_itself_after_deadline(tmp_path):
    out = _run(tmp_path, "latepopup")
    assert out["hidden"] is True
    assert out["warmupDone"] is False, "有步骤被总时限截断，不应写完成标记"
    assert out["modals"] == 0, "晚于总时限才弹出的面板必须自行关闭，不能留在界面上"


@pytest.mark.skipif(BROWSER is None, reason="未找到 Chromium 内核浏览器")
def test_slow_dashboard_reuses_inflight_request(tmp_path):
    out = _run(tmp_path, "slowdash")
    assert out["dashLoaded"] == "demo", "慢回包也必须把看板内容画出来"
    assert out["dashCalls"] <= 3, f"同一看板的轮询不应反复发新请求：{out['dashCalls']} 次"
    assert out["modals"] == 0
    assert out["warmupDone"] is True, "看板慢但仍在时限内画完，应正常写标记"
