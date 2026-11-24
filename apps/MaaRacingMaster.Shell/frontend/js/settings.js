// MaaRM shell 前端 —— 设置页控件、模块「数据/设置」页、数据页看板中心。
// 环境中心与注册表权限体检在 js/env-center.js；配置面板由插件声明、js/config-panel.js 通用渲染。
// 跨文件调用经 window.MaaRM（见文件末尾导出）。
(function () {
  'use strict';

  // 共享物导入（js/rpc.js、js/modal.js、js/preview.js 已先行加载）
  const {
    maarm, $, state, showError, reportError, postWindowAction, escHtml,
    openModal, showGamepadNotice,
    setPreviewPlayState, enterPreviewFullscreen, exitPreviewFullscreen,
  } = window.MaaRM;

  // 调试文件夹（设置页入口）：先给用户数据目录的占用总览，再由面板按钮跳资源管理器。
  // 统计在 sidecar 侧同步完成（本机 3.6 万文件约 0.08s），故不做加载态。
  const USAGE_KEY_RE = /^[a-z][a-z0-9-]*$/; // 目录键要拼进 class 名，只收安全字符

  // 与资源管理器同口径：1024 进制、沿用 KB/MB/GB 字样；小数字数按量级收敛
  function fmtBytes(n) {
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let v = Number(n) || 0;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    const dec = i === 0 ? 0 : (v >= 100 ? 0 : (i >= 3 ? 2 : 1));
    return v.toFixed(dec) + ' ' + units[i];
  }

  async function openDataUsage() {
    let d;
    try {
      d = await maarm.call('get_data_usage');
    } catch (e) {
      showError('读取目录占用失败: ' + e.message);
      return;
    }
    const dirs = Array.isArray(d && d.dirs) ? d.dirs : [];
    const total = Number(d && d.total) || 0;
    // 扇区按累计字节切分：每段接上一段的终点，零字节目录自然没有扇区
    let acc = 0;
    const stops = [];
    dirs.forEach((x) => {
      const bytes = Number(x.bytes) || 0;
      if (bytes <= 0 || total <= 0) return;
      const from = (acc / total) * 360;
      acc += bytes;
      stops.push('var(--maarm-usage-' + x.key + ', var(--maarm-foreground-dim)) '
        + from.toFixed(2) + 'deg ' + (acc / total * 360).toFixed(2) + 'deg');
    });
    const donut = stops.length
      ? 'background: conic-gradient(from -90deg, ' + stops.join(', ') + ');'
      : 'background: var(--maarm-border);';
    const rows = dirs.map((x) => {
      const key = USAGE_KEY_RE.test(String(x.key)) ? x.key : 'unknown';
      return '<li class="space-row">'
        + '<span class="space-dot space-dot--' + key + '"></span>'
        + '<div class="space-main">'
        + '<div class="space-name">' + escHtml(x.key) + '</div>'
        + '<div class="space-desc">' + escHtml(x.label) + '</div>'
        + '</div>'
        + '<span class="space-size">' + fmtBytes(x.bytes) + '</span>'
        + '</li>';
    }).join('');
    return openModal({
      title: '调试数据占用',
      maxWidth: 580,
      bodyHtml: '<p class="space-root">' + escHtml((d && d.root) || '') + '</p>'
        + '<div class="space-body">'
        + '<div class="space-chart">'
        + '<div class="space-donut" style="' + donut + '"></div>'
        + '<div class="space-donut-center"><b>' + fmtBytes(total) + '</b><span>合计占用</span></div>'
        + '</div>'
        + '<ul class="space-list">' + rows + '</ul>'
        + '</div>',
      buttons: [
        {
          text: '打开文件夹',
          primary: true,
          onClick: async (modal) => {
            try {
              await maarm.call('open_user_data_folder', {});
              modal.close();
            } catch (e) {
              showError('打开文件夹失败: ' + e.message);
            }
          },
        },
        { text: '关闭', onClick: (modal) => modal.close() },
      ],
    });
  }

  // ---------- 模块「数据/设置」页（两模块共用同一套模板，id 带模块前缀）----------
  // 数据页的模块差异由看板中心的 block 内容承载；页面骨架不按模块分叉。

  // ---------- 看板中心 ----------
  // 候选集 = 任务列表 ∩ 模块看板声明，按任务列表顺序；当前看板是会话内显式选择，
  // 队列变化只校正被移出的选中项。看板内容统一走 get_module_dashboard 的 block 载荷，
  // 这里只提供通用渲染器与一个今日统计兼容块。

  let _dashCurrentId = '';
  let _dashCandidatesSig = null;
  let _dashPrevIds = [];
  let _dashReqSeq = 0;
  let _dashPayloadSig = '';

  function dashboardCandidates() {
    const byId = new Map();
    state.modules.forEach((m) => { if (m && m.id) byId.set(m.id, m); });
    return state.queue.order
      .map((mid) => byId.get(mid))
      .filter((m) => !!m && m.kind === 'module' && m.has_dashboard === true);
  }

  function resolveDashboardSelection(ids) {
    if (ids.indexOf(_dashCurrentId) !== -1) return;
    if (!ids.length) { _dashCurrentId = ''; return; }
    const prevIndex = _dashPrevIds.indexOf(_dashCurrentId);
    const index = prevIndex >= 0 ? Math.min(prevIndex, ids.length - 1) : 0;
    _dashCurrentId = ids[index];
  }

  function dashboardCenterCard() {
    return `
        <!-- 看板中心：固定槽位；候选集由任务列表与模块声明共同决定 -->
        <div class="card dash-card" id="dash-card">
          <div class="card-head dash-head">
            <h3>看板中心</h3>
            <span class="kind-chip dash-module-chip" id="dash-module-chip" hidden></span>
            <div class="dash-nav">
              <div class="dash-pager" id="dash-pager">
                <button class="dash-nav-btn" id="dash-prev" type="button" title="上一个看板" aria-label="上一个看板">
                  <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M10 3.5 5.5 8l4.5 4.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
                </button>
                <span class="dash-page" id="dash-page" aria-live="polite"></span>
                <button class="dash-nav-btn" id="dash-next" type="button" title="下一个看板" aria-label="下一个看板">
                  <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6 3.5 10.5 8 6 12.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
                </button>
              </div>
            </div>
          </div>
          <div class="dash-body" id="dash-body">${dashLoadingHtml()}</div>
        </div>`;
  }

  // 空态统一视觉：插图框 + 标题 + 一句说明。没有候选（dashEmptyHtml）、模块
  // 空态（empty block）与空载荷共用同一个生成器，文案一律经 escHtml 转义。
  function dashEmptyStateHtml(title, text) {
    return '<div class="dash-empty">'
      + '<div class="empty-art" aria-hidden="true"><svg viewBox="0 0 24 24">'
      + '<rect x="3" y="3" width="8" height="8" rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/>'
      + '<rect x="13" y="3" width="8" height="8" rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/>'
      + '<rect x="3" y="13" width="8" height="8" rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/>'
      + '<rect x="13" y="13" width="8" height="8" rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/>'
      + '</svg></div>'
      + (title ? '<b>' + escHtml(title) + '</b>' : '')
      + (text ? '<p>' + escHtml(text) + '</p>' : '')
      + '</div>';
  }

  function dashEmptyHtml() {
    return dashEmptyStateHtml('还没有可显示的模块看板',
      '任务列表里还没有声明看板的模块。模块接入看板后，这里会显示它的数据；内置步骤不参与看板中心。');
  }

  function dashLoadingHtml() {
    return '<div class="dash-empty"><b>读取看板中…</b></div>';
  }

  function dashErrorHtml(err) {
    return '<div class="dash-empty"><b>看板读取失败</b><p>'
      + escHtml((err && err.message) || err) + '</p></div>';
  }

  // 统计窄条：一行四列；label/value/sub 是共享结构，模块不参与排版。
  function dashStatCell(label, valueHtml, extraHtml, valueCls) {
    return '<div class="dash-stat">'
      + '<span class="dash-stat-label">' + escHtml(label) + '</span>'
      + '<b class="dash-stat-value' + (valueCls || '') + '">' + valueHtml + '</b>'
      + (extraHtml || '')
      + '</div>';
  }

  function dashStatHtml(item) {
    const cls = item.tone === 'positive' ? ' pos'
      : item.tone === 'negative' ? ' neg' : '';
    const sub = item.sub ? '<span class="dash-stat-sub">' + escHtml(item.sub) + '</span>' : '';
    const dots = item.dots === true
      ? '<div class="egg-dots" aria-hidden="true"><i></i><i></i><i></i></div>' : '';
    return dashStatCell(item.label, escHtml(item.value), sub + dots, cls);
  }

  const ROW_PART_TONES = {
    neutral: '',
    positive: ' row-part--positive',
    negative: ' row-part--negative',
    muted: ' row-part--muted',
    warn: ' row-part--warn',
    info: ' row-part--info'
  };

  function dashBadgeHtml(badge) {
    if (!badge) return '';
    const cls = badge.tone === 'positive' ? ' board-res--win'
      : badge.tone === 'negative' ? ' board-res--fail'
      : badge.tone === 'warn' ? ' board-res--warn' : '';
    return '<span class="board-res' + cls + '">' + escHtml(badge.text) + '</span>';
  }

  function dashRowHtml(row) {
    const body = Array.isArray(row.parts) && row.parts.length
      ? row.parts.map((part) => '<span class="row-part'
        + (ROW_PART_TONES[part.tone] || '') + '">' + escHtml(part.text) + '</span>').join('')
      : escHtml(row.text);
    return '<div class="dash-row">'
      + '<span class="dash-row-time">' + escHtml(row.leading || '') + '</span>'
      + '<span class="dash-row-text">' + body + '</span>'
      + '<span class="dash-row-badge">' + dashBadgeHtml(row.badge) + '</span>'
      + '<span class="dash-row-value">' + (row.trailing ? escHtml(row.trailing) : '') + '</span>'
      + '</div>';
  }

  function dashBlockHtml(block) {
    switch (block.type) {
      case 'stats':
        return '<div class="dash-stats">' + block.items.map(dashStatHtml).join('') + '</div>';
      case 'list': {
        const rows = Array.isArray(block.rows) ? block.rows : [];
        const pageSize = Number(block.page_size) > 0 ? Number(block.page_size) : 0;
        const pageAttr = pageSize ? ' data-page-size="' + pageSize + '"' : '';
        let lastGroup = null;
        const items = rows.map((row) => {
          const showGroup = row.group && row.group !== lastGroup;
          lastGroup = row.group || lastGroup;
          return '<div class="board-item">'
            + (showGroup ? '<div class="board-date">' + escHtml(row.group) + '</div>' : '')
            + dashRowHtml(row)
            + '</div>';
        }).join('');
        return '<div class="board-list"' + pageAttr + '>' + items + '</div>';
      }
      case 'text':
        return '<p class="dash-note">' + escHtml(block.text) + '</p>';
      case 'empty':
        return dashEmptyStateHtml(block.title, block.text);
      case 'path':
        return '<div class="path-chip" title="' + escHtml(block.text) + '">'
          + escHtml(block.text) + '</div>';
      case 'action':
        return '<button class="dash-action" type="button" data-dash-action="'
          + escHtml(block.action) + '">' + escHtml(block.label) + '</button>';
      default:
        return '';
    }
  }

  // 列表分页：每页 page_size 条，滚到底自动续下一页；滚动条由 CSS 隐藏。
  // 分页单位是 .board-item（含日期组头），组头随首行一起显隐。
  function bindDashboardLists(root) {
    if (!root) return;
    const lists = root.querySelectorAll('.board-list[data-page-size]');
    if (typeof IntersectionObserver !== 'function') {
      lists.forEach((list) => {
        list.querySelectorAll('.board-item[hidden]').forEach((item) => {
          item.hidden = false;
        });
      });
      return;
    }
    lists.forEach((list) => {
      const size = Math.max(1, Number(list.dataset.pageSize) || 5);
      const items = Array.from(list.querySelectorAll('.board-item'));
      if (items.length <= size) return;
      items.forEach((item, index) => { item.hidden = index >= size; });
      let shown = size;
      const sentinel = document.createElement('div');
      sentinel.className = 'board-sentinel';
      list.appendChild(sentinel);
      let observer = null;
      const reveal = () => {
        if (shown >= items.length) return;
        items.slice(shown, shown + size).forEach((item) => { item.hidden = false; });
        shown = Math.min(items.length, shown + size);
        if (shown >= items.length) {
          if (observer) observer.disconnect();
          sentinel.remove();
          return;
        }
        requestAnimationFrame(() => {
          if (list.scrollHeight <= list.clientHeight + 8) reveal();
        });
      };
      observer = new IntersectionObserver((entries) => {
        if (entries.some((entry) => entry.isIntersecting)) reveal();
      }, { root: list, rootMargin: '0px 0px 32px 0px' });
      observer.observe(sentinel);
    });
  }

  // 主体：顶部一行日期说明（新契约取 subtitle，旧兼容块取其 bucket），
  // 下面是模块下发的 block。模块身份只在卡头胶囊里，不再重复一遍标题。
  function dashboardPayloadHtml(payload) {
    const blocks = (payload && Array.isArray(payload.blocks)) ? payload.blocks : [];
    const subtitle = (payload && typeof payload.subtitle === 'string') ? payload.subtitle : '';
    const badge = payload && payload.badge && typeof payload.badge.text === 'string'
      ? payload.badge : null;
    const badgeCls = badge
      ? (badge.tone === 'warn' ? ' kind-chip--warn'
        : badge.tone === 'positive' ? ' kind-chip--positive'
        : badge.tone === 'negative' ? ' kind-chip--negative' : '')
      : '';
    const caption = subtitle || '';
    const captionRow = (caption || badge)
      ? '<div class="dash-caption-row">'
        + '<span class="dash-caption">' + escHtml(caption) + '</span>'
        + (badge ? '<span class="kind-chip' + badgeCls + '">' + escHtml(badge.text) + '</span>' : '')
        + '</div>'
      : '';
    return '<div class="dash-panel">'
      + captionRow
      + (blocks.length
        ? blocks.map(dashBlockHtml).join('')
        : dashEmptyStateHtml('看板暂无内容'))
      + '</div>';
  }

  function renderDashboardChrome(cands) {
    const pager = $('dash-pager');
    const prev = $('dash-prev');
    const next = $('dash-next');
    const ind = $('dash-page');
    const chip = $('dash-module-chip');
    if (!pager || !prev || !next || !ind) return;
    const list = cands || dashboardCandidates();
    const count = list.length;
    const index = list.findIndex((m) => m.id === _dashCurrentId);
    const runId = (state.queueRun && state.queueRun.current_module) || '';
    const runIndex = runId ? list.findIndex((m) => m.id === runId) : -1;
    const currentIsRunning = runIndex >= 0 && runIndex === index;
    if (chip) {
      const mod = index >= 0 ? list[index] : null;
      chip.textContent = mod ? mod.name : '';
      chip.hidden = !mod;
      // 单看板没有翻页器，运行态改由胶囊上的绿点提示。
      chip.classList.toggle('dash-module-chip--run', currentIsRunning && count <= 1);
      chip.title = currentIsRunning && count <= 1 ? '当前看板正在运行' : '';
    }
    // 只有一个看板时没有可翻的页：翻页器整体隐藏，卡头高度不变。
    pager.hidden = count <= 1;
    ind.textContent = count ? (index + 1) + ' / ' + count : '';
    prev.disabled = index <= 0;
    next.disabled = index < 0 || index >= count - 1;
    const dir = runIndex < 0 || currentIsRunning ? 0 : (runIndex > index ? 1 : -1);
    ind.classList.toggle('is-running', currentIsRunning);
    ind.title = currentIsRunning ? '当前看板正在运行' : '';
    next.classList.toggle('dash-nav-btn--run', dir === 1);
    prev.classList.toggle('dash-nav-btn--run', dir === -1);
    const runName = runIndex >= 0 ? list[runIndex].name : '';
    next.title = dir === 1 ? '运行中的模块在右侧：' + runName : '下一个看板';
    prev.title = dir === -1 ? '运行中的模块在左侧：' + runName : '上一个看板';
    next.setAttribute('aria-label', next.title);
    prev.setAttribute('aria-label', prev.title);
  }

  // 只挂载并轮询当前看板；页面不可见时不拉数，切走即停止。
  async function loadDashboard() {
    const page = $('page-data');
    const body = $('dash-body');
    if (!page || !body) return;
    const mod = dashboardCandidates().find((m) => m.id === _dashCurrentId);
    if (!mod) {
      _dashReqSeq += 1;
      _dashPayloadSig = '';
      delete body.dataset.loadedFor;
      body.innerHTML = dashEmptyHtml();
      return;
    }
    if (page.classList.contains('hidden')) return; // 不可见不拉数；切到数据页立即补拉
    const seq = ++_dashReqSeq;
    if (body.dataset.loadedFor !== mod.id) {
      _dashPayloadSig = '';
      body.innerHTML = dashLoadingHtml();
    }
    try {
      const payload = await maarm.call('get_module_dashboard', { module_id: mod.id });
      if (seq !== _dashReqSeq) return; // 已切到别的看板：丢弃过期响应
      const sig = JSON.stringify(payload);
      if (sig === _dashPayloadSig && body.dataset.loadedFor === mod.id) return;
      _dashPayloadSig = sig;
      body.innerHTML = dashboardPayloadHtml(payload);
      bindDashboardLists(body);
      body.dataset.loadedFor = mod.id;
    } catch (e) {
      if (seq !== _dashReqSeq) return;
      _dashPayloadSig = ''; // 失败后下一次成功必须重渲染，不能因旧签名相同而停在错误态
      body.innerHTML = dashErrorHtml(e);
      body.dataset.loadedFor = mod.id;
    }
  }

  async function pollDashboard() {
    try {
      await loadDashboard();
    } catch (e) { /* 看板轮询失败静默（库未创建 / 尚未跑过），下一次再试 */ }
  }

  function stepDashboard(delta) {
    const list = dashboardCandidates();
    const index = list.findIndex((m) => m.id === _dashCurrentId);
    const target = index + delta;
    if (index < 0 || target < 0 || target >= list.length) return;
    _dashCurrentId = list[target].id;
    renderDashboardChrome(list);
    loadDashboard();
  }

  // 任务列表变化后重算候选集；选中项仍在就保留，被移出则夹到原位置的相邻项。
  function syncDashboardCenter() {
    if (!$('dash-card')) return;
    const cands = dashboardCandidates();
    const ids = cands.map((m) => m.id);
    resolveDashboardSelection(ids);
    const sig = ids.join('|');
    const changed = sig !== _dashCandidatesSig;
    _dashPrevIds = ids;
    _dashCandidatesSig = sig;
    renderDashboardChrome(cands);
    if (changed) loadDashboard();
  }

  // 运行状态变化只更新指示位，不重拉看板（轮询节奏仍由 pollDashboard 控制）。
  function refreshDashboardRunState() {
    if ($('dash-card')) renderDashboardChrome();
  }

  async function onDashboardAction(action) {
    if (!action) return;
    try {
      await maarm.call(action, {});
    } catch (e) {
      reportError('toast', '操作失败: ' + (e.message || e));
    }
  }

  // 数据页每次重建都会生成新的看板中心 DOM：事件按新节点重绑，无重复监听。
  function bindDashboardCenter() {
    const card = $('dash-card');
    if (!card) return;
    const prev = $('dash-prev');
    const next = $('dash-next');
    if (prev) prev.addEventListener('click', () => stepDashboard(-1));
    if (next) next.addEventListener('click', () => stepDashboard(1));
    card.addEventListener('click', (ev) => {
      const btn = ev.target.closest('[data-dash-action]');
      if (btn) onDashboardAction(btn.dataset.dashAction);
    });
    _dashCandidatesSig = null; // 新 DOM：按当前队列强制重算一次
    syncDashboardCenter();
  }

  // 今日统计兼容块的数字口径（旧看板沿用同一套格式化）
  function fmtNum(n) {
    return (n === null || n === undefined || n === '' || isNaN(n)) ? '--' : Number(n).toLocaleString();
  }
  // 性能卡（通用）：所有模块共用一套模板。
  // 资源面（CPU / GPU）由 core 采样器供数；模块行由后端按声明下发
  // （perf.rows），前端不认识任何模块——新增模块只声明行，这里零改动。
  //
  // 形态：一块磁贴就是一张图。右上角是器件大字与型号（弱装饰），左上角是器件图标，
  // 左下角是读数，曲线与渐变面积铺满整格。数字带白色晕影，曲线穿过时仍可读。
  function perfKpiTile(mid, kind, label, icon) {
    const grad = (tone, from) => `
                    <linearGradient id="${mid}-perf-${kind}-${tone}-grad" gradientUnits="userSpaceOnUse"
                                    x1="0" y1="0" x2="0" y2="26">
                      <stop offset="0%" stop-color="${from}" stop-opacity="${tone === 'total' ? '0.20' : '0.24'}"></stop>
                      <stop offset="55%" stop-color="${from}" stop-opacity="${tone === 'total' ? '0.06' : '0.07'}"></stop>
                      <stop offset="100%" stop-color="${from}" stop-opacity="0"></stop>
                    </linearGradient>`;
    return `
            <div class="perf-kpi">
              ${MaaRMIcons.svg(icon, {class: 'perf-kpi-icon'})}
              <div class="perf-kpi-mark">
                <span class="perf-kpi-word" aria-hidden="true">${label}</span>
                <span class="perf-kpi-sub" id="${mid}-perf-${kind}-sub"></span>
              </div>
              <svg class="perf-kpi-spark" id="${mid}-perf-${kind}-spark" viewBox="0 0 100 26"
                   preserveAspectRatio="none" aria-hidden="true">
                <defs>${grad('total', '#8B93A7')}${grad('prog', '#E5484D')}
                </defs>
                <path id="${mid}-perf-${kind}-total-area" fill="url(#${mid}-perf-${kind}-total-grad)" d=""></path>
                <path id="${mid}-perf-${kind}-prog-area" fill="url(#${mid}-perf-${kind}-prog-grad)" d=""></path>
                <path class="spark-line spark-line--total" id="${mid}-perf-${kind}-total-line" d=""></path>
                <path class="spark-line spark-line--prog" id="${mid}-perf-${kind}-prog-line" d=""></path>
              </svg>
              <div class="perf-kpi-nums">
                <span class="num num--total"><b id="${mid}-perf-${kind}">--</b><em>%</em></span>
                <span class="num num--prog"><b id="${mid}-perf-${kind}-prog">--</b><em>%</em></span>
              </div>
            </div>`;
  }

  function perfCard(mid) {
    return `
        <div class="card card-flex perf-card">
          <div class="card-head">
            <h3>性能监控</h3>
            <span class="perf-health" id="${mid}-perf-health"></span>
          </div>
          <div class="card-body">
            <div class="perf-kpis">${perfKpiTile(mid, 'cpu', 'CPU', 'cpu')}${perfKpiTile(mid, 'gpu', 'GPU', 'gpu')}
            </div>
            <div class="perf-rows" id="${mid}-perf-rows"></div>
            <button class="perf-threads" id="${mid}-perf-threads" type="button"
                    aria-expanded="true">
              <svg class="perf-caret" viewBox="0 0 10 10" aria-hidden="true">
                <path d="M3.5 1.5 L7 5 L3.5 8.5" fill="none" stroke="currentColor"
                      stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
              <span>线程明细</span>
              <span class="perf-threads-meta" id="${mid}-perf-threads-meta"></span>
            </button>
            <div class="perf-threads-body" id="${mid}-perf-threads-body">
              <div id="${mid}-perf-thread-rows"></div>
            </div>
          </div>
        </div>`;
  }

  // 数据页模板：左列性能监控；右列看板中心 + 实时预览。
  // 看板中心对所有模块同一套骨架，模块差异只在后端下发的 block 内容里。
  function defaultDataCards(mid) {
    return `
      <div class="page-inner">
      <div class="col-left">
        <!-- 性能监控 -->
        ${perfCard(mid)}
      </div>

      <div class="col-right">
        ${dashboardCenterCard()}

        <!-- 实时预览 -->
        <div class="card card-flex preview-card" id="${mid}-preview-card">
          <!-- 悬浮窗接管期间的占位符（三态互斥，见 previewMode） -->
          <div class="preview-away" id="${mid}-preview-away">
            <span>PEEP 离家出走啦~</span>
          </div>
          <div class="card-head">
            <h3>实时预览</h3>
            <div class="log-head-actions">
              <button class="icon-btn" id="${mid}-btn-preview-float" type="button" title="悬浮窗显示（脱离主界面）">
                ${MaaRMIcons.svg('picture-in-picture-2')}
              </button>
              <button class="icon-btn" id="${mid}-btn-preview-max" type="button" title="全屏">
                <morph-icon reduced-motion="user"></morph-icon>
              </button>
              <button class="icon-btn" id="${mid}-btn-preview-toggle" type="button" title="开始预览">
                ${MaaRMIcons.svg('media-play', {class: 'icon-play'})}
                ${MaaRMIcons.svg('media-pause', {class: 'icon-pause'})}
              </button>
            </div>
          </div>
          <div class="preview-wrap">
            <div class="preview-canvas">
              <img class="preview-img" id="${mid}-preview-img" alt="PEEP 实时预览">
              <div class="preview-empty" id="${mid}-preview-empty">
                ${MaaRMIcons.svg('camera')}
                <span data-peep-empty-text>PEEP 开启后显示实时画面</span>
              </div>
            </div>
          </div>
        </div>
      </div>
      </div>`;
  }

  // 设置页默认模板
  function defaultSettingsCards(mid) {
    return `
      <div class="page-inner">
      <div class="col-left">
        <!-- 调试选项 -->
        <div class="card">
          <div class="card-head"><h3>调试选项</h3></div>
          <div class="card-body card-body--col">
            <div class="option-row">
              <button class="maarm-toggle" id="${mid}-toggle-debug" role="switch" aria-checked="false"></button>
              <div class="option-main">
                <div class="option-title">DEBUG 截图与记录</div>
                <p class="option-desc">保存调试图、读数与取证画面，约 300MB/分钟，仅排查时短时开启</p>
              </div>
              <span class="option-note">约占用 300MB/分钟磁盘空间</span>
            </div>
            <div class="option-row">
              <button class="maarm-toggle" id="${mid}-toggle-estop" role="switch" aria-checked="false"></button>
              <div class="option-main">
                <div class="option-title">紧急停止快捷键</div>
                <p class="option-desc">程序接管操作时，同时按下键盘任意 2 个及以上按键立即停止运行逻辑；人工驾驶/录制阶段不检测（否则键盘驾驶会被误触发）</p>
              </div>
              <span class="option-note">仅本次启动有效，重启后需重开</span>
            </div>
            <div class="option-row">
              <button class="maarm-toggle" id="${mid}-toggle-filelog" role="switch" aria-checked="false"></button>
              <div class="option-main">
                <div class="option-title">日志记录</div>
                <p class="option-desc">开启后运行日志、玩法流水与性能记录写入会话目录；关闭时仅保留在界面</p>
              </div>
              <span class="option-note">默认关闭，排查问题时开启</span>
            </div>
            <div class="option-row">
              <button class="maarm-toggle" id="${mid}-toggle-intent" role="switch" aria-checked="false"></button>
              <div class="option-main">
                <div class="option-title">仅显示意图</div>
                <p class="option-desc">打开后程序只把光标/手柄导航到目标位置，<strong>不执行点击</strong>，由你自己按下/确认；关闭时按选中的点击方式自动点击</p>
              </div>
              <span class="option-note">前台鼠标 / 后台手柄共用此开关</span>
            </div>
          </div>
        </div>

        <!-- 运行选项 -->
        <div class="card">
          <div class="card-head"><h3>运行选项</h3></div>
          <div class="card-body card-body--col">
            <div class="option-row">
              <button class="maarm-toggle" id="${mid}-toggle-mutegame" role="switch" aria-checked="false"></button>
              <div class="option-main">
                <div class="option-title">运行时静音游戏</div>
                <p class="option-desc">运行期间把游戏音量静音（方便听别的），停止/结束后自动恢复运行前的音量</p>
              </div>
              <span class="option-note">结束恢复原音量</span>
            </div>
            <div class="option-row">
              <button class="maarm-toggle" id="${mid}-toggle-boostprio" role="switch" aria-checked="false"></button>
              <div class="option-main">
                <div class="option-title">性能优先</div>
                <p class="option-desc">提高本程序进程的 CPU 优先级，感知与控制在系统繁忙时少排队；立即生效，重启后保持</p>
              </div>
              <span class="option-note">GPU/IO 提级系统未开放</span>
            </div>
          </div>
        </div>

        <!-- 点击方式 -->
        <div class="card">
          <div class="card-head"><h3>点击方式</h3></div>
          <div class="card-body">
            <div class="radio-grid">
              <div class="maarm-radio-card maarm-radio-card--selected" data-clickmode="real">
                <div class="radio-inner">
                  <div class="maarm-radio-dot"></div>
                  <div class="option-main">
                    <div class="radio-title"><strong>前台(鼠标)</strong><span class="badge-recommend">推荐</span></div>
                    <p class="radio-desc">光标移到目标后 SendInput 点击；需游戏在前台</p>
                  </div>
                </div>
              </div>
              <div class="maarm-radio-card maarm-radio-card--disabled" data-clickmode="gamepad">
                <div class="radio-inner">
                  <div class="maarm-radio-dot"></div>
                  <div class="option-main">
                    <div class="radio-title"><strong>后台(手柄)</strong><span class="badge-wip">开发中</span></div>
                    <p class="radio-desc">手柄光标导航到目标后按 A 键确认，游戏可留在后台；需 ViGEmBus 虚拟手柄</p>
                  </div>
                </div>
              </div>
            </div>
            <p class="capture-note">切换后立即生效；开启「仅显示意图」时只导航到目标，由你自己按下</p>
          </div>
        </div>

      </div>

      <div class="col-right">
        <!-- 快捷工具：目录占用面板、环境中心、成就管理三个入口 -->
        <div class="card">
          <div class="card-head"><h3>快捷工具</h3></div>
          <div class="card-body">
            <div class="tool-grid">
              <button class="maarm-tool-btn" data-tool="folder">
                ${MaaRMIcons.svg('folder-open', {class: 'maarm-tool-btn-icon'})}
                <span class="maarm-tool-btn-label">调试文件夹</span>
              </button>
              <button class="maarm-tool-btn" data-tool="env">
                ${MaaRMIcons.svg('settings', {class: 'maarm-tool-btn-icon'})}
                <span class="maarm-tool-btn-label">运行环境</span>
              </button>
              <button class="maarm-tool-btn" data-tool="achievements">
                ${MaaRMIcons.svg('trophy', {class: 'maarm-tool-btn-icon'})}
                <span class="maarm-tool-btn-label">成就管理</span>
              </button>
            </div>
          </div>
        </div>
      </div>
      </div>`;
  }

  // 任务列表为空时，「数据/设置」页的占位内容
  const EMPTY_MODULE_HTML =
    '<div class="card"><div class="card-head"><h3>没有面板模块</h3></div>' +
    '<div class="card-body"><p class="module-desc">请在主控页「运行任务」卡片上展开任意模块，或点右上角 ⚙ 添加模块。</p></div></div>';

  // 按模块渲染「数据/设置」页并绑定控件事件（面板模块只决定性能卡与设置页前缀；
  // 看板中心的主体来自任务列表）
  function renderModulePages(moduleId) {
    // 无面板模块：两页只放占位内容、不挂模块控件；panelModuleId 置空后，
    // 轮询里所有带模块前缀的取值都会落空并由各自的判空守卫跳过。
    if (!moduleId) {
      MaaRM.panelModuleId = '';
      const dataHost = $('page-data');
      const settingsHost = $('page-settings');
      if (dataHost) dataHost.innerHTML = EMPTY_MODULE_HTML;
      if (settingsHost) settingsHost.innerHTML = EMPTY_MODULE_HTML;
      return;
    }
    // 防注入白名单：moduleId 会拼入 HTML 模板（如 id="${mid}-..."），只接受后端注册表
    // 给出的模块 id（get_initial_state 的 modules），非法值退回注册表第一个
    // ——此前是硬编码回退 treasure，那会让合法但未定制的模块显示成鉴宝的页面。
    // （兼断 CodeQL js/xss-through-dom 污点：所有拼进模板的 mid 都出自本表）
    if (!state.modules.some((m) => m && m.id === moduleId)) {
      const first = state.modules.find((m) => m && m.id);
      if (!first) return;
      moduleId = first.id;
    }
    MaaRM.panelModuleId = moduleId;
    const dataPage = $('page-data');
    const settingsPage = $('page-settings');
    if (dataPage) dataPage.innerHTML = defaultDataCards(moduleId);
    if (settingsPage) settingsPage.innerHTML = defaultSettingsCards(moduleId);
    bindModulePages(moduleId);
    refreshDebugState();
  }

  // ---------- switch aria 通用接线（免维护：约定优于配置） ----------
  // .option-row 结构里的 .maarm-toggle[role=switch] 自动关联同排 .option-title（名称）
  // 与 .option-desc（描述），引用 id 从开关自身 id 派生（<开关id>-label / -desc）。
  // 新开关按既有结构写即自动获得读屏播报，无需逐实例手写 aria 属性；
  // hasAttribute 守卫幂等，可对同一子树重复调用。
  function wireSwitchAria(root) {
    (root || document).querySelectorAll('.option-row').forEach((row) => {
      const sw = row.querySelector('.maarm-toggle[role="switch"]');
      if (!sw || sw.hasAttribute('aria-labelledby') || !sw.id) return; // 无 id 无法建立引用，跳过
      const title = row.querySelector('.option-title');
      if (!title) return;
      // 已显式命名的标题/说明保留原 id：文案会被程序改写（如录制开关随模式换说法），
      // 覆盖 id 会让那些更新函数找不到元素。
      if (!title.id) title.id = sw.id + '-label';
      sw.setAttribute('aria-labelledby', title.id);
      const desc = row.querySelector('.option-desc');
      if (desc) {
        if (!desc.id) desc.id = sw.id + '-desc';
        sw.setAttribute('aria-describedby', desc.id);
      }
    });
  }
  wireSwitchAria(document); // 静态 HTML 里的开关（如录制演示）脚本加载即接线

  // 绑定当前模块卡片上的控件事件（渲染后调用；旧节点随 innerHTML 替换一并销毁，无重复绑定）
  function bindModulePages(moduleId) {
    wireSwitchAria($('page-settings')); // 模板重渲染后新开关补接线（幂等）
    bindDashboardCenter();              // 数据页看板中心：翻页 / 标准动作 / 首次装载

    const p = (suffix) => $(moduleId + '-' + suffix);

    const tDebug = p('toggle-debug');
    const tEstop = p('toggle-estop');
    if (!tDebug || !tEstop) return;

    tDebug.addEventListener('click', async () => {
      const on = !toggleState(tDebug);
      setToggle(tDebug, on); // 先翻转视觉状态
      try {
        await maarm.call('set_debug_mode', { enabled: on });
      } catch (e) {
        console.error(e);
        showError(e.message);
        setToggle(tDebug, !on); // 回滚
      }
    });

    tEstop.addEventListener('click', async () => {
      const on = !toggleState(tEstop);
      setToggle(tEstop, on); // 先翻转视觉状态
      try {
        await maarm.call('set_emergency_stop', { enabled: on });
      } catch (e) {
        console.error(e);
        showError(e.message);
        setToggle(tEstop, !on); // 回滚
      }
    });

    // 日志记录开关：开启后才把日志写盘（sidecar 落 user_data_dir/logs，持久化 profile）
    const tFilelog = p('toggle-filelog');
    if (tFilelog) {
      tFilelog.addEventListener('click', async () => {
        const on = !toggleState(tFilelog);
        setToggle(tFilelog, on); // 先翻转视觉状态
        try {
          await maarm.call('set_file_logging', { enabled: on });
        } catch (e) {
          console.error(e);
          showError(e.message);
          setToggle(tFilelog, !on); // 回滚
        }
      });
    }

    // 仅显示意图开关：开启后只导航到目标、不确认点击（由用户自己按）
    const tIntent = p('toggle-intent');
    if (tIntent) {
      tIntent.addEventListener('click', async () => {
        const on = !toggleState(tIntent);
        setToggle(tIntent, on); // 先翻转视觉状态
        try {
          await maarm.call('set_intent_mode', { enabled: on });
        } catch (e) {
          console.error(e);
          showError(e.message);
          setToggle(tIntent, !on); // 回滚
        }
      });
    }

    // 运行选项：运行时静音游戏
    const tMuteGame = p('toggle-mutegame');
    if (tMuteGame) {
      tMuteGame.addEventListener('click', async () => {
        const on = !toggleState(tMuteGame);
        setToggle(tMuteGame, on); // 先翻转视觉状态
        try {
          await maarm.call('set_mute_game', { enabled: on });
        } catch (e) {
          console.error(e);
          showError(e.message);
          setToggle(tMuteGame, !on); // 回滚
        }
      });
    }

    const tBoostPrio = p('toggle-boostprio');
    if (tBoostPrio) {
      tBoostPrio.addEventListener('click', async () => {
        const on = !toggleState(tBoostPrio);
        setToggle(tBoostPrio, on); // 先翻转视觉状态
        try {
          await maarm.call('set_boost_priority', { enabled: on });
        } catch (e) {
          console.error(e);
          showError(e.message);
          setToggle(tBoostPrio, !on); // 回滚
        }
      });
    }

    // 实时预览卡：三个图标按钮 —— 悬浮窗 / 全屏 / 开关（三态互斥，见 previewMode）
    const previewToggle = p('btn-preview-toggle');
    const previewMax = p('btn-preview-max');
    const previewFloat = p('btn-preview-float');
    const previewCard = p('preview-card');
    if (previewMax) {
      const morphEl = previewMax.querySelector('morph-icon');
      if (morphEl) morphEl.icon = MaaRMIcons.node('scan'); // mount 后首个 icon 直接绘制不动画
    }

    if (previewToggle) {
      previewToggle.addEventListener('click', async () => {
        const on = !state.peepEnabled;
        setPreviewPlayState(on);      // 先翻转视觉状态
        try {
          await PeepConsumer.setPeep(maarm, on);
          state.peepEnabled = on;
        } catch (e) {
          console.error(e);
          showError(e.message);
          setPreviewPlayState(!on);   // 回滚
        }
      });
    }

    if (previewCard && previewMax) {
      previewMax.addEventListener('click', () => {
        if (previewCard.classList.contains('preview-card--fullscreen')) exitPreviewFullscreen(previewCard);
        else enterPreviewFullscreen(previewCard);
      });
    }

    if (previewFloat) {
      previewFloat.addEventListener('click', () => {
        // 三态互斥：全屏态点悬浮窗先退回普通态，不与悬浮窗形态并存
        if (previewCard && previewCard.classList.contains('preview-card--fullscreen')) {
          exitPreviewFullscreen(previewCard);
        }
        postWindowAction('peep-float');
      });
    }

    // 点击方式：每个 .radio-grid 是一组单选，组内互斥、组间独立
    // （共用 .maarm-radio-card 样式，选中态不能全页互斥，否则点一组会清掉另一组的选中）
    document.querySelectorAll('.radio-grid').forEach((grid) => {
      grid.querySelectorAll('.maarm-radio-card').forEach((card) => {
        card.addEventListener('click', async () => {
          if (card.classList.contains('maarm-radio-card--disabled')) return; // 开发中选项不可切换
          grid.querySelectorAll('.maarm-radio-card').forEach((c) => c.classList.remove('maarm-radio-card--selected'));
          card.classList.add('maarm-radio-card--selected');
          try {
            if (card.dataset.clickmode) {
              if (card.dataset.clickmode === 'gamepad') showGamepadNotice();
              await maarm.call('set_click_mode', { mode: card.dataset.clickmode });
            }
          } catch (e) {
            console.error(e);
            showError(e.message);
          }
        });
      });
    });

    // 快捷工具：folder → 目录占用面板（面板内再跳资源管理器）；env → 环境中心；
    // achievements → 成就管理整页覆盖层
    document.querySelectorAll('.maarm-tool-btn[data-tool]').forEach((btn) => {
      btn.addEventListener('click', () => {
        if (btn.dataset.tool === 'folder') openDataUsage();
        else if (btn.dataset.tool === 'env') window.MaaRM.openOptimizerCenter();
        else if (btn.dataset.tool === 'achievements') MaaRM.achievements.open();
      });
    });
  }

  // 从后端回填当前模块的调试开关状态（切换模块后丢弃过期回填）
  async function refreshDebugState() {
    const mid = MaaRM.panelModuleId;
    try {
      const d = await maarm.call('get_debug_state');
      if (!d || MaaRM.panelModuleId !== mid) return; // 已切模块：丢弃
      // 判空防护：设置页 DOM 尚未渲染（或模板差异）时跳过对应回填，避免 null.classList 崩溃
      const safeToggle = (suffix, on) => { const el = $(mid + '-' + suffix); if (el) setToggle(el, on); };
      safeToggle('toggle-debug', !!d.debug_mode);
      state.peepEnabled = !!d.peep_enabled;
      setPreviewPlayState(!!d.peep_enabled);
      safeToggle('toggle-estop', !!d.emergency_stop_enabled);
      safeToggle('toggle-filelog', !!d.file_logging);
      safeToggle('toggle-mutegame', !!d.mute_game);
      safeToggle('toggle-boostprio', !!d.boost_priority);
      safeToggle('toggle-intent', !!d.intent_mode);
      // 点击方式选中态
      document.querySelectorAll('.maarm-radio-card').forEach((card) => {
        if (card.dataset.clickmode) {
          card.classList.toggle('maarm-radio-card--selected', card.dataset.clickmode === d.click_mode);
        }
      });
    } catch (e) {
      console.error(e);
      showError('读取调试状态失败: ' + e.message);
    }
  }

  function toggleState(el) {
    return el.classList.contains('maarm-toggle--active');
  }
  function setToggle(el, on) {
    el.classList.toggle('maarm-toggle--active', on);
    el.setAttribute('aria-checked', String(on));
  }

  // ---------- 导出到 window.MaaRM ----------
  Object.assign(window.MaaRM, {
    renderModulePages,
    pollDashboard,
    syncDashboardCenter,
    refreshDashboardRunState,
    refreshDebugState,
    openDataUsage,
  });
})();
