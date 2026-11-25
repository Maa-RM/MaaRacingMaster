// MaaRM shell 前端 —— 数据页看板中心。
// 从 settings.js 拆出：候选集、翻页、block 渲染、拉取与轮询；数据页骨架只挂载空槽位。
// 共享物（maarm / $ / state / reportError / escHtml）经 window.MaaRM 取用。
(function () {
  'use strict';

  const { maarm, $, state, reportError, escHtml } = window.MaaRM;

  // ---------- 看板中心 ----------
  // 候选集 = 任务列表 ∩ 模块看板声明，按任务列表顺序；当前看板是会话内显式选择，
  // 队列变化只校正被移出的选中项。看板内容统一走 get_module_dashboard 的 block 载荷，
  // 这里只提供通用渲染器与一个今日统计兼容块。

  let _dashCurrentId = '';
  let _dashCandidatesSig = null;
  let _dashPrevIds = [];
  let _dashReqSeq = 0;
  let _dashPayloadSig = '';
  // 同一看板的在途请求：3 秒轮询直接复用它，不再每轮发新请求把慢回包作废。
  // 只有切换看板（或候选清空、数据页节点被重建）才递增序号，让旧响应失效。
  let _dashInflight = null; // { id, seq, body, promise }

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
  // 返回 true=已画出、false=拉取失败、null=不适用（无候选 / 页面不可见 / 节点不在）。
  async function loadDashboard() {
    const page = $('page-data');
    const body = $('dash-body');
    if (!page || !body) return null;
    const mod = dashboardCandidates().find((m) => m.id === _dashCurrentId);
    if (!mod) {
      _dashReqSeq += 1;      // 作废在途请求
      _dashInflight = null;
      _dashPayloadSig = '';
      delete body.dataset.loadedFor;
      body.innerHTML = dashEmptyHtml();
      return null;
    }
    if (page.classList.contains('hidden')) return null; // 不可见不拉数；切到数据页立即补拉
    // 同一看板、同一个看板节点已有一轮在途：复用它的结果，等它回来再决定是否重渲染。
    // 数据页重建会换掉 #dash-body 节点，旧请求的回包只能写进已移除的节点；
    // 这时必须作废重发，否则新页要白等一轮查询。
    if (_dashInflight && _dashInflight.id === mod.id) {
      if (_dashInflight.body === body) return _dashInflight.promise;
      _dashReqSeq += 1;
      _dashInflight = null;
    }
    if (body.dataset.loadedFor !== mod.id) {
      _dashPayloadSig = '';
      body.innerHTML = dashLoadingHtml();
    }
    const seq = ++_dashReqSeq;
    const promise = (async () => {
      try {
        const payload = await maarm.call('get_module_dashboard', { module_id: mod.id });
        if (seq !== _dashReqSeq) return null; // 已切到别的看板 / 节点已换：丢弃过期响应
        if (body !== $('dash-body')) return null; // 页面已重建：不再写旧节点
        const sig = JSON.stringify(payload);
        if (sig === _dashPayloadSig && body.dataset.loadedFor === mod.id) return true;
        _dashPayloadSig = sig;
        body.innerHTML = dashboardPayloadHtml(payload);
        bindDashboardLists(body);
        body.dataset.loadedFor = mod.id;
        return true;
      } catch (e) {
        if (seq !== _dashReqSeq) return null;
        if (body !== $('dash-body')) return null;
        _dashPayloadSig = ''; // 失败后下一次成功必须重渲染，不能因旧签名相同而停在错误态
        body.innerHTML = dashErrorHtml(e);
        body.dataset.loadedFor = mod.id;
        return false;
      }
    })();
    _dashInflight = { id: mod.id, seq, body, promise };
    try {
      return await promise;
    } finally {
      // 只清自己这一轮：切换看板后 _dashInflight 已被新请求替换
      if (_dashInflight && _dashInflight.seq === seq) _dashInflight = null;
    }
  }

  async function pollDashboard() {
    try {
      return await loadDashboard();
    } catch (e) {
      return null; // 看板轮询失败静默（库未创建 / 尚未跑过），下一次再试
    }
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

  // 看板动作白名单（与 core/dashboard_contract.py 的 ACTIONS 对齐）：方法名保持
  // 字面量，白名单测试才能扫到调用点；新增动作必须同时改这两处。
  const DASHBOARD_ACTIONS = {
    open_user_data_folder: () => maarm.call('open_user_data_folder', {}),
  };

  async function onDashboardAction(action) {
    const run = DASHBOARD_ACTIONS[action];
    if (!run) return;
    try {
      await run();
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


  Object.assign(window.MaaRM, {
    dashboardCenterCard,
    pollDashboard,
    syncDashboardCenter,
    refreshDashboardRunState,
    bindDashboardCenter,
  });
})();
