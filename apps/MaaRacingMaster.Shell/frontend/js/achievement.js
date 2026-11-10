// 成就管理页（整页覆盖层）：概览 + 筛选 + 按「核心 / 各模块」分组，页头是备份、
// 重置与解锁提示开关。入口在 设置 → 快捷工具 →「成就管理」（settings.js 接线）。
//
// 数据只来自两个 RPC：`get_achievements`（目录 + 进度合并视图，隐藏项后端已遮蔽）与
// `get_initial_state`（只取模块显示名）。文案一律写 textContent —— 标题/描述来自插件
// catalog（不可信输入），不拼 innerHTML。
//
// 冻结态（存档由更高版本写入）必须可见：顶部警示条 + 进度显示「不可用」而不是 0/1，
// 导入与重置置灰——谎报「进度没了」比拒绝更伤人。
(function () {
  'use strict';

  // 共享物：rpc.js / modal.js 已先行加载（见 index.html 脚本序）
  const { maarm, openModal, showError } = window.MaaRM;

  const TIERS = [
    { id: 'normal', label: '普通', color: '#1F2328' },
    { id: 'rare', label: '稀有', color: '#38BDF8' },
    { id: 'precious', label: '珍贵', color: '#E5484D' },
    { id: 'hidden', label: '隐藏', color: '#C9B6F2' },
  ];
  const TIER_LABEL = TIERS.reduce((m, t) => { m[t.id] = t.label; return m; }, {});
  const CORE_ID = 'core';
  const STATUS_HINT = '解锁后永久保留，不随模块移出或重装丢失';

  let items = [];          // get_achievements 合并视图
  let moduleNames = {};    // mod_id → 模块显示名
  let pageState = 'ready'; // ready | frozen | unavailable
  let storeVersion = 0;
  let supportedVersion = 0;
  let notifyOn = true;
  let filter = 'all';

  const $ = (id) => document.getElementById(id);

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function fmtDate(tsNs) {
    if (!Number.isFinite(tsNs)) return '';
    const d = new Date(Number(tsNs) / 1e6);
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }

  function status(text) {
    const node = $('ach-status');
    if (node) node.textContent = text || STATUS_HINT;
  }

  // ---------- 加载 ----------
  async function load() {
    const box = await maarm.call('get_achievements');
    items = (box && box.achievements) || [];
    pageState = (box && box.state) || 'ready';
    notifyOn = !!(box && box.notify);
    storeVersion = (box && box.store_version) || 0;
    supportedVersion = (box && box.supported_version) || 0;
    try {
      const init = await maarm.call('get_initial_state');
      moduleNames = {};
      ((init && init.modules) || []).forEach((m) => {
        if (m && m.id) moduleNames[m.id] = m.name || m.id;
      });
    } catch (e) {
      moduleNames = {};   // 取不到显示名就退回 mod_id，不影响成就本身
    }
  }

  function groupName(modId) {
    if (modId === CORE_ID) return '核心';
    return moduleNames[modId] || modId;
  }

  // ---------- 渲染 ----------
  function card(it) {
    const hiddenLocked = !!it.hidden && !it.unlocked;
    const frozen = pageState !== 'ready';
    const node = el('article', 'maarm-ach-card');
    node.dataset.tier = it.tier || (it.hidden ? 'hidden' : 'normal');
    node.classList.add(it.unlocked ? 'is-unlocked' : 'is-locked');

    const head = el('div', 'maarm-ach-card__head');
    const badge = el('div', 'maarm-ach-card__badge');
    if (window.MaaRMIcons) badge.innerHTML = MaaRMIcons.svg(hiddenLocked ? 'lock' : 'trophy');
    head.appendChild(badge);
    head.appendChild(el('span', 'maarm-ach-card__tier', TIER_LABEL[node.dataset.tier] || '普通'));
    node.appendChild(head);

    const title = el('div', 'maarm-ach-card__title', hiddenLocked ? '？？？' : it.title);
    if (hiddenLocked) title.classList.add('maarm-ach-card__masked');
    else if (it.hidden) title.classList.add('is-rainbow', 'maarm-text-scan', 'maarm-text-scan--rave');
    node.appendChild(title);

    node.appendChild(el('div', 'maarm-ach-card__desc',
      hiddenLocked ? '隐藏成就 · 解锁后揭晓' : it.description));

    const foot = el('div', 'maarm-ach-card__foot');
    if (it.unlocked) {
      foot.appendChild(el('span', null, fmtDate(it.unlocked_ts) + ' 解锁'));
    } else if (frozen) {
      foot.appendChild(el('span', null, '进度暂不可用'));
    } else {
      const target = Number(it.target) || 0;
      const progress = Number(it.progress) || 0;
      if (target > 1 && progress < target) {
        const bar = el('div', 'maarm-ach-card__bar');
        const fill = el('i');
        fill.style.width = Math.max(2, Math.min(100, (progress / target) * 100)) + '%';
        bar.appendChild(fill);
        foot.appendChild(bar);
        foot.appendChild(el('span', null, `${progress} / ${target}`));
      } else {
        foot.appendChild(el('span', null, '尚未解锁'));
      }
    }
    node.appendChild(foot);
    return node;
  }

  function renderSummary() {
    const done = items.filter((it) => it.unlocked).length;
    const total = items.length;
    const pct = total ? Math.round((done / total) * 100) : 0;
    const frozen = pageState !== 'ready';

    $('ach-sum-done').textContent = frozen ? '—' : String(done);
    $('ach-sum-all').textContent = String(total);
    $('ach-sum-pct').textContent = frozen ? '进度暂不可用' : pct + '%';
    $('ach-sum-fill').style.width = frozen ? '0%' : pct + '%';
    $('ach-meta').textContent = frozen ? '' : `已解锁 ${done} / ${total}`;

    const host = $('ach-sum-tiers');
    host.textContent = '';
    TIERS.forEach((t) => {
      const all = items.filter((it) => (it.tier || 'normal') === t.id).length;
      if (!all) return;
      const got = items.filter((it) => (it.tier || 'normal') === t.id && it.unlocked).length;
      const box = el('div', 'maarm-ach-tier-stat');
      box.style.setProperty('--tier-color', t.color);
      box.appendChild(el('span', 'maarm-ach-tier-stat__name', t.label));
      const val = el('span', 'maarm-ach-tier-stat__val', frozen ? '—' : String(got));
      if (!frozen) {
        const em = el('em', null, ` / ${all}`);
        val.appendChild(em);
      }
      box.appendChild(val);
      host.appendChild(box);
    });
  }

  function renderGroups() {
    const host = $('ach-groups');
    host.textContent = '';
    if (!items.length) {
      host.appendChild(el('div', 'maarm-ach-empty',
        pageState === 'unavailable' ? '成就功能暂不可用（引擎未初始化）' : '还没有任何成就'));
      return;
    }

    // 分组：core 自称「核心」，其余按 mod_id 归组；空组不渲染
    const order = [];
    const groups = new Map();
    items.forEach((it) => {
      const modId = it.mod_id || (it.global_id || '').split('.')[0] || 'unknown';
      if (!groups.has(modId)) {
        groups.set(modId, []);
        order.push(modId);
      }
      groups.get(modId).push(it);
    });

    order.forEach((modId) => {
      const all = groups.get(modId);
      const shown = all.filter((it) => filter === 'all'
        ? true : filter === 'unlocked' ? it.unlocked : !it.unlocked);
      if (!shown.length) return;

      const section = el('section', 'maarm-ach-group');
      const head = el('div', 'maarm-ach-group__head');
      head.appendChild(el('span', 'maarm-ach-group__title', groupName(modId)));
      head.appendChild(el('span', 'maarm-ach-group__count', `${all.filter((it) => it.unlocked).length} / ${all.length}`));
      const bar = el('span', 'maarm-ach-group__bar');
      const fill = el('i');
      fill.style.width = Math.round((all.filter((it) => it.unlocked).length / all.length) * 100) + '%';
      bar.appendChild(fill);
      head.appendChild(bar);
      section.appendChild(head);

      const grid = el('div', 'maarm-ach-grid');
      shown.forEach((it) => grid.appendChild(card(it)));
      section.appendChild(grid);
      host.appendChild(section);
    });

    if (!host.childNodes.length) {
      host.appendChild(el('div', 'maarm-ach-empty', '当前筛选下没有成就'));
    }
  }

  function renderChrome() {
    const sheet = $('ach-sheet');
    sheet.classList.toggle('is-frozen', pageState === 'frozen');

    const banner = $('ach-banner');
    if (pageState === 'frozen') {
      banner.hidden = false;
      banner.textContent = `成就存档由更高版本（v${storeVersion}）写入，本程序支持到 v${supportedVersion}：`
        + '本次不读写成就进度。升级程序后会自动恢复；在此之前可以先导出备份。';
    } else if (pageState === 'unavailable') {
      banner.hidden = false;
      banner.textContent = '成就功能暂不可用（引擎未初始化）：本次运行的成就不会被记录，其他功能不受影响。';
    } else {
      banner.hidden = true;
      banner.textContent = '';
    }

    const ready = pageState === 'ready';
    $('ach-import').disabled = !ready;
    $('ach-reset').disabled = !ready;

    const sw = $('ach-notify');
    sw.classList.toggle('is-on', notifyOn);
    sw.setAttribute('aria-pressed', notifyOn ? 'true' : 'false');
  }

  function render() {
    renderChrome();
    renderSummary();
    renderGroups();
  }

  // ---------- 动作 ----------
  async function refresh() {
    try {
      await load();
      render();
    } catch (e) {
      pageState = 'unavailable';
      render();
      status('读取成就失败：' + (e && e.message ? e.message : e));
    }
  }

  async function doExport() {
    try {
      const res = await maarm.call('export_achievements', {});
      if (res && res.cancelled) { status('已取消导出'); return; }
      status(`已导出 ${res.count} 条到 ${res.path}`);
    } catch (e) {
      status('导出失败');
      showError('导出失败：' + (e && e.message ? e.message : e));
    }
  }

  function confirmThen(title, bodyText, okText, run) {
    const modal = openModal({
      title,
      buttons: [
        { text: '取消' },
        { text: okText, primary: true, onClick: (m) => { m.close(); run(); } },
      ],
    });
    // 确认正文用纯文本写入：不把任何文案拼进 bodyHtml
    const body = modal.card.querySelector('.maarm-modal-body');
    if (body) body.textContent = bodyText;
  }

  function doImport() {
    confirmThen('导入成就备份', '导入会用备份覆盖当前进度（现档会自动备份一份 pre-import）。要继续吗？',
      '导入', async () => {
        try {
          const res = await maarm.call('import_achievements', {});
          if (res && res.cancelled) { status('已取消导入'); return; }
          await refresh();
          status(`已导入 ${res.count} 条`);
        } catch (e) {
          status('导入失败');
          showError('导入失败：' + (e && e.message ? e.message : e));
        }
      });
  }

  function doReset() {
    confirmThen('重置成就进度', '这会清空全部成就进度与去重记录，且不会自动备份。确定要重置吗？',
      '全部重置', async () => {
        try {
          const res = await maarm.call('reset_achievements', { scope: 'all' });
          await refresh();
          status(`已重置${res && res.cleared ? `（${res.cleared} 条）` : ''}`);
        } catch (e) {
          status('重置失败');
          showError('重置失败：' + (e && e.message ? e.message : e));
        }
      });
  }

  async function toggleNotify() {
    try {
      const res = await maarm.call('set_achievement_notify', { enabled: !notifyOn });
      notifyOn = !!(res && res.achievement_notify);
      renderChrome();
    } catch (e) {
      showError('切换解锁提示失败：' + (e && e.message ? e.message : e));
    }
  }

  // ---------- 开关 ----------
  function open() {
    const sheet = $('ach-sheet');
    if (!sheet) return;
    sheet.hidden = false;
    sheet.setAttribute('aria-hidden', 'false');
    requestAnimationFrame(() => sheet.classList.add('is-open'));
    status('');
    refresh();
  }

  function close() {
    const sheet = $('ach-sheet');
    if (!sheet || sheet.hidden) return;
    sheet.classList.remove('is-open');
    sheet.setAttribute('aria-hidden', 'true');
    setTimeout(() => {
      if (!sheet.classList.contains('is-open')) sheet.hidden = true;
    }, 240);
  }

  document.addEventListener('click', (e) => {
    const f = e.target.closest('.maarm-ach-filter');
    if (!f) return;
    document.querySelectorAll('.maarm-ach-filter').forEach((b) => b.classList.remove('is-active'));
    f.classList.add('is-active');
    filter = f.dataset.filter;
    renderGroups();
  });

  const bind = (id, fn) => {
    const node = $(id);
    if (node) node.addEventListener('click', fn);
  };
  bind('ach-close', close);
  bind('ach-export', doExport);
  bind('ach-import', doImport);
  bind('ach-reset', doReset);
  bind('ach-notify', toggleNotify);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') close();
  });

  window.MaaRM.achievements = { open, close, refresh };
})();
