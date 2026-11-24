// MaaRM shell 前端 —— 性能卡渲染。
// 从 run.js 拆出：资源磁贴走势、模块行、健康度与线程明细；只吃状态快照，不掺运行控制。
// 共享物（$ / state）与当前面板模块 id 经 window.MaaRM 取用。
(function () {
  'use strict';

  const MaaRM = window.MaaRM;
  const { $, state } = MaaRM;

  // ---------- 性能卡渲染 ----------
  // 前端只呈现：资源面磁贴由 core 采样器供数，模块行按后端下发的声明行渲染。
  // 不认识任何模块——新增模块只声明行，这里零改动。
  const PERF_LEVEL_RANK = { ok: 0, idle: 1, warn: 2, error: 3 };
  const PERF_HEALTH_TEXT = { ok: '运行正常', warn: '偏高', error: '异常', idle: '待运行' };
  let _perfSampleId = null;

  function setPerfText(id, text) {
    const el = $(id);
    if (el) el.textContent = text;   // textContent：后端文案不进 HTML 通道
  }

  function pushPerfHist(key, value) {
    const h = state.perfHist[key];
    if (!h || !Number.isFinite(value)) return;
    h.push(value);
    if (h.length > state.PERF_HIST_MAX) h.shift();
  }

  // 走势：固定 0–100 标尺（两格之间可以直接比大小），窗口固定 120 点、新点贴右缘进入。
  // 平滑用单调三次 Hermite（Fritsch–Carlson）：曲线平滑但不过冲，
  // 负载不会被插值画成负数或凭空冒尖。
  const PERF_VB_W = 100;
  const PERF_VB_BASE = 26;
  const PERF_VB_PLOT = 25;   // 曲线基线：离底边 1 个单位（约 4px），贴边会被裁掉

  function perfPath(arr) {
    const n = arr.length;
    if (n < 2) return { line: '', area: '', topY: PERF_VB_PLOT };
    const dx = PERF_VB_W / (state.PERF_HIST_MAX - 1);
    const xs = [];
    const ys = [];
    for (let i = 0; i < n; i++) {
      xs.push(PERF_VB_W - (n - 1 - i) * dx);
      ys.push(PERF_VB_PLOT
        - (Math.min(100, Math.max(0, arr[i])) / 100) * (PERF_VB_PLOT - 2));
    }

    const d = [];
    for (let i = 0; i < n - 1; i++) d.push((ys[i + 1] - ys[i]) / dx);
    const m = new Array(n);
    m[0] = d[0];
    m[n - 1] = d[n - 2];
    for (let i = 1; i < n - 1; i++) m[i] = (d[i - 1] + d[i]) / 2;
    for (let i = 0; i < n - 1; i++) {
      if (d[i] === 0) { m[i] = 0; m[i + 1] = 0; continue; }
      const a = m[i] / d[i];
      const b = m[i + 1] / d[i];
      const s2 = a * a + b * b;
      if (s2 > 9) {
        const t = 3 / Math.sqrt(s2);
        m[i] = t * a * d[i];
        m[i + 1] = t * b * d[i];
      }
    }

    let line = 'M' + xs[0].toFixed(2) + ',' + ys[0].toFixed(2);
    for (let i = 0; i < n - 1; i++) {
      line += 'C' + (xs[i] + dx / 3).toFixed(2) + ',' + (ys[i] + (m[i] * dx) / 3).toFixed(2)
        + ' ' + (xs[i + 1] - dx / 3).toFixed(2) + ',' + (ys[i + 1] - (m[i + 1] * dx) / 3).toFixed(2)
        + ' ' + xs[i + 1].toFixed(2) + ',' + ys[i + 1].toFixed(2);
    }
    const area = line
      + 'L' + xs[n - 1].toFixed(2) + ',' + PERF_VB_BASE
      + 'L' + xs[0].toFixed(2) + ',' + PERF_VB_BASE + 'Z';
    return { line, area, topY: Math.min.apply(null, ys) };
  }

  function setPath(id, d) {
    const el = $(id);
    if (el) el.setAttribute('d', d || '');
  }

  function drawPerfSpark(mid, kind, totalArr, progArr) {
    const total = perfPath(totalArr);
    setPath(mid + '-perf-' + kind + '-total-line', total.line);
    setPath(mid + '-perf-' + kind + '-total-area', total.area);
    const tg = $(mid + '-perf-' + kind + '-total-grad');
    if (tg && total.line) {
      tg.setAttribute('y1', String(total.topY));
      tg.setAttribute('y2', String(PERF_VB_BASE));
    }
    // 没有程序负载数据时不留空曲线（GPU 目前取不到按进程占用）
    const prog = progArr.length >= 2 ? perfPath(progArr) : null;
    setPath(mid + '-perf-' + kind + '-prog-line', prog && prog.line);
    setPath(mid + '-perf-' + kind + '-prog-area', prog && prog.area);
    const pg = $(mid + '-perf-' + kind + '-prog-grad');
    if (pg && prog) {
      pg.setAttribute('y1', String(prog.topY));
      pg.setAttribute('y2', String(PERF_VB_BASE));
    }
  }

  // 模块行：结构由声明决定（一次会话内不变），只在 key 序列变化时重建 DOM。
  // 显示串由后端格式化（含单位与百分比），前端不再算——避免两处各算一套。
  function renderPerfRows(mid, rows) {
    const box = $(mid + '-perf-rows');
    if (!box) return;
    const sig = rows.map((r) => r.key).join(',');
    if (box.dataset.sig !== sig) {
      box.textContent = '';
      rows.forEach((row) => {
        const el = document.createElement('div');
        el.className = 'perf-row';
        el.dataset.key = row.key;
        const label = document.createElement('span');
        label.className = 'perf-row-label';
        label.textContent = row.label;
        const val = document.createElement('span');
        val.className = 'perf-row-val';
        el.appendChild(label);
        el.appendChild(val);
        box.appendChild(el);
      });
      box.dataset.sig = sig;
    }
    rows.forEach((row) => {
      const el = box.querySelector('.perf-row[data-key="' + row.key + '"]');
      if (!el) return;
      const val = el.querySelector('.perf-row-val');
      val.textContent = row.display;
      val.className = 'perf-row-val'
        + (row.level === 'warn' ? ' perf-row-val--warn' : '')
        + (row.level === 'error' ? ' perf-row-val--error' : '');
      // 建议只在真出问题时给，正常态不塞文案
      if (row.hint && (row.level === 'warn' || row.level === 'error')) {
        el.title = row.hint;
      } else {
        el.removeAttribute('title');
      }
    });
  }

  // 右上角健康度：取所有行里最差的一档，坏行多于一个时补计数。
  function renderPerfHealth(mid, rows) {
    const el = $(mid + '-perf-health');
    if (!el) return;
    let worst = 'ok';
    let bad = 0;
    rows.forEach((row) => {
      // 缺失原因二分：not_reported 是模块说「本轮该项不适用」（导航阶段还没有
      // 驾驶拍），按行自带等级（idle）聚合；其余三种是监控真的没取到值，才点亮
      // 异常灯。任何缺失都算 error 会把每局开跑前的正常等待读成异常。
      const lv = (row.reason && row.reason !== 'not_reported')
        ? 'error' : (row.level || 'ok');
      if (PERF_LEVEL_RANK[lv] > PERF_LEVEL_RANK[worst]) worst = lv;
      if (lv === 'warn' || lv === 'error') bad += 1;
    });
    const level = rows.length ? worst : 'idle';
    let text = PERF_HEALTH_TEXT[level] || '待运行';
    if (bad > 1) text += ' ' + bad + ' 项';
    el.className = 'perf-health perf-health--' + level;
    el.textContent = '';
    const dot = document.createElement('span');
    dot.className = 'perf-health-dot';
    el.appendChild(dot);
    el.appendChild(document.createTextNode(text));
  }

  // 线程明细：默认展开，折叠只影响显示（采集与落盘始终全量）。
  // 没贴中文别名的线程显示原始线程名——core 不认识业务，不会替它翻译。
  function renderPerfThreads(mid, threads) {
    const rows = (threads && threads.rows) || [];
    const meta = $(mid + '-perf-threads-meta');
    const box = $(mid + '-perf-thread-rows');
    const toggle = $(mid + '-perf-threads');
    if (meta) meta.textContent = rows.length ? rows.length + ' 个' : '';
    if (toggle && toggle.dataset.bound !== '1') {
      toggle.dataset.bound = '1';
      toggle.addEventListener('click', () => {
        const body = $(mid + '-perf-threads-body');
        if (!body) return;
        const open = toggle.getAttribute('aria-expanded') === 'true';
        toggle.setAttribute('aria-expanded', open ? 'false' : 'true');
        body.hidden = open;
      });
    }
    if (!box) return;
    box.textContent = '';
    rows.forEach((row) => {
      const el = document.createElement('div');
      el.className = 'perf-trow';
      const name = document.createElement('span');
      name.className = 'perf-tname';
      name.textContent = row.label || row.name;
      if (row.label) name.title = row.name;      // 原始名放悬停，不占版面
      const bar = document.createElement('span');
      bar.className = 'perf-tbar';
      const fill = document.createElement('i');
      const pct = Number.isFinite(row.cpu_pct)
        ? Math.min(100, Math.max(0, row.cpu_pct)) : null;
      fill.style.width = (pct === null ? 0 : pct) + '%';
      if (pct !== null && pct >= 15) fill.className = 'hot';
      bar.appendChild(fill);
      const cpu = document.createElement('span');
      cpu.className = 'perf-tnum';
      cpu.textContent = pct === null ? '—' : pct.toFixed(0) + '%';
      const hz = document.createElement('span');
      hz.className = 'perf-tnum';
      hz.textContent = Number.isFinite(row.hz) ? row.hz.toFixed(0) + '/秒' : '—';
      el.appendChild(name);
      el.appendChild(bar);
      el.appendChild(cpu);
      el.appendChild(hz);
      box.appendChild(el);
    });
    if (threads && threads.hidden > 0) {
      const more = document.createElement('div');
      more.className = 'perf-tmore';
      more.textContent = '其余 ' + threads.hidden + ' 个未列出';
      box.appendChild(more);
    }
  }

  function pctText(v) {
    return Number.isFinite(v) ? String(Math.round(v)) : '--';
  }

  function renderPerfCard(perf) {
    const mid = MaaRM.panelModuleId;
    const c = (perf && perf.cpu) || {};
    const sys = (perf && perf.sys) || {};
    const gpu = (perf && perf.gpu) || {};
    const rows = (perf && perf.rows) || [];

    const cpuTotal = Number.isFinite(sys.cpu_pct) ? sys.cpu_pct : NaN;
    const cpuProg = (c.available && Number.isFinite(c.load_p50)) ? c.load_p50 * 100 : NaN;
    const gpuOk = gpu.status === 'ok' && Number.isFinite(gpu.util_pct);
    const gpuTotal = gpuOk ? gpu.util_pct : NaN;
    // 按进程的 GPU 占用要等采样器补上（Windows GPU 引擎计数器），没有就留空
    const gpuProg = (gpuOk && Number.isFinite(gpu.prog_pct)) ? gpu.prog_pct : NaN;

    setPerfText(mid + '-perf-cpu', pctText(cpuTotal));
    setPerfText(mid + '-perf-cpu-prog', pctText(cpuProg));
    setPerfText(mid + '-perf-gpu', pctText(gpuTotal));
    setPerfText(mid + '-perf-gpu-prog', pctText(gpuProg));

    setPerfText(mid + '-perf-cpu-sub', Number.isFinite(c.cores) ? c.cores + ' 核' : '');
    let gpuSub = '';
    if (gpuOk) {
      if (gpu.name) {
        gpuSub = gpu.name;
      } else if (Number.isFinite(gpu.mem_total_mb)) {
        gpuSub = (gpu.mem_used_mb / 1024).toFixed(1)
          + ' / ' + Math.round(gpu.mem_total_mb / 1024) + ' GB';
      }
    } else if (gpu.status) {
      gpuSub = '无数据';
    }
    setPerfText(mid + '-perf-gpu-sub', gpuSub);

    // 走势只在采样号变化时推进：4Hz 轮询 / 1Hz 采样，否则同一个值被塞四次，曲线被拉平
    const sid = perf ? perf.sample_id : null;
    if (sid !== null && sid !== undefined && sid !== _perfSampleId) {
      _perfSampleId = sid;
      pushPerfHist('cpuTotal', cpuTotal);
      pushPerfHist('cpuProg', cpuProg);
      pushPerfHist('gpuTotal', gpuTotal);
      pushPerfHist('gpuProg', gpuProg);
    }
    drawPerfSpark(mid, 'cpu', state.perfHist.cpuTotal, state.perfHist.cpuProg);
    drawPerfSpark(mid, 'gpu', state.perfHist.gpuTotal, state.perfHist.gpuProg);

    renderPerfRows(mid, rows);
    renderPerfHealth(mid, rows);
    renderPerfThreads(mid, perf && perf.threads);
  }

  Object.assign(MaaRM, { renderPerfCard });
})();
