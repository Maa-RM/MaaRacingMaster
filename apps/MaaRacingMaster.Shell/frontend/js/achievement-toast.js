// 成就解锁提示条（灵动岛）：圆点入场 → 胶囊（标题 + 描述）→ 详情卡，多条堆叠串行推进。
// 四档（普通 / 稀有 / 珍贵 / 隐藏）落在整条外圈描边上，隐藏档彩色环旋转 + 彩虹标题 + 礼花。
//
// 输入只有一个来源：壳侧推送 `{ type: 'achievement', data: {...} }`。载荷自足——标题、
// 描述、档位、触发条件、解锁时间都在里面，不回查 `get_achievements`（解锁瞬间的查询
// 会与推送竞争）。
//
// 数据安全：catalog 的标题/描述/触发条件来自插件（不可信输入），一律走 textContent；
// 动态文案只进文本节点，不拼 innerHTML。
(function () {
  'use strict';

  const H = 56;              // 紧凑态直径 = 胶囊高度
  const GAP = 8;             // 堆叠间距
  const HOLD = 5000;         // 队首展示时长
  const IDLE = 9000;         // 详情卡无人操作时自动收回
  const CARD_W = 320;        // 详情卡宽
  const CARD_H = 156;        // 详情卡高

  const TIER_LABEL = { normal: '普通', rare: '稀有', precious: '珍贵', hidden: '隐藏' };
  const TIERS = Object.keys(TIER_LABEL);

  let island = null;
  const items = [];          // 旧 → 新：items[0] 是队首（最下方）
  let frontTimer = null;     // 队首的 5 秒计时
  let idleTimer = null;

  function $id(id) { return document.getElementById(id); }

  function clockOf(tsNs) {
    const d = Number.isFinite(tsNs) ? new Date(Number(tsNs) / 1e6) : new Date();
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  // ---------- 礼花（仅隐藏档） ----------
  const FX = (() => {
    const COLORS = ['#38BDF8', '#E5484D', '#F5B23F', '#34D399', '#A78BFA', '#FB7185'];
    let cv = null, ctx = null;
    let W = 0, Hgt = 0, raf = 0, last = 0, pending = 0;
    const parts = [];

    function ensure() {
      if (cv) return;
      cv = $id('ach-fx');
      if (!cv) return;
      ctx = cv.getContext('2d');
      window.addEventListener('resize', fit);
      fit();
    }

    function fit() {
      if (!cv) return;
      const dpr = window.devicePixelRatio || 1;
      W = window.innerWidth;
      Hgt = window.innerHeight;
      cv.width = Math.round(W * dpr);
      cv.height = Math.round(Hgt * dpr);
      cv.style.width = `${W}px`;
      cv.style.height = `${Hgt}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    /** 从底部两侧角落向中心斜上方喷出的纸屑（开伞式阻力：先射出、后骤降） */
    function burstSide(fromLeft) {
      const dir = fromLeft ? 1 : -1;
      for (let i = 0; i < 30; i++) {
        const angle = (26 + Math.random() * 58) * Math.PI / 180;
        const reach = (0.30 + Math.random() * 0.62) * W;
        const speed = Math.min(1950, (reach / 0.44) / Math.cos(angle));
        parts.push({
          x: dir > 0 ? -10 - Math.random() * 60 : W + 10 + Math.random() * 60,
          y: Hgt + 8 + Math.random() * 34,
          vx: dir * Math.cos(angle) * speed,
          vy: -Math.sin(angle) * speed,
          w: 4 + Math.random() * 7,
          h: 8 + Math.random() * 10,
          rot: Math.random() * Math.PI,
          vrot: (Math.random() - 0.5) * 16,
          phase: Math.random() * Math.PI * 2,
          age: 0,
          delay: Math.random() * 0.28,
          life: 4.6 + Math.random() * 1.4,
          drag: 0.009 + Math.random() * 0.004,
          chuteAt: 0.20 + Math.random() * 0.14,
          color: COLORS[(Math.random() * COLORS.length) | 0],
        });
      }
    }

    function step(ts) {
      const dt = Math.min(0.032, (ts - (last || ts)) / 1000);
      last = ts;
      const t = ts / 1000;
      ctx.clearRect(0, 0, W, Hgt);
      for (let i = parts.length - 1; i >= 0; i--) {
        const p = parts[i];
        p.age += dt;
        const a = p.age - p.delay;
        if (a < 0) continue;
        if (a > p.life) { parts.splice(i, 1); continue; }
        const spd = Math.hypot(p.vx, p.vy);
        const k = p.drag * (a < p.chuteAt ? 0.18 : 1) * spd;
        p.vx -= k * p.vx * dt;
        p.vy -= k * p.vy * dt;
        p.vy += 70 * dt;
        p.vx += Math.sin(t * 2 + p.phase) * 22 * dt;
        p.x += p.vx * dt;
        p.y += p.vy * dt;
        p.rot += p.vrot * dt;
        if (p.y > Hgt + 80 || p.x < -140 || p.x > W + 140) { parts.splice(i, 1); continue; }

        ctx.save();
        ctx.globalAlpha = Math.min(1, (p.life - a) / 0.5) * 0.95;
        ctx.fillStyle = p.color;
        const flutter = 0.35 + 0.65 * Math.abs(Math.cos(t * 5 + p.phase));
        ctx.translate(p.x, p.y);
        ctx.rotate(p.rot);
        ctx.fillRect(-p.w * flutter / 2, -p.h / 2, p.w * flutter, p.h);
        ctx.restore();
      }
      if (parts.length || pending) raf = requestAnimationFrame(step);
      else { raf = 0; last = 0; ctx.clearRect(0, 0, W, Hgt); }
    }

    function schedule(fn, ms) {
      pending++;
      setTimeout(() => { pending--; fn(); }, ms);
    }

    function burst() {
      if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
      ensure();
      if (!ctx) return;
      schedule(() => { burstSide(true); burstSide(false); }, 0);
      schedule(() => { burstSide(true); burstSide(false); }, 260);
      if (!raf) { last = 0; raf = requestAnimationFrame(step); }
    }

    return { burst };
  })();

  // ---------- 单条成就 ----------
  function createItem(data) {
    island = island || $id('ach-island');
    if (!island) return null;

    const tier = TIERS.indexOf(data.tier) >= 0 ? data.tier : 'normal';
    const el = document.createElement('article');
    el.className = 'maarm-ach';
    el.dataset.tier = tier;
    el.dataset.state = 'stacked';
    // 静态骨架：只有标记与类名，文案随后逐节点写入（不拼 innerHTML）
    el.innerHTML = [
      '<div class="maarm-ach__face">',
      '  <div class="maarm-ach__badge" data-icon-slot></div>',
      '  <div class="maarm-ach__text">',
      '    <div class="maarm-ach__title"></div>',
      '    <div class="maarm-ach__sub"></div>',
      '  </div>',
      '</div>',
      '<div class="maarm-ach__detail">',
      '  <div class="maarm-ach__detail-head">',
      '    <span class="maarm-ach__detail-title"></span>',
      '    <span class="maarm-ach__tier"></span>',
      '    <span class="maarm-ach__time"></span>',
      '  </div>',
      '  <div class="maarm-ach__detail-desc"></div>',
      '  <div class="maarm-ach__detail-cond"></div>',
      '</div>',
    ].join('');

    const title = data.title || '';
    const desc = data.description || '';
    const slot = el.querySelector('[data-icon-slot]');
    // 图标串只来自本仓库真源 icons.js（静态 SVG），不掺任何运行时数据
    if (slot && window.MaaRMIcons) slot.innerHTML = MaaRMIcons.svg('gift');
    if (tier === 'hidden') {
      el.querySelector('.maarm-ach__title').classList.add('maarm-text-scan', 'maarm-text-scan--rave');
      el.querySelector('.maarm-ach__detail-title').classList.add('maarm-text-scan', 'maarm-text-scan--rave');
    }
    el.querySelector('.maarm-ach__title').textContent = title;
    el.querySelector('.maarm-ach__sub').textContent = desc;      // 副标题只放描述，档位由描边承担
    el.querySelector('.maarm-ach__detail-title').textContent = title;
    el.querySelector('.maarm-ach__tier').textContent = TIER_LABEL[tier] + '成就';
    el.querySelector('.maarm-ach__time').textContent = clockOf(data.unlocked_ts) + ' 解锁';
    el.querySelector('.maarm-ach__detail-desc').textContent = desc;
    el.querySelector('.maarm-ach__detail-cond').textContent = data.hint ? '触发条件：' + data.hint : '';

    el.style.width = `${H}px`;
    el.style.height = `${H}px`;

    const item = { el, tier, state: 'stacked' };
    el.addEventListener('click', (e) => { e.stopPropagation(); onItemClick(item); });
    island.appendChild(el);
    items.push(item);
    layout();
    if (tier === 'hidden') FX.burst();

    // 入场：先以圆点弹出，再展开成胶囊入队
    requestAnimationFrame(() => requestAnimationFrame(() => {
      el.classList.add('is-shown');
      setTimeout(() => {
        if (item.state !== 'stacked') return;
        el.style.width = `${fullWidth(el)}px`;
      }, 200);
    }));
    return item;
  }

  // 旧的在前（下方），新的往上排：位置用布局属性，避免 transform 让文字留在合成层
  function layout() {
    let y = 0;
    items.forEach((item, i) => {
      item.el.style.setProperty('--dy', `${y}px`);
      item.el.style.zIndex = String(100 - i);
      y += (item.state === 'detail' ? CARD_H : H) + GAP;
    });
  }

  function fullWidth(el) {
    const prevW = el.style.width;
    const prevR = el.style.right;
    el.style.transition = 'none';
    // 绝对定位同时有 left/right 时 width:auto 会撑满整行，量宽前先放开 right
    el.style.right = 'auto';
    el.style.width = 'auto';
    const w = Math.min(el.offsetWidth, 460);
    el.style.width = prevW || `${H}px`;
    el.style.right = prevR;
    void el.offsetWidth;
    el.style.transition = '';
    return w;
  }

  function toggleDetail(item) {
    const el = item.el;
    if (item.state === 'detail') {
      item.state = 'stacked';
      el.dataset.state = 'stacked';
      el.style.width = `${fullWidth(el)}px`;
      el.style.height = `${H}px`;
    } else {
      item.state = 'detail';
      el.dataset.state = 'detail';
      el.style.width = `${CARD_W}px`;
      el.style.height = `${CARD_H}px`;
    }
    layout();
  }

  // 串行推进：队首展示满 5 秒即消失，后一条顶上
  function startFrontTimer() {
    clearTimeout(frontTimer);
    const front = items[0];
    if (!front) return;
    frontTimer = setTimeout(() => {
      if (items[0] !== front || front.state === 'detail') return;
      dismiss(front);
    }, HOLD);
  }

  function resumeTimer() {
    clearTimeout(frontTimer);
    if (items.some((it) => it.state === 'detail')) return;   // 用户正展开某条，计时暂停
    startFrontTimer();
  }

  function dismiss(item) {
    const i = items.indexOf(item);
    if (i < 0) return;
    items.splice(i, 1);
    clearTimeout(frontTimer);
    const el = item.el;
    el.classList.remove('is-shown');
    el.style.width = `${H}px`;
    el.style.height = `${H}px`;
    setTimeout(() => el.remove(), 420);
    layout();
    startFrontTimer();
  }

  function onItemClick(item) {
    clearTimeout(idleTimer);
    toggleDetail(item);
    resumeTimer();
    armIdle();
  }

  function armIdle() {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
      const open = items.find((it) => it.state === 'detail');
      if (open) {
        toggleDetail(open);
        resumeTimer();
      }
    }, IDLE);
  }

  function dismissAll() {
    clearTimeout(frontTimer);
    clearTimeout(idleTimer);
    for (const item of items) {
      item.el.classList.remove('is-shown');
      setTimeout(() => item.el.remove(), 420);
    }
    items.length = 0;
  }

  function push(data) {
    if (!data || !data.title) return;
    createItem(data);
    if (items.length === 1) startFrontTimer();   // 只有队首计时，后来者候队
    armIdle();
  }

  // 点空白处收回详情卡
  document.addEventListener('click', () => {
    const open = items.find((it) => it.state === 'detail');
    if (!open) return;
    toggleDetail(open);
    resumeTimer();
    armIdle();
  });

  // ---------- 接线：只消费壳侧转发的第二层事件 ----------
  if (window.maarm && typeof window.maarm.onNativeMessage === 'function') {
    window.maarm.onNativeMessage((msg) => {
      if (msg && msg.type === 'achievement') push(msg.data);
    });
  }
  window.MaaRM = window.MaaRM || {};
  window.MaaRM.achievementToast = { push, dismissAll };
})();
