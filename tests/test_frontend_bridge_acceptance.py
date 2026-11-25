# -*- coding: utf-8 -*-
"""前端页面装载验收：用桥桩驱动真实的 index.html + 全部脚本，在无头浏览器里跑多种场景。

为什么要有这一层：其余前端测试大多是源码文本锁，只能证明「接线还在」，证明不了
「页面真的能走完」。这里用一份临时副本 + `window.chrome.webview` 桥桩，在真实
渲染路径上验证：

1. normal      —— 正常回包时预热走完、遮罩收起、通知宿主落完成标记；关于页渲染出
                  版本/更新/公告，并真的点一次「检查更新」发出宿主动作；
2. noreply     —— 后端完全不回包时，总时限兜底放出界面，且不写完成标记；
3. latepopup   —— 弹窗回包晚于总时限时，迟到的回包不再开窗（遮罩撤下后不得冒弹窗）；
4. slowdash    —— 看板查询慢于轮询周期时，同一看板复用同一个在途请求，内容最终落地；
5. dashfail    —— 看板回错时不得写完成标记（预热失败就是失败）；
6. envfail     —— 环境中心三个页签回错时不得写完成标记（错误文案不当内容已就绪）；
7. rebuilddash —— 慢看板在途时数据页被重建，回包不得写进已移除的节点，新页要重新取数；
8. slowinit    —— 初始化晚于总时限才回包：只能收尾一次（不得二次体检、不得再关用户弹窗）；
9. initfail    —— 初始化直接报错：不得写完成标记；
10. announcefail —— 启动公告拉取失败、网络恢复后切进关于页要能补拉出公告；
11. remindfocus —— 勾选「本次更新不再提示」后焦点仍留在同一个复选框上。

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
  window.__regCalls = 0;
  window.__announceCalls = 0;
  window.__native = null;
  function reply(msg, data, delay) {
    setTimeout(function () {
      if (window.__native) {
        window.__native({ data: { type: 'response', callId: msg.callId, ok: true, data: data } });
      }
    }, delay || 0);
  }
  function replyErr(msg, message, delay) {
    setTimeout(function () {
      if (window.__native) {
        window.__native({ data: { type: 'response', callId: msg.callId, ok: false, error: message } });
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
    update_check: { revision: 2, phase: 'idle', source: 'cnb', channel: 'stable', auto_check: true },
    fetch_announcement: { level: 'none' }
  };
  // 发现更新相位：用于验证「本次更新不再提示」勾选后的焦点保持
  var availableStatus = {
    revision: 1, phase: 'available', source: 'cnb', channel: 'stable', auto_check: true,
    target_version: '9.9.10', dismissed_version: null
  };
  var availableDismissed = {
    revision: 2, phase: 'available', source: 'cnb', channel: 'stable', auto_check: true,
    target_version: '9.9.10', dismissed_version: '9.9.10'
  };
  window.chrome = {
    webview: {
      addEventListener: function (type, fn) { if (type === 'message') window.__native = fn; },
      postMessage: function (msg) {
        window.__sent.push(msg);
        if (!msg || msg.type !== 'call') return;
        if (scen === 'noreply') return;
        var method = msg.method;
        if (method === 'get_initial_state') {
          if (scen === 'initfail') { replyErr(msg, 'stub init failure', 0); return; }
          if (scen === 'slowinit') { reply(msg, payloads.get_initial_state, 13000); return; }
        }
        if (method === 'get_registry_optimizations') window.__regCalls += 1;
        if (scen === 'remindfocus' && method === 'update_status') {
          reply(msg, availableStatus, 0);
          return;
        }
        if (scen === 'remindfocus' && method === 'update_set_dismissed') {
          reply(msg, availableDismissed, 0);
          return;
        }
        if (method === 'fetch_announcement') {
          window.__announceCalls += 1;
          // 启动那次拉取失败，之后（网络恢复）返回真实公告：验证切页补拉的出口
          if (scen === 'announcefail' && window.__announceCalls === 1) {
            replyErr(msg, 'stub announcement offline', 0);
            return;
          }
          if (scen === 'announcefail') {
            reply(msg, {
              level: 'info', title: '网络恢复后的公告', body: '', date: '2026-11-25'
            }, 0);
            return;
          }
        }
        if (method === 'get_module_dashboard') {
          window.__dashCalls += 1;
          if (scen === 'dashfail') { replyErr(msg, 'stub dashboard failure', 0); return; }
          reply(msg, { subtitle: '演示看板', blocks: [{ type: 'text', text: '看板已就绪' }] },
                scen === 'slowdash' ? 5000 : (scen === 'rebuilddash' ? 2500 : 0));
          return;
        }
        if (method === 'get_data_usage') {
          reply(msg, { root: 'stub-root', total: 10, dirs: [{ key: 'logs', label: '日志', bytes: 10 }] },
                scen === 'latepopup' ? 13000 : 0);
          return;
        }
        if (scen === 'envfail' && (method === 'get_registry_optimizations'
            || method === 'get_optional_dependencies' || method === 'webview2_status')) {
          replyErr(msg, 'stub env failure', 0);
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
  var scen = window.__SCEN || 'normal';
  var hiddenAt = 0;
  var modalsAfterHidden = false;
  var rebuildAt = 0;
  var rebuildDoneAt = 0;
  var loadedAt = 0;
  var bootFocus = null;
  var remindFocus = null;
  // 遮罩期间键盘隔离：焦点不得落到被遮住的「开始」上（inert 生效时 focus 会被拒绝）
  setTimeout(function () {
    var start = document.getElementById('btn-start');
    var main = document.querySelector('main');
    var header = document.getElementById('titlebar');
    var overlay = document.getElementById('boot-overlay');
    if (start) start.focus();
    bootFocus = {
      overlayUp: !!(overlay && !overlay.hidden),
      inertMain: !!(main && main.inert),
      inertHeader: !!(header && header.inert),
      activeId: document.activeElement ? (document.activeElement.id || document.activeElement.tagName) : ''
    };
  }, 300);
  // 勾选「本次更新不再提示」后，焦点必须还留在同一个复选框节点上
  if (scen === 'remindfocus') {
    setTimeout(function () {
      var about = document.querySelector('.maarm-tab[data-tab="about"]');
      if (about) about.click(); // 关于页在后台是 display:none，先切过去才能聚焦
      setTimeout(function () {
        var cb = document.querySelector('#upd-detail input[type="checkbox"]');
        remindFocus = { found: !!cb, focusKept: null, hint: '' };
        if (!cb) return;
        cb.focus();
        cb.click();
        setTimeout(function () {
          var live = document.querySelector('#upd-detail input[type="checkbox"]');
          remindFocus.focusKept = document.activeElement === cb && live === cb;
          remindFocus.hint = (document.querySelector('.upd-remind-hint') || {}).textContent || '';
        }, 400);
      }, 200);
    }, 9000);
  }
  // 启动公告拉取失败后网络恢复：切进关于页应补拉出来
  if (scen === 'announcefail') {
    setTimeout(function () {
      var about = document.querySelector('.maarm-tab[data-tab="about"]');
      if (about) about.click();
      setTimeout(function () {
        var control = document.querySelector('.maarm-tab[data-tab="control"]');
        if (control) control.click();
      }, 300);
    }, 8000);
  }
  var watch = setInterval(function () {
    var o = document.getElementById('boot-overlay');
    var hidden = !o || o.hidden === true || getComputedStyle(o).display === 'none';
    if (hidden && !hiddenAt) hiddenAt = Math.round(performance.now() - start);
    // 遮罩撤下后用户已经看到界面：这时再冒出预热弹窗就是行为问题
    if (hiddenAt && document.querySelector('.maarm-modal-overlay')) modalsAfterHidden = true;
    if (hiddenAt && scen === 'rebuilddash' && !rebuildAt) {
      rebuildAt = Math.round(performance.now() - start);
      var ctrl = document.getElementById('page-control');
      var data = document.getElementById('page-data');
      if (ctrl) ctrl.classList.add('hidden');
      if (data) data.classList.remove('hidden');
      // 先发一轮慢看板请求，趁它还在途时重建数据页：旧回包只能写进已移除的节点
      if (window.MaaRM && window.MaaRM.pollDashboard) window.MaaRM.pollDashboard();
      setTimeout(function () {
        if (window.MaaRM && window.MaaRM.renderModulePages) window.MaaRM.renderModulePages('demo');
        rebuildDoneAt = Math.round(performance.now() - start);
      }, 500);
    }
    if (rebuildDoneAt && !loadedAt) {
      var live = document.getElementById('dash-body');
      if (live && live.dataset.loadedFor === 'demo') {
        loadedAt = Math.round(performance.now() - start);
      }
    }
  }, 100);
  if (scen === 'normal') {
    setTimeout(function () {
      // 顺手点一次「检查更新」：验证关于页更新卡真的接上了宿主动作（不只是渲染静态文本）
      var action = document.getElementById('btn-update-action');
      if (action) action.click();
    }, 13500);
  }
  var announceBeforeSwitch = 0;
  setTimeout(function () {
    // 切页链路：公告刷新绑在更新检查上，来回切关于页不得再打 fetch_announcement
    announceBeforeSwitch = countCalls('fetch_announcement');
    var about = document.querySelector('.maarm-tab[data-tab="about"]');
    var control = document.querySelector('.maarm-tab[data-tab="control"]');
    if (about) about.click();
    setTimeout(function () { if (control) control.click(); }, 120);
    setTimeout(function () { if (about) about.click(); }, 240);
  }, 14000);
  function countCalls(method) {
    return (window.__sent || []).filter(function (m) {
      return m.type === 'call' && m.method === method;
    }).length;
  }
  var tabOk = null;
  setTimeout(function () {
    // 遮罩撤下后 tab 必须可点：热启动没有遮罩，若「已放行」标志没初始化，切页会被一直挡住
    var tab = document.querySelector('.maarm-tab[data-tab="about"]');
    if (tab) tab.click();
    var about = document.getElementById('page-about');
    tabOk = !!(about && !about.classList.contains('hidden'));
  }, 15000);
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
      modalsAfterHidden: modalsAfterHidden,
      warmupDone: (window.__sent || []).some(function (m) { return m.type === 'warmup-done'; }),
      updateCheckCalls: (window.__sent || []).filter(function (m) {
        return m.type === 'call' && m.method === 'update_check';
      }).length,
      dashCalls: window.__dashCalls || 0,
      dashLoaded: body ? (body.dataset.loadedFor || '') : '',
      dashText: body ? body.textContent.trim() : '',
      rebuildAt: rebuildAt,
      rebuildDoneAt: rebuildDoneAt,
      loadedAt: loadedAt,
      version: (document.getElementById('app-version') || {}).textContent || '',
      updTitle: (document.getElementById('upd-title') || {}).textContent || '',
      updateLabel: (document.getElementById('btn-update-action-label') || {}).textContent || '',
      announce: (document.getElementById('about-announce-body') || {}).textContent || '',
      tabOk: tabOk,
      bootFocus: bootFocus,
      finalInert: !!(document.querySelector('main') && document.querySelector('main').inert),
      regCalls: window.__regCalls || 0,
      remindFocus: remindFocus,
      announceCalls: countCalls('fetch_announcement'),
      announceBeforeSwitch: announceBeforeSwitch,
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


def _run(tmp_path: Path, scenario: str, warmup: bool = True) -> dict:
    index = _prepare(tmp_path, scenario)
    profile = tmp_path / ("profile-" + scenario)
    url = index.as_uri() + ("?warmup=1" if warmup else "")
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
    # 关于页的更新与公告已拆到独立模块：这几个字段证明它完成加载、订阅并渲染了快照
    assert out["version"] == "v9.9.9", f"关于页版本未落地：{out['version']!r}"
    assert out["updateLabel"] == "检查更新", f"更新卡主按钮未按快照渲染：{out['updateLabel']!r}"
    assert out["announce"] == "暂无公告", f"公告卡未渲染：{out['announce']!r}"
    assert out["announceCalls"] == 2, (
        f"公告该在启动与手动检查收尾各拉一次，实际 {out['announceCalls']} 次")
    assert out["announceBeforeSwitch"] == out["announceCalls"], (
        "来回切关于页又重拉公告了：刷新应跟随更新检查")
    assert out["updateCheckCalls"] == 1, "点击「检查更新」必须真的发出宿主动作"
    assert out["updateLabel"] == "检查更新", "检查回包后主按钮应回到可再次点击的态"
    assert out["modalsAfterHidden"] is False, "遮罩撤下后不得再冒出预热弹窗"
    assert out["tabOk"] is True, "遮罩撤下后点击关于 tab 未切页（放行标志没生效）"
    assert out["finalInert"] is False, "遮罩放行后没有恢复顶栏/正文/底栏的焦点序列"


@pytest.mark.skipif(BROWSER is None, reason="未找到 Chromium 内核浏览器")
def test_late_init_finalizes_once_and_blocks_keyboard(tmp_path):
    """初始化晚于总时限才回包：兜底已经收过尾，晚到的初始化不得再收一遍
    （二次体检、关掉用户弹窗），也不得借遮罩期间的键盘焦点启动任务。"""
    out = _run(tmp_path, "slowinit")
    assert out["hidden"] is True, "兜底后遮罩必须收起"
    assert out["warmupDone"] is False, "初始化未及时到位，不得写完成标记"
    assert out["step"] == "启动较慢，先进入界面", f"兜底文案被二次收尾改写：{out['step']!r}"
    assert out["regCalls"] == 1, (
        f"启动体检应只跑一次（晚到初始化又收了一遍尾）：{out['regCalls']} 次")
    assert out["modalsAfterHidden"] is False, "二次收尾不得在放行后再开弹窗"
    bf = out["bootFocus"] or {}
    assert bf.get("overlayUp") is True, "300ms 时遮罩应仍在"
    assert bf.get("inertMain") is True and bf.get("inertHeader") is True, (
        "遮罩期间正文/顶栏未从焦点序列隔离")
    assert bf.get("activeId") != "btn-start", "焦点落到了被遮住的「开始」按钮"
    assert out["finalInert"] is False, "收尾后未恢复焦点序列"


@pytest.mark.skipif(BROWSER is None, reason="未找到 Chromium 内核浏览器")
def test_init_failure_releases_without_marking_done(tmp_path):
    """初始化直接报错：预热没有意义，放出界面但不得写完成标记（否则下次不再预热）。"""
    out = _run(tmp_path, "initfail")
    assert out["hidden"] is True, "初始化失败也必须放出界面"
    assert out["warmupDone"] is False, "初始化失败时不得写完成标记"
    assert out["hiddenAt"] < 5000, f"初始化失败应立即放行：{out['hiddenAt']}ms"


@pytest.mark.skipif(BROWSER is None, reason="未找到 Chromium 内核浏览器")
def test_announcement_retries_on_about_tab_after_network_recovery(tmp_path):
    """普通 ZIP 没有后台检查：启动公告拉取失败后，切进关于页要能补拉出公告。"""
    out = _run(tmp_path, "announcefail")
    assert "网络恢复后的公告" in out["announce"], (
        f"切进关于页没有补拉公告：{out['announce']!r}")
    assert out["announceCalls"] == 2, (
        f"公告应只在启动失败后补拉一次，实际 {out['announceCalls']} 次")


@pytest.mark.skipif(BROWSER is None, reason="未找到 Chromium 内核浏览器")
def test_dismiss_checkbox_keeps_focus_after_rerender(tmp_path):
    """勾选「本次更新不再提示」会回一张新快照：原地更新，焦点不得从复选框掉到 body。"""
    out = _run(tmp_path, "remindfocus")
    rf = out["remindFocus"] or {}
    assert rf.get("found") is True, "发现更新相位未渲染提醒复选框"
    assert rf.get("focusKept") is True, "勾选后复选框被重建，键盘焦点丢失"
    assert "下个版本会重新提示" in (rf.get("hint") or ""), (
        f"勾选后的说明没有就地更新：{rf.get('hint')!r}")


@pytest.mark.skipif(BROWSER is None, reason="未找到 Chromium 内核浏览器")
def test_hot_start_keeps_tabs_clickable(tmp_path):
    """热启动没有遮罩（首帧 boot-skip），切页不能被「已放行」标志挡住——
    否则第二次启动起四个 tab 全部点不动。"""
    out = _run(tmp_path, "normal", warmup=False)
    assert out["display"] == "none", f"热启动不该显示预热遮罩：display={out['display']}"
    assert out["tabOk"] is True, "热启动后点击关于 tab 未切页"


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
    assert out["modalsAfterHidden"] is False, (
        "遮罩撤下后回包才到：不得再开预热弹窗抢焦点")


@pytest.mark.skipif(BROWSER is None, reason="未找到 Chromium 内核浏览器")
def test_dashboard_error_does_not_mark_warmup_done(tmp_path):
    """看板回错 = 该预热面没画出来，不能写完成标记。"""
    out = _run(tmp_path, "dashfail")
    assert out["hidden"] is True, "失败也要按时放出界面"
    assert out["warmupDone"] is False, "看板回错时不得写完成标记"
    assert out["step"].startswith("未完成"), f"步骤文案应标记未完成：{out['step']!r}"


@pytest.mark.skipif(BROWSER is None, reason="未找到 Chromium 内核浏览器")
def test_env_center_error_does_not_mark_warmup_done(tmp_path):
    """环境中心回错写的是错误文案，不能被当成「内容已就绪」。"""
    out = _run(tmp_path, "envfail")
    assert out["hidden"] is True, "失败也要按时放出界面"
    assert out["warmupDone"] is False, "环境中心回错时不得写完成标记"
    assert out["step"].startswith("未完成"), f"步骤文案应标记未完成：{out['step']!r}"


@pytest.mark.skipif(BROWSER is None, reason="未找到 Chromium 内核浏览器")
def test_dashboard_rebuild_fetches_into_live_node(tmp_path):
    """慢看板在途时重建数据页：旧回包不得写进已移除的节点，新页要立刻重取。"""
    out = _run(tmp_path, "rebuilddash")
    assert out["rebuildDoneAt"] > 0, "场景没有触发数据页重建"
    assert out["loadedAt"] > 0, "重建后看板内容没有落地"
    elapsed = out["loadedAt"] - out["rebuildDoneAt"]
    assert elapsed <= 4000, (
        f"重建后应立刻重取看板，不能等旧请求作废再白等一轮：{elapsed}ms（"
        f"rebuildDoneAt={out['rebuildDoneAt']}, loadedAt={out['loadedAt']}）")
    assert "看板已就绪" in out["dashText"], f"新节点没有收到看板内容：{out['dashText']!r}"


@pytest.mark.skipif(BROWSER is None, reason="未找到 Chromium 内核浏览器")
def test_slow_dashboard_reuses_inflight_request(tmp_path):
    out = _run(tmp_path, "slowdash")
    assert out["dashLoaded"] == "demo", "慢回包也必须把看板内容画出来"
    assert out["dashCalls"] <= 3, f"同一看板的轮询不应反复发新请求：{out['dashCalls']} 次"
    assert out["modals"] == 0
    assert out["warmupDone"] is True, "看板慢但仍在时限内画完，应正常写标记"
