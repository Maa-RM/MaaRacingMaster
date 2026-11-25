/* 首次使用引导 demo —— 声明纯色页 + 聚光灯引导。
   正式实现里声明状态与引导进度由后端持有，前端只渲染；这里用 localStorage 模拟。 */
(function () {
  'use strict';

  var TERMS_VERSION = '2026-11-A';
  var READ_MS = 5000;
  var KEY = 'maarm.onboarding.demo.v2';

  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var delay = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };

  var TASKS = {
    start_game: { name: '启动游戏', desc: '打开游戏并等待进入大厅' },
    close_game: { name: '关闭游戏', desc: '运行结束后退出游戏进程' },
    infinite_drive: { name: '无限驾驶', desc: '需要先安装车道线模型' }
  };

  function defaultState() {
    return {
      terms: { version: TERMS_VERSION, acceptedAt: null },
      tab: 'control',
      tasks: [],
      modelReady: false,
      tourIndex: 0,
      tourDone: false
    };
  }

  var state = defaultState();
  var SNAP = false;
  var countdown = { raf: 0, active: false, done: false };
  var tourIndex = 0;
  var tourActive = false;

  // ---------- 持久化 ----------
  function load() {
    try {
      var raw = localStorage.getItem(KEY);
      if (!raw) return;
      var p = JSON.parse(raw);
      if (!p || typeof p !== 'object') return;
      state.terms = p.terms || state.terms;
      state.tab = p.tab === 'settings' ? 'settings' : 'control';
      state.tasks = Array.isArray(p.tasks) ? p.tasks.filter(function (id) { return TASKS[id]; }) : [];
      state.modelReady = !!p.modelReady;
      state.tourIndex = Number.isInteger(p.tourIndex) ? p.tourIndex : 0;
      state.tourDone = !!p.tourDone;
    } catch (e) { /* 读不动按首次处理 */ }
  }
  function save() {
    try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) { /* demo 忽略 */ }
  }
  function termsAccepted() {
    return state.terms.version === TERMS_VERSION && !!state.terms.acceptedAt;
  }

  // ---------- 应用交互 ----------
  function setTab(name) {
    state.tab = name === 'settings' ? 'settings' : 'control';
    $$('.tab').forEach(function (b) { b.classList.toggle('is-active', b.dataset.tab === state.tab); });
    $('#page-control').hidden = state.tab !== 'control';
    $('#page-settings').hidden = state.tab !== 'settings';
    save();
  }

  function openSheet(name) {
    var el = $('#sheet-' + name);
    if (el) el.hidden = false;
  }
  function closeSheet(name) {
    var el = $('#sheet-' + name);
    if (el) el.hidden = true;
  }
  function sheetOpen(name) {
    var el = $('#sheet-' + name);
    return !!el && !el.hidden;
  }

  function renderTasks() {
    var list = $('#task-list');
    if (!state.tasks.length) {
      list.innerHTML = '<li class="task-empty"><b>还没有要运行的任务</b><p>点右上角的按钮把模块加进列表。</p></li>';
    } else {
      list.innerHTML = state.tasks.map(function (id, i) {
        var d = TASKS[id];
        return '<li class="task-row"><span class="idx">' + (i + 1) + '</span>'
          + '<span><b>' + d.name + '</b><em>' + d.desc + '</em></span></li>';
      }).join('');
    }
    $$('.mod-card').forEach(function (card) {
      card.classList.toggle('is-added', state.tasks.indexOf(card.dataset.mod) !== -1);
    });
    $('#manage-count').textContent = '已添加 ' + state.tasks.length + ' 项';
    updateStatus();
  }

  function updateStatus() {
    var dot = $('#status-dot');
    var text = $('#status-text');
    var missingModel = state.tasks.indexOf('infinite_drive') !== -1 && !state.modelReady;
    if (!state.tasks.length) {
      dot.classList.add('is-warn'); text.textContent = '还没有要运行的任务';
    } else if (missingModel) {
      dot.classList.add('is-warn'); text.textContent = '插件资源缺失';
    } else {
      dot.classList.remove('is-warn'); text.textContent = '系统就绪';
    }
  }

  function toggleTask(id) {
    if (!TASKS[id]) return;
    var i = state.tasks.indexOf(id);
    if (i === -1) state.tasks.push(id); else state.tasks.splice(i, 1);
    renderTasks(); save();
  }

  function installModel() {
    if (state.modelReady) return;
    var row = $('.env-row');
    var bar = $('#model-bar');
    row.classList.add('is-busy');
    bar.style.width = '0%';
    requestAnimationFrame(function () { bar.style.width = '100%'; });
    $('#btn-install').disabled = true;
    setTimeout(function () {
      row.classList.remove('is-busy');
      state.modelReady = true;
      $('#model-state').textContent = '已安装';
      $('#model-state').classList.add('is-ready');
      $('#btn-install').textContent = '已安装';
      save(); updateStatus();
    }, 1200);
  }

  // ---------- 声明门 ----------
  function startCountdown() {
    if (countdown.active || countdown.done) return;
    countdown.active = true;
    var t0 = performance.now();
    function tick(now) {
      if (!countdown.active) return;
      var p = Math.min(1, (now - t0) / READ_MS);
      var left = Math.max(1, Math.ceil((1 - p) * READ_MS / 1000));
      $('#gate-accept-label').textContent = p >= 1 ? '同意并继续' : '请阅读 ' + left + ' 秒';
      if (p >= 1) { unlockAccept(); return; }
      countdown.raf = requestAnimationFrame(tick);
    }
    countdown.raf = requestAnimationFrame(tick);
  }
  function unlockAccept() {
    countdown.active = false;
    countdown.done = true;
    cancelAnimationFrame(countdown.raf);
    $('#gate-accept').disabled = false;
    $('#gate-accept-label').textContent = '同意并继续';
  }
  function skipCountdown() {
    if (countdown.done) return;
    unlockAccept();
  }

  function showGate() {
    $('#gate').hidden = false;
    $('#app').inert = true;
    $('#gate-accept').disabled = true;
    $('#gate-accept-label').textContent = '请阅读 5 秒';
    if (SNAP) skipCountdown(); else startCountdown();
  }

  function acceptTerms() {
    state.terms = { version: TERMS_VERSION, acceptedAt: new Date().toISOString() };
    save();
    countdown.active = false;
    cancelAnimationFrame(countdown.raf);
    var gate = $('#gate');
    gate.classList.add('is-leaving');
    setTimeout(function () {
      gate.hidden = true;
      gate.classList.remove('is-leaving');
      $('#app').inert = false;
      startTour(state.tourIndex || 0);
    }, 260);
  }

  // ---------- 聚光灯步骤 ----------
  // 每步：目标选择器、进入前的页面准备、完成判据（结果状态而不是点击事件）。
  var STEPS = [
    {
      target: '#btn-manage',
      title: '打开模块管理',
      body: '点这个按钮，把要运行的东西加进任务列表。',
      prepare: function () { setTab('control'); closeSheet('manage'); closeSheet('env'); },
      done: function () { return sheetOpen('manage'); }
    },
    {
      target: '.mod-card[data-mod="start_game"]',
      title: '先加启动游戏',
      body: '这是内置步骤，负责打开游戏并等待进入大厅。',
      prepare: function () { setTab('control'); openSheet('manage'); },
      done: function () { return state.tasks.indexOf('start_game') !== -1; }
    },
    {
      target: '.mod-card[data-mod="close_game"]',
      title: '再加关闭游戏',
      body: '它放在最后，一轮跑完自动退出游戏。',
      prepare: function () { setTab('control'); openSheet('manage'); },
      done: function () { return state.tasks.indexOf('close_game') !== -1; }
    },
    {
      target: '#manage-done',
      title: '确认列表',
      body: '顺序就是执行顺序。确认后关掉这个弹窗。',
      prepare: function () { setTab('control'); openSheet('manage'); },
      done: function () { return !sheetOpen('manage'); }
    },
    {
      target: '.tab[data-tab="settings"]',
      title: '去看运行环境',
      body: '模型、驱动和系统权限都在设置里。',
      prepare: function () { closeSheet('manage'); closeSheet('env'); setTab('control'); },
      done: function () { return state.tab === 'settings'; }
    },
    {
      target: '#btn-env',
      title: '打开运行环境',
      body: '需要模型或驱动时在这里处理；暂时不装也不影响启动和关闭游戏。',
      prepare: function () { setTab('settings'); },
      done: function () { return sheetOpen('env'); }
    }
  ];

  function waitFor(fn, ms) {
    return new Promise(function (resolve) {
      var t0 = performance.now();
      (function loop() {
        var ok = false;
        try { ok = !!fn(); } catch (e) { ok = false; }
        if (ok) return resolve(true);
        if (performance.now() - t0 > ms) return resolve(false);
        setTimeout(loop, 60);
      })();
    });
  }

  function currentTarget() {
    var step = STEPS[tourIndex];
    return step ? document.querySelector(step.target) : null;
  }

  // 四块遮罩拼出洞：洞内没有元素，点击直接落到真实目标。
  function layout(el) {
    if (!el) return;
    var r = el.getBoundingClientRect();
    var pad = 6;
    var l = Math.max(0, r.left - pad);
    var t = Math.max(0, r.top - pad);
    var w = r.width + pad * 2;
    var h = r.height + pad * 2;
    var vw = window.innerWidth;
    var vh = window.innerHeight;

    setBox('[data-block="top"]', { left: 0, top: 0, width: vw, height: Math.max(0, t) });
    setBox('[data-block="left"]', { left: 0, top: t, width: Math.max(0, l), height: h });
    setBox('[data-block="right"]', { left: l + w, top: t, width: Math.max(0, vw - l - w), height: h });
    setBox('[data-block="bottom"]', { left: 0, top: t + h, width: vw, height: Math.max(0, vh - t - h) });

    var ring = $('#tour-ring');
    ring.style.left = l + 'px';
    ring.style.top = t + 'px';
    ring.style.width = w + 'px';
    ring.style.height = h + 'px';

    var bubble = $('#tour-bubble');
    var bw = 292;
    var bh = bubble.offsetHeight || 150;
    var spaceBelow = vh - (t + h);
    var above = spaceBelow < bh + 28;
    var bTop = above ? (t - bh - 12) : (t + h + 12);
    var bLeft = Math.min(Math.max(12, l), vw - bw - 12);
    if (bTop < 12) bTop = 12;
    bubble.style.left = bLeft + 'px';
    bubble.style.top = bTop + 'px';
    bubble.dataset.arrow = above ? 'down' : 'up';
    bubble.style.setProperty('--arrow-x', Math.max(14, Math.min(bw - 24, r.left + r.width / 2 - bLeft - 5)) + 'px');
  }

  function setBox(sel, box) {
    var el = $(sel);
    if (!el) return;
    el.style.left = box.left + 'px';
    el.style.top = box.top + 'px';
    el.style.width = box.width + 'px';
    el.style.height = box.height + 'px';
  }

  async function showStep(i) {
    if (i >= STEPS.length) return finishTour(true);
    tourIndex = i;
    state.tourIndex = i;
    save();
    var step = STEPS[i];
    var tour = $('#tour');
    tour.hidden = true;             // 先藏起来，避免用旧位置闪一下
    try { await step.prepare(); } catch (e) { console.warn(e); }
    await delay(SNAP ? 30 : 280);   // 等弹窗/切页落定再定位
    var el = currentTarget();
    if (!el) {
      console.warn('[tour] 目标不存在，跳过:', step.target);
      return showStep(i + 1);
    }
    $('#bubble-step').textContent = '第 ' + (i + 1) + ' 步 / 共 ' + STEPS.length + ' 步';
    $('#bubble-title').textContent = step.title;
    $('#bubble-body').textContent = step.body;
    var bubble = $('#tour-bubble');
    bubble.classList.remove('is-nudge');   // 不把上一步的抖动带到这一步
    tour.hidden = false;
    tourActive = true;
    layout(el);
    // 等气泡渲染完再定位一次，高度才是准的
    await delay(20);
    layout(el);
    try { el.focus({ preventScroll: true }); } catch (e) { /* 忽略 */ }
  }

  async function advance(force) {
    if (!force) {
      var step = STEPS[tourIndex];
      var el = currentTarget();
      if (!el) return showStep(tourIndex + 1);
      var ok = await waitFor(step.done, 1600);
      if (!ok) { nudgeBubble(); return; }
    }
    if (tourIndex + 1 >= STEPS.length) return finishTour(true);
    showStep(tourIndex + 1);
  }

  function nudgeBubble() {
    var b = $('#tour-bubble');
    b.classList.remove('is-nudge');
    void b.offsetWidth;
    b.classList.add('is-nudge');
    setTimeout(function () { b.classList.remove('is-nudge'); }, 340);
  }

  function startTour(from) {
    if (state.tourDone) return;
    tourIndex = Math.max(0, from || 0);
    showStep(tourIndex);
  }

  function finishTour(completed) {
    tourActive = false;
    $('#tour').hidden = true;
    state.tourDone = true;
    state.tourIndex = STEPS.length;
    save();
    updateStatus();
    if (completed) toast('引导结束。需要模型或驱动时，在运行环境里处理。');
  }

  function toast(text) {
    var el = document.createElement('div');
    el.className = 'toast';
    el.textContent = text;
    document.body.appendChild(el);
    setTimeout(function () { el.classList.add('is-out'); }, 2200);
    setTimeout(function () { el.remove(); }, 2600);
  }

  // ---------- 事件 ----------
  function bind() {
    $$('.tab').forEach(function (b) {
      b.addEventListener('click', function () { setTab(b.dataset.tab); });
    });
    $('#btn-manage').addEventListener('click', function () { openSheet('manage'); });
    $('#manage-done').addEventListener('click', function () { closeSheet('manage'); });
    $$('[data-close="manage"]').forEach(function (b) {
      b.addEventListener('click', function () { closeSheet('manage'); });
    });
    $$('.mod-card').forEach(function (card) {
      card.addEventListener('click', function () {
        toggleTask(card.dataset.mod);
        nudgeTarget(card);
      });
    });
    $('#btn-env').addEventListener('click', function () { openSheet('env'); });
    $$('[data-close="env"]').forEach(function (b) {
      b.addEventListener('click', function () { closeSheet('env'); });
    });
    $('#btn-install').addEventListener('click', installModel);

    // 遮罩块：严格模式，只能点高亮目标
    $$('.tour-block').forEach(function (block) {
      block.addEventListener('click', nudgeBubble);
    });

    $('#tour-skip-step').addEventListener('click', function () { advance(true); });
    $('#tour-exit').addEventListener('click', function () { finishTour(false); });

    // 目标点击：等结果出现再前进，不是点了就算
    document.addEventListener('click', function (e) {
      if (!tourActive || $('#tour').hidden) return;
      var el = currentTarget();
      if (!el || !el.contains(e.target)) return;
      setTimeout(function () { advance(false); }, 40);
    });

    // 声明门
    $('#gate-accept').addEventListener('click', function () {
      if (this.disabled) return;
      acceptTerms();
    });
    $('#gate-decline').addEventListener('click', function () {
      countdown.active = false;
      cancelAnimationFrame(countdown.raf);
      $('#exit').hidden = false;
    });
    $('#exit-restart').addEventListener('click', function () {
      $('#exit').hidden = true;
      $('#gate').hidden = false;
      countdown.done = false;
      showGate();
    });

    // Esc 退出引导（声明门不响应）
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && tourActive) finishTour(false);
    });

    window.addEventListener('resize', function () {
      if (tourActive) layout(currentTarget());
    });

    // 演示控制
    $('#demo-dock').addEventListener('click', function (e) {
      var btn = e.target.closest('[data-demo]');
      if (!btn) return;
      var a = btn.dataset.demo;
      if (a === 'reset') { localStorage.removeItem(KEY); location.href = location.pathname; }
      else if (a === 'revise') {
        state.terms = { version: '2026-11-B', acceptedAt: null };
        state.tourDone = false; state.tourIndex = 0; save(); location.reload();
      } else if (a === 'skip-count') { skipCountdown(); }
      else if (a === 'next') { if (tourActive) advance(true); }
      else if (a === 'reopen') {
        state.tourDone = false; state.tourIndex = 0; state.tasks = [];
        state.modelReady = false; save();
        location.reload();
      }
    });
  }

  function nudgeTarget(el) {
    el.classList.remove('is-tap');
    void el.offsetWidth;
    el.classList.add('is-tap');
    setTimeout(function () { el.classList.remove('is-tap'); }, 220);
  }

  // ---------- URL：截图/演示跳转 ----------
  function parseQuery() {
    return new URLSearchParams(location.search.replace(/^\?/, '') + '&' + location.hash.replace(/^#/, ''));
  }

  function applyView() {
    var q = parseQuery();
    SNAP = q.has('snap');
    if (SNAP) document.documentElement.classList.add('snap-mode');
    if (q.has('reset')) {
      localStorage.removeItem(KEY);
      state = defaultState();
      save();
      return;
    }
    var view = q.get('view');
    if (view === 'gate') {
      state.terms = { version: TERMS_VERSION, acceptedAt: null };
      state.tourDone = false;
      state.tourIndex = 0;
      save();
    } else if (view === 'tour') {
      state.terms = { version: TERMS_VERSION, acceptedAt: new Date().toISOString() };
      state.tourDone = false;
      var si = Math.max(0, Math.min(STEPS.length - 1, parseInt(q.get('step') || '0', 10) || 0));
      state.tourIndex = si;
      // 让指定步骤的前置页面状态成立，截图才和真实流程一致
      state.tasks = [];
      if (si >= 2) state.tasks = ['start_game'];
      if (si >= 3) state.tasks = ['start_game', 'close_game'];
      state.modelReady = false;
      state.tab = 'control';
      save();
    }
  }

  // ---------- 启动 ----------
  function boot() {
    load();
    applyView();
    bind();
    renderTasks();
    setTab(state.tab);
    if (!termsAccepted()) {
      showGate();
    } else if (!state.tourDone) {
      startTour(state.tourIndex || 0);
    }
  }

  boot();
})();
