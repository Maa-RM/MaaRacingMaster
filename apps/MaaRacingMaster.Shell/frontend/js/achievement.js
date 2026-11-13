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
  let frozenReason = null; // version | corrupt | null
  let repairedBackup = null; // 本次启动修复存档时留下的原文件备份名
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

  // 提示文案落在右栏「总览」底部一行。长路径不整条铺进来——右栏是固定宽度，
  // 一串几十字符的绝对路径会把这一段撑成三行；只显示文件名，完整路径挂 title。
  function status(text, fullText) {
    const node = $('ach-status');
    if (!node) return;
    node.textContent = text || STATUS_HINT;
    // 带完整路径的结果行只占一行、超出省略：右栏固定 340，不能让一串路径把卡片顶高
    node.classList.toggle('is-result', !!fullText);
    node.title = fullText || '';
  }

  // ---------- 加载 ----------
  async function load() {
    const box = await maarm.call('get_achievements');
    items = (box && box.achievements) || [];
    pageState = (box && box.state) || 'ready';
    frozenReason = (box && box.frozen_reason) || null;
    repairedBackup = (box && box.repaired_backup) || null;
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
    // 整张卡可点：翻到背面看触发条件（见 style.css 的卡片翻转段）
    node.tabIndex = 0;
    node.setAttribute('role', 'button');
    node.setAttribute('aria-expanded', 'false');
    node.setAttribute('aria-label', hiddenLocked
      ? '查看触发条件（隐藏成就，尚未解锁）'
      : `查看「${it.title}」的触发条件`);

    // 正面：翻面时绕 Y 轴转走的那一面（两面都直接挂在卡上，翻的是卡本身）
    const front = el('div', 'maarm-ach-card__face maarm-ach-card__front');

    const head = el('div', 'maarm-ach-card__head');
    const badge = el('div', 'maarm-ach-card__badge');
    if (window.MaaRMIcons) badge.innerHTML = MaaRMIcons.svg(hiddenLocked ? 'lock' : 'trophy');
    head.appendChild(badge);
    head.appendChild(el('span', 'maarm-ach-card__tier', TIER_LABEL[node.dataset.tier] || '普通'));
    front.appendChild(head);

    const title = el('div', 'maarm-ach-card__title', hiddenLocked ? '？？？' : it.title);
    if (hiddenLocked) title.classList.add('maarm-ach-card__masked');
    else if (it.hidden) title.classList.add('is-rainbow', 'maarm-text-scan', 'maarm-text-scan--rave');
    front.appendChild(title);

    front.appendChild(el('div', 'maarm-ach-card__desc',
      hiddenLocked ? '隐藏成就 · 解锁后揭晓' : it.description));

    // 锁定隐藏态不渲染页脚：目标值本身就是条件的一部分（后端也不下发）。
    if (!hiddenLocked) {
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
      front.appendChild(foot);
    }
    node.appendChild(front);

    // 背面：一行居中的触发条件。隐藏项未解锁时后端已把 hint 抹空，这里显式说“未揭晓”。
    const back = el('div', 'maarm-ach-card__face maarm-ach-card__back');
    back.appendChild(el('span', 'maarm-ach-card__cond-tag', '触发条件'));
    back.appendChild(el('span', 'maarm-ach-card__cond',
      hiddenLocked ? '？？？' : (it.hint || '暂无说明')));
    node.appendChild(back);
    return node;
  }

  function renderSummary() {
    const done = items.filter((it) => it.unlocked).length;
    const total = items.length;
    const pct = total ? Math.round((done / total) * 100) : 0;
    const frozen = pageState !== 'ready';

    $('ach-sum-done').textContent = frozen ? '—' : String(done);
    $('ach-sum-all').textContent = String(total);
    $('ach-sum-pct').textContent = frozen ? '' : pct + '%';
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
    // 重绘会换掉节点：把弹簧队列与指针态一并作废，别让旧节点继续被积分
    animating.clear();
    hot = null;
    flipped = null;
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

      // 每组一张通用卡：卡头是组名与进度，卡内是成就网格
      const section = el('div', 'card maarm-ach-group');
      const head = el('div', 'card-head');
      head.appendChild(el('h3', null, groupName(modId)));
      const meta = el('div', 'maarm-ach-group__meta');
      meta.appendChild(el('span', 'maarm-ach-group__count',
        `${all.filter((it) => it.unlocked).length} / ${all.length}`));
      const bar = el('span', 'maarm-ach-group__bar');
      const fill = el('i');
      fill.style.width = Math.round((all.filter((it) => it.unlocked).length / all.length) * 100) + '%';
      bar.appendChild(fill);
      meta.appendChild(bar);
      head.appendChild(meta);
      section.appendChild(head);

      const body = el('div', 'card-body');
      const grid = el('div', 'maarm-ach-grid');
      shown.forEach((it) => grid.appendChild(card(it)));
      body.appendChild(grid);
      section.appendChild(body);
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
    if (pageState === 'frozen' && frozenReason === 'corrupt') {
      banner.hidden = false;
      banner.textContent = '成就存档含无法识别的内容，且本地无法留备份：本次已停止读写，'
        + '原文件保持不变。可在数据目录手动备份该文件后反馈问题。';
    } else if (pageState === 'frozen') {
      banner.hidden = false;
      banner.textContent = `成就存档由更高版本（v${storeVersion}）写入，本程序支持到 v${supportedVersion}：`
        + '本次不读写成就进度。升级程序后会自动恢复；在此之前可以先导出备份。';
    } else if (pageState === 'unavailable') {
      banner.hidden = false;
      banner.textContent = '成就功能暂不可用（引擎未初始化）：本次运行的成就不会被记录，其他功能不受影响。';
    } else if (repairedBackup) {
      banner.hidden = false;
      banner.textContent = `成就存档含非法数值，已自动修复；原文件已备份为「${repairedBackup}」。`;
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
      const base = String(res.path || '').split(/[\\/]/).pop();
      status(`已导出 ${res.count} 条 · ${base}`, res.path);
    } catch (e) {
      status('导出失败');
      showError('导出失败：' + (e && e.message ? e.message : e));
    }
  }

  function confirmThen(title, bodyText, okText, run) {
    const modal = openModal({
      title,
      buttons: [
        // 取消必须自己关：openModal 的按钮只跑传进来的 onClick，不内置关闭语义
        { text: '取消', onClick: (m) => m.close() },
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

  // ---------- 镜面反光：指针跟随 ----------
  // 一层委托 + rAF 合并，不为每张卡挂监听。只有已解锁的卡参与——未解锁保持哑光，
  // 而且连合成层都不给它建（提层会让文字从次像素抗锯齿退化成灰度，看着像抖了一像素）。
  //
  // 数值与积分方式照 reactbits TiltedCard 的实现：二阶弹簧 v += (ω²(target−x) − 2ζω·v)·h，
  // 240Hz 子步防大 dt 失稳。三组参数也照它——tilt ω=20·0.2^smoothness、ζ=1−0.3·smoothness
  // （默认 smoothness 0.6 → 7.62 / 0.82），翻转 ω=9.5、ζ=0.72（略欠阻尼，到位轻轻回弹），
  // 按压 ω=26、ζ=0.8。翻面与倾斜合进同一个 rotateY，转的是卡本身，不是卡里的图。
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const TILT_X = 5.5;             // 上下倾斜上限（度）
  const TILT_Y = 7;               // 左右倾斜上限（度）
  const HOVER_SCALE = 1.03;       // 悬停放大
  const PRESS_SCALE = 0.02;       // 按下缩小量（参考实现同值）
  const PRESS_MOVE_PX = 6;        // 按下后位移超过它 = 拖动，不算点击（同参考实现）
  const OMEGA_TILT = 20 * Math.pow(0.2, 0.6);
  const ZETA_TILT = 1 - 0.3 * 0.6;
  const OMEGA_FLIP = 9.5;
  const ZETA_FLIP = 0.72;
  const OMEGA_PRESS = 26;
  const ZETA_PRESS = 0.8;

  const fx = new WeakMap();       // 每张卡的弹簧状态
  const animating = new Set();    // 本轮还要继续积分的卡（静止就移出，不空转）
  let fxRaf = 0;
  let fxLast = 0;
  let hot = null;                 // 指针当前所在的卡

  const clamp01 = (v) => (v < 0 ? 0 : v > 1 ? 1 : v);

  function fxState(node) {
    let st = fx.get(node);
    if (!st) {
      st = {
        rx: 0, vrx: 0, ry: 0, vry: 0, flip: 0, vflip: 0,
        lift: 0, vlift: 0, press: 0, vpress: 0,
        trx: 0, tryY: 0, tflip: 0, tlift: 0, tpress: 0,
        pressed: false, moved: false, downX: 0, downY: 0,
      };
      fx.set(node, st);
    }
    return st;
  }

  // 单轴弹簧积分；返回是否还在动
  function spring(st, key, target, omega, zeta, dt) {
    const vk = 'v' + key;
    const steps = Math.max(1, Math.ceil(dt / (1 / 240)));
    const h = dt / steps;
    for (let i = 0; i < steps; i++) {
      st[vk] += (omega * omega * (target - st[key]) - 2 * zeta * omega * st[vk]) * h;
      st[key] += st[vk] * h;
    }
    if (Math.abs(target - st[key]) < 0.001 && Math.abs(st[vk]) < 0.01) {
      st[key] = target;
      st[vk] = 0;
      return false;
    }
    return true;
  }

  function paint(node, st) {
    const scale = 1 + (HOVER_SCALE - 1) * st.lift - PRESS_SCALE * st.press;
    node.style.setProperty('--rx', st.rx.toFixed(2) + 'deg');
    node.style.setProperty('--ry', st.ry.toFixed(2) + 'deg');
    node.style.setProperty('--flip', st.flip.toFixed(2) + 'deg');
    node.style.setProperty('--scale', scale.toFixed(4));
    node.style.setProperty('--lift', (-2 * st.lift).toFixed(2) + 'px');
  }

  function tick(now) {
    fxRaf = 0;
    const dt = Math.min(0.05, Math.max(0, (now - fxLast) / 1000));
    fxLast = now;
    const rm = reducedMotion.matches;
    const gone = [];
    animating.forEach((node) => {
      const st = fxState(node);
      let moving = false;
      moving = spring(st, 'rx', rm ? 0 : st.trx, OMEGA_TILT, ZETA_TILT, dt) || moving;
      moving = spring(st, 'ry', rm ? 0 : st.tryY, OMEGA_TILT, ZETA_TILT, dt) || moving;
      moving = spring(st, 'lift', st.tlift, OMEGA_TILT, Math.max(ZETA_TILT, 0.85), dt) || moving;
      moving = spring(st, 'press', st.tpress, OMEGA_PRESS, ZETA_PRESS, dt) || moving;
      moving = spring(st, 'flip', st.tflip, rm ? 18 : OMEGA_FLIP, rm ? 1 : ZETA_FLIP, dt) || moving;
      paint(node, st);
      if (!moving) gone.push(node);
    });
    gone.forEach((node) => animating.delete(node));
    if (animating.size) fxRaf = requestAnimationFrame(tick);
  }

  function wake(node) {
    animating.add(node);
    if (!fxRaf) {
      fxLast = performance.now();
      fxRaf = requestAnimationFrame(tick);
    }
  }

  // 镜面高光：位置与角度仍跟着指针即时走，倾斜则由弹簧带过去
  function paintGlare(node, px, py) {
    node.style.setProperty('--glare', '1');
    node.style.setProperty('--mx', (px * 100).toFixed(1) + '%');
    node.style.setProperty('--my', (py * 100).toFixed(1) + '%');
    node.style.setProperty('--ang', (100 + px * 55).toFixed(0) + 'deg');
    node.style.setProperty('--hue', (px * 300).toFixed(0) + 'deg');
  }

  function leaveCard(node) {
    const st = fxState(node);
    st.trx = 0; st.tryY = 0; st.tlift = 0; st.tpress = 0;
    st.pressed = false;
    node.style.setProperty('--glare', '0');
    wake(node);
    if (hot === node) hot = null;
  }

  function sheetCard(target) {
    const sheet = $('ach-sheet');
    if (!sheet || sheet.hidden) return null;
    return target && target.closest ? target.closest('.maarm-ach-card') : null;
  }

  document.addEventListener('pointermove', (e) => {
    const node = sheetCard(e.target);
    if (hot && hot !== node) leaveCard(hot);
    if (!node) return;
    hot = node;
    const st = fxState(node);
    if (st.pressed
      && (Math.abs(e.clientX - st.downX) > PRESS_MOVE_PX
        || Math.abs(e.clientY - st.downY) > PRESS_MOVE_PX)) {
      st.moved = true;          // 拖着走不算点击，松手不翻面
    }
    // 未解锁的卡保持哑光；看背面的卡不再跟着倾斜（文字歪着读不划算）
    if (node.classList.contains('is-unlocked') && !node.classList.contains('is-flipped')) {
      const r = node.getBoundingClientRect();
      const px = clamp01((e.clientX - r.left) / Math.max(1, r.width));
      const py = clamp01((e.clientY - r.top) / Math.max(1, r.height));
      st.trx = (0.5 - py) * TILT_X;
      st.tryY = (px - 0.5) * TILT_Y;
      paintGlare(node, px, py);
    }
    // 未解锁的卡不倾斜也不放大（只有按下时的回弹）——它不该比到手的更显眼
    st.tlift = node.classList.contains('is-unlocked') ? 1 : 0;
    wake(node);
  }, { passive: true });

  document.addEventListener('pointerdown', (e) => {
    const node = sheetCard(e.target);
    if (!node) return;
    const st = fxState(node);
    st.pressed = true;
    st.moved = false;
    st.downX = e.clientX;
    st.downY = e.clientY;
    st.tpress = 1;
    wake(node);
  });

  window.addEventListener('pointerup', () => {
    if (!animating.size && !hot) return;
    animating.forEach((node) => {
      const st = fxState(node);
      if (!st.pressed) return;
      st.pressed = false;
      st.tpress = 0;
      wake(node);
    });
    if (hot) {
      const st = fxState(hot);
      if (st.pressed) { st.pressed = false; st.tpress = 0; wake(hot); }
    }
  }, { passive: true });

  document.addEventListener('pointerleave', () => { if (hot) leaveCard(hot); }, { passive: true });
  const fxScrollHost = document.querySelector('.maarm-ach-body');
  if (fxScrollHost) fxScrollHost.addEventListener('scroll', () => { if (hot) leaveCard(hot); }, { passive: true });

  // ---------- 翻面：点一下卡看触发条件 ----------
  // 只翻一张：翻新的那张时，先前那张自己转回去（同屏两张背面会让人不知道在看哪张）。
  let flipped = null;

  function flipTo(node, on) {
    const st = fxState(node);
    st.tflip = on ? 180 : 0;
    node.classList.toggle('is-flipped', on);
    node.setAttribute('aria-expanded', on ? 'true' : 'false');
    if (on) { st.trx = 0; st.tryY = 0; }   // 背面不倾斜
    wake(node);
  }

  function setFlipped(node) {
    const on = !node.classList.contains('is-flipped');
    if (on && flipped && flipped !== node) flipTo(flipped, false);
    flipTo(node, on);
    flipped = on ? node : null;
  }

  function clearFlip() {
    if (!flipped) return;
    flipTo(flipped, false);
    flipped = null;
  }

  // 拖动收尾：拖过就不翻，并把标记清掉（下一次按下重新计）
  function dragSuppress(node) {
    const st = fxState(node);
    if (!st.moved) return false;
    st.moved = false;
    return true;
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
    if (hot) leaveCard(hot);
    clearFlip();                 // 下次进来从正面开始
    // 关闭前把焦点移出：否则 aria-hidden 会盖住仍在页内的焦点元素（读屏与浏览器都会告警）
    if (sheet.contains(document.activeElement)) document.activeElement.blur();
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
  bind('ach-back', close);           // 返回与 Esc 都回设置页；页头不再单设关闭按钮
  bind('ach-export', doExport);
  bind('ach-import', doImport);
  bind('ach-reset', doReset);
  bind('ach-notify', toggleNotify);

  // 卡片翻面：一层委托（卡片由 renderGroups 重建，不给每张挂监听）
  const groupsHost = $('ach-groups');
  if (groupsHost) {
    groupsHost.addEventListener('click', (e) => {
      const node = e.target && e.target.closest ? e.target.closest('.maarm-ach-card') : null;
      if (!node) { clearFlip(); return; }
      if (dragSuppress(node)) return;   // 刚才是拖动，不是点击
      setFlipped(node);
    });
    // 键盘：卡片是 role=button，Enter/空格等价于点一下
    groupsHost.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      const node = e.target && e.target.closest ? e.target.closest('.maarm-ach-card') : null;
      if (!node) return;
      e.preventDefault();
      setFlipped(node);
    });
  }
  const bodyHost = document.querySelector('.maarm-ach-body');
  if (bodyHost) {
    // 点正文空白处收起背面（点卡片本身由上面的委托处理）
    bodyHost.addEventListener('click', (e) => {
      if (e.target && e.target.closest && e.target.closest('.maarm-ach-card')) return;
      clearFlip();
    });
  }

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    // 有弹窗时 Esc 归弹窗：别连页面一起关掉
    if (document.querySelector('.maarm-modal-overlay')) return;
    if (flipped) { clearFlip(); return; }   // 先收背面，再按一次才离开本页
    close();
  });

  window.MaaRM.achievements = { open, close, refresh };
})();
