// MaaRM shell 前端 —— 启动装配入口（在 index.html 中最后加载）。
// 职责：tab 切换、窗口控制、模块下拉、关于页、init 装配与全局事件绑定入口。
// 通信层与共享 state 在 js/rpc.js；弹窗在 js/modal.js；日志在 js/log.js；预览在
// js/preview.js；模块选项/优化中心/模块页面在 js/settings.js；运行控制在 js/run.js。
// 共享物一律经 window.MaaRM 传递（下方导入清单）。
//
// 关于页「检查更新 / 公告」与 ViGEmBus 引导弹窗保留在本文件：
// tests/test_frontend_remote_render.py 静态锁按函数签名从本文件抽取
// checkUpdate / fetchAnnouncement 的函数体（且抽取锚点要求签名串的首个出现位置
// 即函数定义，故本注释不得写出完整签名文本），并断言本文件存在
// open_external_url / open_vigembus_download / openTarget 调用点——迁出会让 CI 即红
//（该锁的看护范围调整需另行修改 Python 测试，不在本次拆分范围）。
(function () {
  'use strict';

  // 共享物导入（js/ 各模块已先行加载）
  const {
    maarm, $, state, showError, reportError, mkEl, appendIcon, postWindowAction,
    openModal, setPreviewFloating, peepConsumer, waitFor, twoFrames, delay,
    renderModulePages, checkRegistryOptimizations, pollDashboard,
    renderTaskList, pollStatus, setStatus, pollLogs,
  } = window.MaaRM;

  // ---------- ViGEmBus 驱动缺失引导弹框 ----------
  // 外链一律只报**逻辑目标名**（home / issue / docs / announcement / download / vigembus），
  // 地址由 sidecar 侧白名单映射（core/remote_meta.py EXTERNAL_TARGETS）——前端与远程数据
  // 都不持有可打开的 URL，XSS 也就无法把「打开浏览器」的能力用在别处。
  // VIGEM_DL_URL 是本仓库静态常量（非远程数据），只用于弹框里的 <a> 与后端失败兜底。
  const VIGEM_DL_URL = 'https://github.com/nefarius/ViGEmBus/releases/latest';
  function showVigemDialog(detailMsg) {
    void detailMsg;
    return openModal({
      title: '缺少 ViGEmBus 驱动',
      titleColor: 'var(--maarm-danger,#ef4444)',
      bodyHtml:
        '<p class="maarm-modal-text">' +
        '需要虚拟手柄（vgamepad）控制的模块，底层依赖 <b>ViGEmBus</b> 内核驱动。它无法随解压包分发，需在本机安装一次。</p>',
      buttons: [
        { text: '手动打开下载页', asLink: true, href: VIGEM_DL_URL },
        {
          text: '下载并安装 ViGEmBus 驱动', primary: true,
          onClick: async (modal) => {
            try {
              await maarm.call('open_vigembus_download', {});
            } catch (err) {
              // 后端打开失败：前端兜底新开标签页
              window.open(VIGEM_DL_URL, '_blank');
            }
            showError('已打开 ViGEmBus 下载页。下载安装后请重新运行。');
            modal.close();
          }
        },
        { text: '暂不', onClick: (modal) => modal.close() },
      ],
    });
  }
  // run.js 的 _doStart 先于本文件加载，经 MaaRM 延迟取用
  Object.assign(window.MaaRM, { showVigemDialog });

  // 本模块拥有的预热面：ViGEmBus 引导弹窗。其余预热面由各自模块登记，入口只跑一遍。
  window.MaaRM.registerWarmup({
    label: '驱动引导',
    order: 50,
    async run() {
      const modal = showVigemDialog('');
      if (!modal) return false;
      try {
        await twoFrames();
        return true;
      } finally {
        modal.close();
      }
    },
  });

  // ---------- Tab 切换 ----------
  const tabButtons = Array.from(document.querySelectorAll('.maarm-tab'));
  const TAB_ORDER = ['control', 'data', 'settings', 'about'];
  let _curTabIdx = 0; // 初始即主控 tab（HTML 默认激活），防止首次点当前 tab 误触发切换动画
  function switchTab(name) {
    // 预热遮罩期间不接受切页：遮罩挡住鼠标但拦不住键盘，用户误触会把 tab 状态
    // 与预热自己翻的页面搅乱（收尾只切回主控页，顶栏却还停在别的 tab）。
    if (!_bootReleased) return;
    const nextIdx = TAB_ORDER.indexOf(name);
    if (nextIdx === _curTabIdx) return; // 点击当前激活 tab：不重播切换动画
    const prevIdx = _curTabIdx >= 0 ? _curTabIdx : nextIdx;
    _curTabIdx = nextIdx;
    const fromRight = nextIdx > prevIdx; // 前进：新页从右滑入；后退：从左滑入
    tabButtons.forEach((btn) => {
      btn.classList.toggle('maarm-tab--active', btn.dataset.tab === name);
    });
    TAB_ORDER.forEach((p) => {
      $('page-' + p).classList.toggle('hidden', p !== name);
    });
    animatePageIn($('page-' + name), fromRight);
    moveTabSlider(name);
    if (name === 'data') pollDashboard(); // 切回数据页立即补拉一次看板，不等下一个 3s 轮询拍
    if (name === 'about') fetchAnnouncement(); // 切到关于页重拉公告，避免停留在启动时的旧缓存
    glitchVersionEgg(); // 1% 概率版本号乱码（彩蛋，详见 glitchVersionEgg 注释）
  }
  // 滑块缓动平移到目标 tab 底部（用 offsetLeft/offsetWidth，不逐页遍历）
  // 宽度取 tab 的 85%，并在 tab 内水平居中
  function moveTabSlider(name) {
    const btn = tabButtons.find((b) => b.dataset.tab === name);
    const slider = $('tab-slider');
    if (!btn || !slider) return;
    const w = Math.round(btn.offsetWidth * 0.85);
    slider.style.left = (btn.offsetLeft + Math.round((btn.offsetWidth - w) / 2)) + 'px';
    slider.style.width = w + 'px';
  }
  // 分块进场：新页的每个面板各自从 tab 方向滑入 + 对焦收清，起步时间按对角位置错开。
  // 前进（切到右边的 tab）从右滑入、从右列起；后退反之——和 tab 的左右顺序一致。
  // 位移走 rAF 逐帧写样式，不用 CSS animation：CSS 的 transform 动画会被合成器接管，
  // 运动期间按低分辨率位图平移、停下重新栅格，观感是「尾部突然变清」；主线程逐帧改样式
  // 则每帧都按设备像素重绘，全程锐利（实测同位移下 46% vs 101%）。
  // 页面本身不做变换：整页一变形，全屏文字会同时被降分辨率重绘。
  const ENTER_DURATION_MS = 420;
  const ENTER_SPREAD_MS = 200;  // 对角首尾块的起步差
  const ENTER_SHIFT_PX = 28;    // 单块滑动距离
  const ENTER_BLUR_PX = 4;      // 起始模糊：与参考组件同值，进场时明显对焦
  // cubic-bezier(0.22, 1, 0.36, 1)：先快后缓，收尾无回弹
  const ENTER_EASE = (() => {
    const [x1, y1, x2, y2] = [0.22, 1, 0.36, 1];
    const cx = 3 * x1, bx = 3 * (x2 - x1) - cx, ax = 1 - cx - bx;
    const cy = 3 * y1, by = 3 * (y2 - y1) - cy, ay = 1 - cy - by;
    const sx = (t) => ((ax * t + bx) * t + cx) * t;
    const sy = (t) => ((ay * t + by) * t + cy) * t;
    const dx = (t) => (3 * ax * t + 2 * bx) * t + cx;
    return (x) => {
      if (x <= 0) return 0;
      if (x >= 1) return 1;
      let t = x;
      for (let i = 0; i < 5; i++) {
        const d = dx(t);
        if (Math.abs(d) < 1e-6) break;
        t -= (sx(t) - x) / d;
      }
      return sy(Math.min(1, Math.max(0, t)));
    };
  })();
  let _enterRaf = 0;
  let _enterPrev = []; // 上一轮正在动的元素，切页打断时要收回内联样式

  function animatePageIn(page, fromRight) {
    if (!page) return;
    // 打断上一轮：取消帧循环并把内联样式收干净，否则旧页会带着 transform 进后台
    if (_enterRaf) cancelAnimationFrame(_enterRaf);
    _enterRaf = 0;
    for (const item of _enterPrev) item.el.style.cssText = item.saved;
    _enterPrev = [];
    // 只取最外层块，避免卡片套卡片重复播动画；没有卡片时退化为整页一块
    const all = Array.from(page.querySelectorAll('.card, .dash-panel, .about-hero'));
    const blocks = all.filter((el) => !all.some((other) => other !== el && other.contains(el)));
    const targets = blocks.length ? blocks : [page];
    if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    // 对角排序：水平权重是垂直的两倍——进场方向是横向的。等权时左列首行与右列末行
    // 会算出同一个对角值（数据页左栏整列高，正好是这个格局），两块就会同时起步。
    const rects = targets.map((el) => el.getBoundingClientRect());
    const minTop = Math.min(...rects.map((r) => r.top));
    const minLeft = Math.min(...rects.map((r) => r.left));
    const spanTop = Math.max(1, Math.max(...rects.map((r) => r.top)) - minTop);
    const spanLeft = Math.max(1, Math.max(...rects.map((r) => r.left)) - minLeft);
    const shift = fromRight ? ENTER_SHIFT_PX : -ENTER_SHIFT_PX;
    const diags = targets.map((el, i) => {
      const xRatio = (rects[i].left - minLeft) / spanLeft;
      const yRatio = (rects[i].top - minTop) / spanTop;
      return (fromRight ? 1 - xRatio : xRatio) + 0.5 * yRatio;
    });
    // 起步时间按名次等距摊开，不按对角值比例：对角值仍可能撞车（同列不同行、行列互补
    // 等），撞车的块会同一帧起步；按名次摊开保证任意两块都错开。
    const order = targets.map((el, i) => i).sort((a, b) => (
      diags[a] - diags[b] || rects[a].top - rects[b].top || rects[a].left - rects[b].left
    ));
    const step = order.length > 1 ? ENTER_SPREAD_MS / (order.length - 1) : 0;
    const items = order.map((idx, rank) => ({
      el: targets[idx],
      saved: targets[idx].style.cssText,
      delay: Math.round(rank * step),
    }));
    _enterPrev = items;
    const start = performance.now();
    const apply = (item, local) => {
      const p = local <= 0 ? 0 : local >= 1 ? 1 : ENTER_EASE(local);
      const el = item.el;
      el.style.transform = 'translateX(' + (shift * (1 - p)).toFixed(2) + 'px)';
      el.style.opacity = p.toFixed(3);
      el.style.filter = 'blur(' + (ENTER_BLUR_PX * (1 - p)).toFixed(2) + 'px)';
    };
    // 首帧先压到起始态，避免延迟块先以终态闪一下
    for (const item of items) apply(item, 0);
    const frame = (now) => {
      const t = now - start;
      let done = true;
      for (const item of items) {
        const local = (t - item.delay) / ENTER_DURATION_MS;
        apply(item, local);
        if (local < 1) done = false;
      }
      if (!done) {
        _enterRaf = requestAnimationFrame(frame);
        return;
      }
      _enterRaf = 0;
      for (const item of items) item.el.style.cssText = item.saved;
      _enterPrev = [];
    };
    _enterRaf = requestAnimationFrame(frame);
  }
  tabButtons.forEach((btn) => {
    btn.addEventListener('click', () => {
      // 成就管理是设置页的子页：切 tab 即退出它（再点当前 tab 等于返回设置）
      if (window.MaaRM.achievements) MaaRM.achievements.close();
      switchTab(btn.dataset.tab);
    });
  });
  moveTabSlider('control'); // 初始：主控 tab 默认激活，滑块落地到首项
  // 字体异步加载完成后 tab 宽度会变，滑块按旧宽度算的位置/长度会错位：就绪后重算一次
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(() => moveTabSlider(TAB_ORDER[_curTabIdx]));
  }
  // 关于页底部跳转按钮（data-link 值即逻辑目标名，经 sidecar 用默认浏览器打开）
  document.querySelectorAll('[data-link]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const target = btn.dataset.link;
      if (!target) return;
      try {
        await maarm.call('open_external_url', { target });
      } catch (e) {
        showError('打开链接失败: ' + e.message);
      }
    });
  });

  // ---------- 关于页：检查更新 / 公告 ----------
  // 渠道/下载源的真源是 update_status 快照（偏好由壳存用户数据根、随升级保留）；
  // 前端只在点击后即时高亮，收到快照即以快照校正。
  // 两个下载地址指向同一发行货架：切源只换首选（下次检查生效），切渠道才清空重查。
  let updateSource = 'cnb';
  let updateChannel = 'stable';
  let autoCheckEnabled = true; // 后台自动检查开关回显（真源是快照）

  function applySettingsSnapshot(snapshot) {
    if (!snapshot) return;
    if (snapshot.source === 'cnb' || snapshot.source === 'github') updateSource = snapshot.source;
    if (snapshot.channel === 'stable' || snapshot.channel === 'preview') updateChannel = snapshot.channel;
    if (typeof snapshot.auto_check === 'boolean') autoCheckEnabled = snapshot.auto_check;
    renderUpdateSettings();
  }

  function renderUpdateSettings() {
    document.querySelectorAll('#upd-source-options .upd-set-opt').forEach((item) => {
      const active = item.dataset.source === updateSource;
      item.classList.toggle('is-active', active);
      item.setAttribute('aria-pressed', String(active));
    });
    document.querySelectorAll('#upd-channel-options .upd-set-opt').forEach((item) => {
      const active = item.dataset.channel === updateChannel;
      item.classList.toggle('is-active', active);
      item.setAttribute('aria-pressed', String(active));
    });
    const note = $('upd-set-note');
    if (note) {
      note.textContent = '切换版本渠道立即重新检查；切换下载源自下次检查起作为首选';
    }
    const autoCheck = $('upd-auto-check');
    if (autoCheck) autoCheck.checked = autoCheckEnabled;
  }

  function closeUpdateSettings() {
    const pop = $('update-settings-popover');
    const trigger = $('btn-update-settings');
    if (pop) pop.hidden = true;
    if (trigger) trigger.setAttribute('aria-expanded', 'false');
  }

  function openUpdateSettings() {
    const pop = $('update-settings-popover');
    const trigger = $('btn-update-settings');
    if (!pop || !trigger) return;
    const rect = trigger.getBoundingClientRect();
    pop.style.top = Math.round(rect.bottom + 6) + 'px';
    pop.style.right = Math.max(8, Math.round(window.innerWidth - rect.right)) + 'px';
    renderUpdateSettings();
    pop.hidden = false;
    trigger.setAttribute('aria-expanded', 'true');
  }

  async function setUpdateSource(source) {
    const value = source === 'github' ? 'github' : 'cnb';
    updateSource = value; // 即时回显；真源以 RPC 返回的快照为准
    renderUpdateSettings();
    closeUpdateSettings();
    try {
      // 回包要同时刷新版本卡与提醒点（切源会前进修订号），不只刷设置高亮
      renderSnapshot(await maarm.call('update_set_source', { value }));
    } catch (e) {
      showError('切换下载源失败：' + e.message);
      await refreshUpdateStatus(); // 失败回退到后端真实状态
    }
  }

  async function setUpdateChannel(channel) {
    const value = channel === 'preview' ? 'preview' : 'stable';
    updateChannel = value;
    renderUpdateSettings();
    closeUpdateSettings();
    try {
      // 切渠道会取消旧索引并当场重查，回包是新渠道的结果；经同一渲染入口落到版本卡
      renderSnapshot(await maarm.call('update_set_channel', { value }));
    } catch (e) {
      showError('切换渠道失败：' + e.message);
      await refreshUpdateStatus();
    }
  }

  // 打开外部目标：只传逻辑目标名，地址由 sidecar 侧白名单给出（前端不传 URL）。
  function openTarget(target) {
    if (!target) return;
    maarm.call('open_external_url', { target }).catch((e) => showError('打开链接失败: ' + e.message));
  }

  // 渲染「版本与更新」卡状态：cls 决定配色，text 只作纯文本落地。
  // 入参是字符串而不是 HTML——调用点会喂远程文案（latest_tag）与异常信息，一旦拼接
  // 就等于把远程数据交给 HTML 解析器。
  function renderUpdateStatus(statusEl, cls, text, opts) {
    statusEl.className = 'ver-status ' + cls;
    statusEl.textContent = '';
    if (opts && opts.spinner) statusEl.appendChild(mkEl('span', 'spinner'));
    statusEl.appendChild(document.createTextNode(text));
  }

  // ---------- 版本与更新卡（宿主更新协调器驱动） ----------
  // 主流程只发固定意图（update_* 宿主动作；不携带来源/路径/目标版本组合——来源由
  // 壳从安装树随包配置读取）。下载任务在壳内协调器：跨页面切换不中断，关于页重开
  // 时用 update_status 恢复快照；进度渲染带修订守卫（revision 只前进才渲染，旧响应
  // 不回退进度）。升级操作区动态文案一律 textContent 落地，不进 HTML 解析。

  let lastUpdateRevision = -1;
  let updatePollTimer = null;

  // 后台自动检查的推送：壳在检查提交成功后主动发快照（不必等用户打开关于页）。
  // 与轮询/回包共用 renderSnapshot 的修订守卫；页面初始化的 update_status 查询
  // 可补回导航间错过的推送。
  maarm.onNativeMessage((msg) => {
    if (!msg || msg.type !== 'update-status') return;
    renderSnapshot(msg.data);
  });

  function updateElements() {
    return {
      actionBtn: $('btn-update-action'),
      actionLabel: $('btn-update-action-label'),
      statusEl: $('about-update-status'),
      releaseEl: $('about-new-release'),
      remindRow: $('upd-remind-row'),
      remindCheck: $('upd-remind-check'),
      remindHint: $('upd-remind-hint'),
      tabDot: $('about-tab-dot'),
    };
  }

  // 卡头主按钮「吸收」检查/下载/重启升级：按钮留在原位，按相位改文字与处理器。
  // 图标随动作类别切换（检查=旋转箭头、下载/升级=右上箭头）；primary=false 用白色。
  function setMainAction(label, handler, icon, primary) {
    const { actionBtn, actionLabel } = updateElements();
    if (!actionBtn || !actionLabel) return;
    actionLabel.textContent = label;
    actionBtn.onclick = handler || null;
    actionBtn.disabled = !handler;
    actionBtn.classList.toggle('maarm-btn--primary', primary !== false);
    const iconEl = actionBtn.querySelector('i[data-icon]');
    if (iconEl && icon) iconEl.setAttribute('data-icon', icon);
  }

  // 卡体次按钮：取消下载 / 稍后 这类辅助动作，不走主按钮。
  function secondaryAction(label, handler) {
    const btn = mkEl('button', 'maarm-btn', label);
    btn.type = 'button';
    btn.addEventListener('click', handler);
    return btn;
  }

  function stopUpdatePolling() {
    if (updatePollTimer) {
      clearInterval(updatePollTimer);
      updatePollTimer = null;
    }
  }

  function startUpdatePolling() {
    if (updatePollTimer) return;
    updatePollTimer = setInterval(async () => {
      try {
        const d = await maarm.call('update_status');
        renderSnapshot(d);
      } catch (e) {
        // 状态轮询失败不打断下载任务本身，下一拍重试
      }
    }, 500);
  }

  function setupManualDownloadAction() {
    setMainAction('前往下载', () => openTarget('download'), 'arrow-up-right');
  }

  function clearReleaseArea() {
    const { releaseEl } = updateElements();
    releaseEl.textContent = '';
    releaseEl.style.display = 'none';
  }

  // 关于标签上的更新提醒点：只在「发现可更新目标且本次未选择不再提示」时亮起。
  // 位置锚在文字外框（.maarm-tab-label）上，不随按钮内边距漂移。
  function renderUpdateDot(snapshot) {
    const { tabDot } = updateElements();
    const tab = document.querySelector('.maarm-tab[data-tab="about"]');
    if (!tabDot || !tab) return;
    const target = snapshot ? snapshot.target_version : null;
    const on = !!(snapshot && snapshot.phase === 'available'
      && target && snapshot.dismissed_version !== target);
    tabDot.classList.toggle('is-on', on);
    if (on) {
      tab.title = '发现新版本 v' + target;
      tab.setAttribute('aria-label', '关于，发现新版本 v' + target);
    } else {
      tab.removeAttribute('title');
      tab.removeAttribute('aria-label');
    }
  }

  // 「本次更新不再提示」只在发现可更新目标时出现；勾选状态回显自快照。
  function renderRemindRow(snapshot) {
    const { remindRow, remindCheck, remindHint } = updateElements();
    if (!remindRow || !remindCheck || !remindHint) return;
    const target = snapshot ? snapshot.target_version : null;
    const active = !!(snapshot && snapshot.phase === 'available' && target);
    remindRow.hidden = !active;
    if (!active) return;
    const dismissed = snapshot.dismissed_version === target;
    remindCheck.checked = dismissed;
    remindHint.textContent = dismissed
      ? '「关于」上的提醒点已隐藏，下个版本会重新提示'
      : '仅对 v' + target + ' 生效';
  }

  // 快照 → 状态行 + 卡头主按钮 + 卡体次动作区。主按钮吸收检查/下载/重启升级，
  // 卡体只留进度条与取消/稍后这类辅助动作。error 如实显示且保留恢复入口。
  function renderSnapshot(snapshot) {
    if (!snapshot || typeof snapshot.revision !== 'number') return;
    if (snapshot.revision < lastUpdateRevision) return; // 修订守卫：旧响应不回退进度
    lastUpdateRevision = snapshot.revision;
    applySettingsSnapshot(snapshot); // 渠道/源真源来自后端快照
    const { statusEl, releaseEl } = updateElements();
    if (!statusEl || !releaseEl) return;
    const phase = snapshot.phase;
    const target = snapshot.target_version ? ' v' + snapshot.target_version : '';

    if (phase !== 'downloading') stopUpdatePolling();

    clearReleaseArea();
    renderUpdateDot(snapshot);
    renderRemindRow(null); // 默认收起，available 分支再按快照展开
    switch (phase) {
      case 'unavailable':
        renderUpdateStatus(statusEl, 'ver-status--err', snapshot.error || '自动升级不可用');
        setupManualDownloadAction();
        break;
      case 'idle':
        // 全源失败/来源未配置都以 idle + error 返回：失败用错误色，不能伪装成绿色「就绪」
        renderUpdateStatus(statusEl, snapshot.error ? 'ver-status--err' : 'ver-status--ok',
          snapshot.error || '');
        setMainAction('检查更新', checkUpdate, 'rotate-cw', false);
        break;
      case 'checking':
        renderUpdateStatus(statusEl, 'ver-status--checking', '正在检查更新…', { spinner: true });
        setMainAction('检查中…', null, 'rotate-cw');
        break;
      case 'up_to_date':
        // 从测试轨切回且稳定版尚未追上时如实说明，不假装「已是最新版本」。
        renderUpdateStatus(statusEl, 'ver-status--ok',
          snapshot.awaiting_stable ? '当前版本高于稳定渠道，等待下一稳定版' : '已是最新版本');
        setMainAction('检查更新', checkUpdate, 'rotate-cw', false);
        break;
      case 'available':
        // error 只可能来自「旧界面点击被拒」：目标已重新确认过，提示玩家重新操作
        renderUpdateStatus(statusEl, 'ver-status--new',
          snapshot.error ? '发现新版本' + target + '：' + snapshot.error : '发现新版本' + target);
        setMainAction('下载更新', downloadUpdate, 'arrow-up-right');
        renderRemindRow(snapshot);
        break;
      case 'downloading': {
        renderUpdateStatus(statusEl, 'ver-status--checking',
          '下载中 ' + snapshot.progress + '%', { spinner: true });
        setMainAction('下载中…', null, 'arrow-up-right');
        releaseEl.style.display = 'flex';
        const track = mkEl('div', 'update-progress-track');
        const bar = mkEl('div', 'update-progress-bar');
        bar.style.width = snapshot.progress + '%';
        track.appendChild(bar);
        releaseEl.appendChild(track);
        releaseEl.appendChild(secondaryAction('取消下载', cancelDownload));
        startUpdatePolling();
        break;
      }
      case 'ready':
        renderUpdateStatus(statusEl, 'ver-status--ok',
          snapshot.error ? '下载完成，但上一次提交未成功：' + snapshot.error
                         : '已就绪，可升级到' + target);
        setMainAction('重启升级', applyUpdate, 'arrow-up-right');
        releaseEl.style.display = 'flex';
        releaseEl.appendChild(secondaryAction('稍后', () => {
          // 稍后：保留下载结果，收起次动作区；重开关于页可再次恢复
          clearReleaseArea();
          renderUpdateStatus(statusEl, 'ver-status--ok',
            '已就绪' + target + '，可在关于页随时重启升级');
        }));
        break;
      case 'preparing':
      case 'applying':
        // 提交临界区：主按钮禁用显示进度语义，不提供取消（虚假取消）
        renderUpdateStatus(statusEl, 'ver-status--checking',
          phase === 'applying' ? '即将关闭并完成升级…' : '正在准备升级（释放任务与文件占用）…',
          { spinner: true });
        setMainAction(phase === 'applying' ? '正在升级…' : '准备中…', null, 'arrow-up-right');
        break;
      default:
        renderUpdateStatus(statusEl, 'ver-status--err', snapshot.error || '未知状态');
        setMainAction('检查更新', checkUpdate, 'rotate-cw');
    }
  }

  async function refreshUpdateStatus() {
    // 关于页初始化/重开时恢复快照（下载任务在壳内跨页面持续）
    try {
      const d = await maarm.call('update_status');
      renderSnapshot(d);
    } catch (e) {
      // 协调器不可用（极端情况）：状态行静默、主按钮保持初始的检查更新态
    }
  }

  async function checkUpdate() {
    const { statusEl } = updateElements();
    if (!statusEl) return;
    closeUpdateSettings();
    renderUpdateStatus(statusEl, 'ver-status--checking', '正在检查更新…', { spinner: true });
    // 检查在途：主按钮禁用显示「检查中…」，卡体次动作区清空（慢网络下迟到点击不可靠）
    clearReleaseArea();
    setMainAction('检查中…', null, 'rotate-cw');
    try {
      const d = await maarm.call('update_check');
      renderSnapshot(d);
      // 非受管安装 / 来源未配置时，renderSnapshot 的 unavailable 分支已设手动入口；
      // 「来源未配置」以 idle+error 返回，这里补一个手动入口（两种路径都不重复）。
      if (d.phase !== 'unavailable' && d.error && d.error.indexOf('未配置') >= 0) {
        setupManualDownloadAction();
      }
    } catch (e) {
      console.error(e);
      // 失败不得把主按钮留在禁用的「检查中…」：先按当前快照恢复按钮与状态行，再覆盖错误文案
      await refreshUpdateStatus();
      renderUpdateStatus(statusEl, 'ver-status--err', '检查失败：' + e.message);
    }
  }

  async function downloadUpdate() {
    const { statusEl } = updateElements();
    try {
      // 带上玩家看到目标时的修订号：后台已换目标则后端拒绝，不替旧界面下载新版本
      const d = await maarm.call('update_start_download', { revision: lastUpdateRevision });
      renderSnapshot(d);
    } catch (e) {
      console.error(e);
      renderUpdateStatus(statusEl, 'ver-status--err', '下载启动失败：' + e.message);
    }
  }

  async function cancelDownload() {
    try {
      const d = await maarm.call('update_cancel_download');
      renderSnapshot(d);
    } catch (e) {
      console.error(e);
      const { statusEl } = updateElements();
      renderUpdateStatus(statusEl, 'ver-status--err', '取消失败：' + e.message);
    }
  }

  async function applyUpdate() {
    const { statusEl } = updateElements();
    try {
      const d = await maarm.call('update_apply');
      if (d.phase === 'preparing' || d.phase === 'applying') {
        // 提交临界区：壳接管收尾与计划内关窗，界面只如实显示，不提供取消（虚假取消）。
        // 与异常关闭的区分：applying 相位由壳计划内关闭、更新器随后重启新版。
        clearReleaseArea();
        renderUpdateStatus(statusEl, 'ver-status--checking',
          d.phase === 'applying' ? '即将关闭并完成升级…'
                                 : '正在准备升级（释放任务与文件占用）…',
          { spinner: true });
        setMainAction(d.phase === 'applying' ? '正在升级…' : '准备中…', null, 'arrow-up-right');
        return;
      }
      renderSnapshot(d); // 闸门拒绝：状态回 ready，原因与重启升级入口都保留
    } catch (e) {
      console.error(e);
      renderUpdateStatus(statusEl, 'ver-status--err', '提交失败：' + e.message);
    }
  }

  // 拉取并渲染公告（title/date/url_text/body 全部走 textContent）
  async function fetchAnnouncement() {
    const bodyEl = $('about-announce-body');
    if (!bodyEl) return;
    bodyEl.textContent = '';
    try {
      const d = await maarm.call('fetch_announcement');
      if (!d || d.level === 'none' || !d.title) {
        bodyEl.appendChild(mkEl('div', 'about-announce-empty', '暂无公告'));
        return;
      }
      const cls = d.level === 'warn' ? 'about-announce--warn' : 'about-announce--info';
      const card = mkEl('div', 'about-announce ' + cls);
      card.appendChild(mkEl('span', 'about-announce-badge', d.level === 'warn' ? '重要' : '公告'));
      const main = mkEl('div', 'about-announce-main');
      const titleRow = mkEl('div', 'about-announce-title', d.title);
      if (d.date) titleRow.appendChild(mkEl('span', 'about-announce-date', d.date));
      main.appendChild(titleRow);
      if (d.body) main.appendChild(mkEl('div', 'about-announce-body', d.body));
      if (d.url) {
        const linkBtn = mkEl('button', 'about-announce-link', d.url_text || '查看详情');
        linkBtn.id = 'btn-announce-link';
        linkBtn.type = 'button';
        linkBtn.addEventListener('click', () => openTarget('announcement'));
        main.appendChild(linkBtn);
      }
      card.appendChild(main);
      bodyEl.appendChild(card);
    } catch (e) {
      console.error(e);
      bodyEl.textContent = '';
      bodyEl.appendChild(mkEl('div', 'about-announce-empty', '暂无公告'));
    }
  }

  function initAbout() {
    const trigger = $('btn-update-settings');
    const pop = $('update-settings-popover');
    if (trigger && pop) {
      trigger.addEventListener('click', (event) => {
        event.stopPropagation();
        if (pop.hidden) openUpdateSettings(); else closeUpdateSettings();
      });
      pop.querySelectorAll('#upd-source-options .upd-set-opt').forEach((item) => {
        item.addEventListener('click', () => setUpdateSource(item.dataset.source));
      });
      pop.querySelectorAll('#upd-channel-options .upd-set-opt').forEach((item) => {
        item.addEventListener('click', () => setUpdateChannel(item.dataset.channel));
      });
      pop.addEventListener('click', (event) => event.stopPropagation());
      document.addEventListener('click', (event) => {
        if (pop.hidden) return;
        if (pop.contains(event.target) || trigger.contains(event.target)) return;
        closeUpdateSettings();
      });
      document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && !pop.hidden) closeUpdateSettings();
      });
      window.addEventListener('resize', closeUpdateSettings);
      window.addEventListener('scroll', closeUpdateSettings, true);
    }
    renderUpdateSettings();
    const { remindCheck } = updateElements();
    if (remindCheck) {
      remindCheck.addEventListener('change', async () => {
        const value = remindCheck.checked;
        try {
          // 同下载：勾选只对玩家看到的目标版本生效，快照过期就由后端拒绝
          const d = await maarm.call('update_set_dismissed', { value, revision: lastUpdateRevision });
          renderSnapshot(d);
        } catch (e) {
          remindCheck.checked = !value; // 失败回退到真实状态
          showError('更新提醒设置失败：' + e.message);
        }
      });
    }
    const autoCheck = $('upd-auto-check');
    if (autoCheck) {
      autoCheck.addEventListener('change', async () => {
        const value = autoCheck.checked;
        try {
          // 开关回包是快照：经同一渲染入口回显，而不是只改本地勾选
          renderSnapshot(await maarm.call('update_set_auto_check', { value }));
        } catch (e) {
          autoCheck.checked = !value; // 失败回退到真实状态
          showError('自动检查设置失败：' + e.message);
        }
      });
    }
    refreshUpdateStatus(); // 恢复壳内协调器快照（下载任务跨页面持续，重开关于页不丢状态）
    fetchAnnouncement(); // 启动拉一次公告（进入关于页即展示）
  }
  initAbout();

  // ---------- 关于页彩蛋：点击版本号掉落文字（同款 MAA） ----------
  // 掉落池：怪谈短语，每第 FALLING_EGG_EVERY 次点击混入一条（其余掉当前版本号）
  const FALLING_EGG_TEXTS = ['3:33', '不要清点', '它记得你'];
  const FALLING_EGG_EVERY = 5;
  const MAX_FALLING = 40;
  let eggFallCount = 0;
  // 连点 EGG_DIALOG_CLICKS 次（2s 内不中断）弹出「员工守则」
  const EGG_DIALOG_CLICKS = 10;
  let eggClickCount = 0;
  let eggLastClickAt = 0;

  // 员工守则（规则怪谈，致敬 MAA）。结构：池中每次抽 EGG_RULE_SAMPLE 条（编号 1-8，随机）
  // + 第 9 条固定（修订声明，给抽样一个"官方解释"）+ 第 10 条（数字乱码 + 悬停显影）。
  // 条目锚点全部是 GUI 真实存在的元素（静音恢复/日志轮转/状态栏绿点/自动关机/数据页看板）；
  // 口味：执行体口吻不许笑、暗示事件不解释、数字母题（清点/数错）贯穿首尾。
  const EGG_RULE_META = '本守则会不定期修订。你读到的版本，就是你需要读的版本。';
  const EGG_RULES_POOL = [
    '正式版没有「调试模式」。如果你在任何页面看到它，不要点击，也不要截图。开发者会知道你来过。',
    '运行前请断开所有物理手柄。如果你确定已经断开，界面却显示「已连接」，请相信你的手，不要相信界面。',
    '运行期间游戏会被静音，结束后自动恢复。如果音量没有回来，先别动音量键——想一想，刚才那一场，是不是你没有开始过的。',
    '日志按会话分目录，只保留最近若干份。如果某个目录的修改时间早于你安装软件的时间，不要打开它。里面没有你会想看的东西。',
    '左下角的绿点表示系统就绪。它应该一直是静止的。如果你看到它闪了两下，那不是故障，是确认。',
    '从关于页掉落的版本号是正常的。如果它们开始排队，请不要清点数量。',
    '任务结束后它会关闭模拟器。它只会关闭模拟器。到目前为止，它只关闭模拟器。',
    '「停止」按钮随时有效。如果它在你按下之前就已经变灰，说明有人替你停止了这一场。不要去查是谁。',
    '软件不会在你睡觉时更新自己。如果你早上发现版本号变了，那不是更新。',
    '数据页会如实记录今天的每一场。如果数字对不上，多出来的那一场，不算你的。',
    '版本号由版本控制自动推导，任何人不得手改。如果你在关于页看到自己的名字，而你从没参与开发——这一版是专门为你编译的。',
    '软件没有语音。如果你听到低语，先听清它在说什么。多数时候，它只是在重复你自己说过的话。'
  ];
  const EGG_RULE_LAST = '最后一条规则不存在。如果你已经数到了它，说明你数错了。这很正常。';
  const EGG_RULE_SAMPLE = 8;

  function blinkStatusDot() {
    const dot = $('status-dot');
    if (!dot) return;
    dot.classList.remove('maarm-dot-blink');
    void dot.offsetWidth; // 重置动画：摘类→强制回流→再挂
    dot.classList.add('maarm-dot-blink');
  }

  // 离开按钮的 DodgeField [v6]：躲避场 = 整张弹层卡，按钮二维逃跑（能扑到正文上）。
  // 位移 = 背离光标方向 × 强度（随距离平方衰减）× reach；出界钳制 + 撞墙滑移（被夹掉的
  // 分量沿自由轴重分配，角落困不死）+ 角落反困死（残余压力转墙向滑移选离光标最远候选）。
  // 感知基准 = 按钮视觉中心。抓到 = 亮下一句（EGG_CATCH_LINES），末句亮起后放弃躲避，
  // 再抓一次才关门。灵感来自 reactbits.dev 的 DodgeField / DecryptedText（MIT + Commons
  // Clause）——未复制其代码，按公开交互行为 vanilla 自实现。
  // reduced-motion 与触屏不启用；监听挂 window，经返回的 cleanup 随弹层关闭解绑。
  const EGG_DODGE = { radius: 120, reach: 140, falloff: 2, inset: 12 };
  const EGG_CATCH_LINES = ['抓不到', '差一点', '太慢了', '别追了', '你为什么执意要走', '好吧。你走吧。'];
  function wireEggDodge(modal, chase) {
    const field = modal.card;
    const btn = field.querySelector('.maarm-modal-btn');
    if (!field || !btn) return null;
    btn.classList.add('maarm-modal-btn--egg');
    // 抓到 = 亮下一句：pointerdown 无条件挂（按下瞬间即判定，逃跑中也能抓到）。
    // 躲避受环境门控可缺席（reduced-motion/触屏/远桌面下 media query 不匹配），
    // 但交互不能跟着缺席——否则按钮成死按钮，用户被困在弹层里。
    btn.addEventListener('pointerdown', () => {
      chase.catches++;
      const idx = chase.catches - 1;
      if (idx < EGG_CATCH_LINES.length) {
        btn.textContent = EGG_CATCH_LINES[idx];
        if (idx === EGG_CATCH_LINES.length - 1) chase.gave = true;
        return;
      }
      modal.close();
    });
    // 移动层门控：动画缺席时交互退化为纯点击推进
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return null;
    if (!window.matchMedia('(hover: hover) and (pointer: fine)').matches) return null;
    btn.style.minWidth = btn.offsetWidth + 'px'; // 换字不抖行
    let raf = 0, pointer = null;
    let curX = 0, curY = 0; // 当前位移，用于从视觉矩形反推布局位
    const setT = (tx, ty, fleeing) => {
      // 逃离用短促缓出，返回用带回弹的长缓动（对齐原组件 flee/return 双段手感）
      btn.style.transition = 'transform ' + (fleeing ? '.13s' : '.62s') + ' cubic-bezier('
        + (fleeing ? '0.23, 1, 0.32, 1' : '0.34, 1.56, 0.64, 1') + ')';
      btn.style.transform = 'translate(' + tx + 'px,' + ty + 'px)';
      curX = tx; curY = ty;
    };
    function frame() {
      raf = 0;
      if (!document.body.contains(btn)) return; // 弹层已卸载，正式清理走 cleanup
      const cardR = field.getBoundingClientRect();
      const btnR = btn.getBoundingClientRect();
      const layoutL = btnR.left - curX, layoutT = btnR.top - curY;
      const cx = btnR.left + btnR.width / 2, cy = btnR.top + btnR.height / 2;
      const d = pointer ? Math.hypot(pointer.x - cx, pointer.y - cy) : Infinity;
      let flee = 0, tx = 0, ty = 0, fleeing = false;
      if (!chase.gave && pointer && Number.isFinite(d) && d > 0) {
        flee = d < EGG_DODGE.radius ? Math.pow(1 - d / EGG_DODGE.radius, EGG_DODGE.falloff) : 0;
        if (flee > 0) {
          const dx = pointer.x - cx, dy = pointer.y - cy;
          const dd = Math.hypot(dx, dy) || 1;
          tx = -(dx / dd) * flee * EGG_DODGE.reach;
          ty = -(dy / dd) * flee * EGG_DODGE.reach;
          fleeing = true;
        }
      }
      // 出界钳制 + 撞墙滑移：被夹掉的分量沿自由轴重分配——角落不再困死按钮
      const minX = cardR.left + EGG_DODGE.inset - layoutL;
      const maxX = cardR.right - EGG_DODGE.inset - btnR.width - layoutL;
      const minY = cardR.top + EGG_DODGE.inset - layoutT;
      const maxY = cardR.bottom - EGG_DODGE.inset - btnR.height - layoutT;
      const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), Math.max(lo, hi));
      const desiredX = tx, desiredY = ty;
      tx = clamp(desiredX, minX, maxX);
      ty = clamp(desiredY, minY, maxY);
      const lostX = Math.abs(desiredX - tx);
      const lostY = Math.abs(desiredY - ty);
      if (lostY > 0.5) tx = clamp(tx + Math.sign(desiredX || 1) * lostY, minX, maxX);
      if (lostX > 0.5) ty = clamp(ty + Math.sign(desiredY || 1) * lostX, minY, maxY);
      // 角落反困死：主方向被两面墙同时吃掉时（残余压力大），把残余压力转成墙向滑移——
      // 四个候选里选「离光标最远」的那个，总位移仍不超过 reach
      const press = flee > 0 ? flee * EGG_DODGE.reach : 0;
      const got = Math.hypot(tx, ty);
      const residual = press - got;
      if (residual > 6 && pointer) {
        const dist2 = (vx, vy) => {
          const px = layoutL + curX + vx + btnR.width / 2;
          const py = layoutT + curY + vy + btnR.height / 2;
          return (px - pointer.x) ** 2 + (py - pointer.y) ** 2;
        };
        const cands = [
          [clamp(tx + residual, minX, maxX), ty],
          [clamp(tx - residual, minX, maxX), ty],
          [tx, clamp(ty + residual, minY, maxY)],
          [tx, clamp(ty - residual, minY, maxY)],
        ];
        let best = null, bestD = -1;
        for (const cd of cands) {
          const mag = Math.hypot(cd[0], cd[1]);
          const scale = mag > EGG_DODGE.reach ? EGG_DODGE.reach / mag : 1;
          const dd2 = dist2(cd[0] * scale, cd[1] * scale);
          if (dd2 > bestD) { bestD = dd2; best = [cd[0] * scale, cd[1] * scale]; }
        }
        if (best && bestD > dist2(tx, ty)) { tx = best[0]; ty = best[1]; }
      }
      setT(tx, ty, fleeing);
    }
    const onMove = (e) => { pointer = { x: e.clientX, y: e.clientY }; if (!raf) raf = requestAnimationFrame(frame); };
    const onLeave = () => { pointer = null; if (!raf) raf = requestAnimationFrame(frame); };
    window.addEventListener('pointermove', onMove, { passive: true });
    document.addEventListener('pointerleave', onLeave);
    window.addEventListener('blur', onLeave);
    return () => {
      window.removeEventListener('pointermove', onMove);
      document.removeEventListener('pointerleave', onLeave);
      window.removeEventListener('blur', onLeave);
      if (raf) cancelAnimationFrame(raf);
    };
  }

  // [v6] 第 10 条数字乱码：字符集内随机翻动，自左向右逐位落定为「10.」（DecryptedText 手法
  // 自实现）。起播在悬停触发——若挂在渐现延迟里起播，整行还透明，等于白播。
  function scrambleNumber(el, finalText, frames) {
    const glyphs = '01<>#$%&@?!/\\_-';
    let frame = 0;
    // 帧数缺省按文本长度适配（40ms/帧）：版本号长串也来得及看清解码过程
    const total = frames || Math.max(20, Math.ceil(finalText.length * 1.2));
    const timer = setInterval(() => {
      if (!document.body.contains(el)) { clearInterval(timer); return; }
      frame++;
      const settled = Math.floor((frame / total) * finalText.length);
      let s = finalText.slice(0, settled);
      for (let i = settled; i < finalText.length; i++) {
        s += glyphs[Math.floor(Math.random() * glyphs.length)];
      }
      el.textContent = s;
      if (frame >= total) { el.textContent = finalText; clearInterval(timer); }
    }, 40);
    return timer;
  }

  // 1% 概率：切到关于页时版本号套一次乱码解码（与第 10 条同款手法，同源世界观）。
  // 概率常量放在这里：想预览效果临时改成 1 即可。reduced-motion 下不出。
  const EGG_VERSION_GLITCH_P = 0.01;
  let versionScrTimer = null;
  function glitchVersionEgg() {
    if (Math.random() >= EGG_VERSION_GLITCH_P) return;
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const n = $('about-version');
    if (!n || !n.textContent) return;
    if (versionScrTimer) clearInterval(versionScrTimer);
    // 捕获触发时的文本作为落定值；期间若被更新检查重写，下一次重写会覆盖回来
    versionScrTimer = scrambleNumber(n, n.textContent);
  }

  function showRulesDialog() {
    const pool = EGG_RULES_POOL.slice();
    const picked = [];
    while (picked.length < EGG_RULE_SAMPLE && pool.length) {
      picked.push(pool.splice(Math.floor(Math.random() * pool.length), 1)[0]);
    }
    const rows = [];
    picked.forEach((r, i) => {
      rows.push('<p class="maarm-modal-rule" style="animation-delay:' + (i * 100) + 'ms">'
        + '<b class="maarm-modal-rule-num">' + (i + 1) + '.</b><span>' + r + '</span></p>');
    });
    rows.push('<p class="maarm-modal-rule" style="animation-delay:' + (picked.length * 100) + 'ms">'
      + '<b class="maarm-modal-rule-num">9.</b><span>' + EGG_RULE_META + '</span></p>');
    rows.push('<p class="maarm-modal-rule maarm-modal-rule--ghost" style="animation-delay:'
      + (picked.length * 100 + 150) + 'ms"><b class="maarm-modal-rule-num" data-final="10.">10.</b>'
      + '<span class="maarm-modal-rule-secret">' + EGG_RULE_LAST + '</span></p>');
    const chase = { catches: 0, gave: false };
    let cleanupDodge = null;
    let scrTimer = null;
    const modal = openModal({
      title: '员工守则',
      maxWidth: 580,
      bodyHtml:
        '<div class="maarm-modal-rules">' +
        '<img class="maarm-modal-rules-icon" src="../../../assets/icon.ico" alt="">' +
        '<div class="maarm-modal-rules-list">' + rows.join('') + '</div>' +
        '</div>',
      closeOnOverlay: false, // 不给逃课出口：点空白不关（Esc 仍可）
      onClose: () => {
        blinkStatusDot();
        if (cleanupDodge) cleanupDodge();
        if (scrTimer) clearInterval(scrTimer);
      },
      buttons: [
        { text: '确定要退出吗？', primary: true },
      ],
    });
    const title = modal.card.querySelector('.maarm-modal-title');
    if (title) title.classList.add('maarm-modal-title--egg');
    // 第 10 条：整行（序号+正文）默认隐形；悬停时序号乱码解码 + 正文显影，移出复隐
    const ghost = modal.card.querySelector('.maarm-modal-rule--ghost');
    if (ghost) {
      ghost.addEventListener('mouseenter', () => {
        ghost.classList.add('revealed');
        const num = ghost.querySelector('.maarm-modal-rule-num');
        if (scrTimer) clearInterval(scrTimer);
        scrTimer = scrambleNumber(num, '10.');
      });
      ghost.addEventListener('mouseleave', () => ghost.classList.remove('revealed'));
    }
    cleanupDodge = wireEggDodge(modal, chase);
  }

  function spawnFallingText(text, cx, cy, isEgg) {
    const alive = document.querySelectorAll('.falling-text');
    if (alive.length >= MAX_FALLING) alive[0].remove();
    const el = document.createElement('div');
    el.className = 'falling-text' + (isEgg ? ' falling-text--egg' : '');
    el.textContent = text;
    document.body.appendChild(el);
    const w = el.offsetWidth;
    const h = el.offsetHeight;
    let x = cx - w / 2;
    let y = cy - h / 2;
    let vx = (Math.random() - 0.5) * 240;
    let vy = -80 - Math.random() * 100;
    let rot = 0;
    let vr = (Math.random() - 0.5) * 160;
    let bounces = 0;
    let gone = false;
    let last = performance.now();
    function step(now) {
      const dt = Math.min((now - last) / 1000, 0.05);
      last = now;
      vy += 1800 * dt;
      x += vx * dt;
      y += vy * dt;
      rot += vr * dt;
      const floor = window.innerHeight - h - 8;
      if (y > floor) {
        y = floor;
        vy = -vy * 0.42;
        vx *= 0.72;
        vr *= 0.5;
        if (++bounces > 3 || Math.abs(vy) < 60) gone = true;
      }
      el.style.transform = 'translate(' + x + 'px,' + y + 'px) rotate(' + rot + 'deg)';
      if (gone) {
        el.style.opacity = '0';
        setTimeout(() => el.remove(), 500);
        return;
      }
      requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  function initVersionEgg() {
    ['about-version'].forEach((id) => {
      const n = $(id);
      if (!n) return;
      n.addEventListener('click', (e) => {
        const now = Date.now();
        eggClickCount = now - eggLastClickAt < 2000 ? eggClickCount + 1 : 1;
        eggLastClickAt = now;
        if (eggClickCount >= EGG_DIALOG_CLICKS) {
          eggClickCount = 0;
          showRulesDialog();
        }
        eggFallCount++;
        const eggHit = Array.isArray(FALLING_EGG_TEXTS) && FALLING_EGG_TEXTS.length
          && eggFallCount % FALLING_EGG_EVERY === 0;
        const t = eggHit
          ? FALLING_EGG_TEXTS[Math.floor(Math.random() * FALLING_EGG_TEXTS.length)]
          : n.textContent;
        spawnFallingText(t, e.clientX, e.clientY, eggHit);
      });
    });
  }
  initVersionEgg();

  // ---------- 标题栏交互区上报 ----------
  // 交互区（brand / tabs / win-controls）逐元素矩形上报给 C#：精确注册 Passthrough，
  // 空白区保持 Draggable（整条标题栏带，系统处理拖动与双击最大化）
  function reportDragExcludes() {
    const header = document.getElementById('titlebar');
    if (!header || !window.chrome.webview) return;
    const rects = Array.from(header.querySelectorAll('.brand, .tabs, .win-controls'))
      .map((n) => {
        const r = n.getBoundingClientRect();
        return { x: r.left, y: r.top, w: r.width, h: r.height };
      });
    window.chrome.webview.postMessage({
      type: 'drag-exclude',
      rects, // CSS 像素（DIP）
    });
  }
  reportDragExcludes();
  // resize：重报拖拽区 + tab 滑块按当前激活 tab 重定位（窗口宽窄变化会改变 tab 的
  // offsetLeft/offsetWidth，不重算滑块会留在旧位置造成视觉错位）
  window.addEventListener('resize', () => {
    reportDragExcludes();
    moveTabSlider(TAB_ORDER[_curTabIdx]);
  });

  // ---------- 自绘窗口控制按钮 ----------
  // 点击 → C# win-action（最小化/最大化/关闭，postWindowAction 在 js/rpc.js）；
  // C# 推送 maximized → 切换图标与无障碍文案
  function initWindowControls() {
    $('btn-win-min').addEventListener('click', () => postWindowAction('minimize'));
    $('btn-win-max').addEventListener('click', () => postWindowAction('maximize'));
    $('btn-win-close').addEventListener('click', () => postWindowAction('close'));
    maarm.onNativeMessage((msg) => {
      // PEEP 悬浮窗开/关：预览卡与占位符互换，帧消费权交接
      if (msg.type === 'peep-floating') {
        setPreviewFloating(Boolean(msg.value));
        return;
      }
      if (msg.type !== 'maximized') return;
      const btn = $('btn-win-max');
      if (!btn) return;
      const maxed = Boolean(msg.value);
      btn.classList.toggle('win-btn--maximized', maxed);
      btn.setAttribute('aria-label', maxed ? '还原' : '最大化');
      btn.setAttribute('title', maxed ? '还原' : '最大化');
    });
  }
  initWindowControls();

  // ---------- 主控 ----------
  async function init() {
    try {
      const data = await maarm.call('get_initial_state');
      $('app-version').textContent = 'v' + data.version;
      $('about-version').textContent = 'v' + data.version;
      const verCur = $('about-ver-current');
      if (verCur) verCur.textContent = 'v' + data.version;
      state.modules = Array.isArray(data.modules) ? data.modules : [];
      state.queue = data.queue || { order: [], enabled: {} };
      state.breakpoints = data.breakpoints || {};
      state.moduleAssets = data.module_assets || {};
      state.coverDefault = data.cover_default || '';
      // 初始就按运行状态锁定（重连/刷新时可能已有轮次在跑）
      MaaRM.configPanel.setLocked(!!data.is_running);
      renderTaskList();
      // 「数据/设置」页跟随初始面板：第一个启用项，否则列表第一项，否则注册表第一个
      const initialMid = state.queue.order.find((m) => state.queue.enabled[m])
        || state.queue.order[0]
        || (state.modules[0] && state.modules[0].id) || '';
      renderModulePages(initialMid);
      if (!data.model_ok) setStatus('插件资源缺失', 'error');
      _initOk = true;
    } catch (e) {
      console.error(e);
      reportError('toast', '初始化失败: ' + e.message);
    }
    setTimeout(pollStatus, 250);
    setTimeout(pollLogs, 300);
    setInterval(pollDashboard, 3000); // 看板中心（仅数据页可见时刷新当前看板）
    peepConsumer.start(500);           // PEEP 预览轮询（peep 开启 + 数据页可见 + 未脱离悬浮窗时 ~10fps）
  }

  // ---------- 首次启动：界面预热（只有冷启动会看到遮罩） ----------
  // GPU 光栅下，首访的绘制组合要现场编译着色器（100–250ms 的帧停顿），Chromium 不提供
  // 全量预编译或「待编译清单」接口，只能把界面真实画一遍。冷启动时遮罩盖住界面，
  // 在遮罩后逐个打开页面与弹窗、等异步内容渲染完再关闭，把编译集中到遮罩期间。
  // 热启动由 index.html 头部脚本直接隐藏遮罩，这里直接返回、不产生任何等待。
  // 进度是「预热步骤」的完成度，不是着色器条数。
  // 预热面由各功能在自己的模块里登记（window.MaaRM.registerWarmup），这里只按 order 跑一遍。
  // 漏登记只影响该弹窗首次打开的流畅度。
  const BOOT_MIN_MS = 1200;   // 保底时长：预热再快也让遮罩停留一会儿，不闪一下
  const BOOT_FILL = 0.92;     // 步骤走完先到 92%，保底时长结束时再补满
  // 总时限：从脚本开始执行算起，到必须释放界面的上限。正常冷启动约 3–4 秒；
  // 超时只保证界面可用，本次不写完成标记（下次冷启动重试预热）。
  const BOOT_TOTAL_MS = 12000;
  const BOOT_T0 = performance.now();
  // 遮罩已经放过（正常收尾或时限兜底）。热启动根本没有遮罩（首帧就 boot-skip）：
  // 必须从一开始就算放行，否则切页与其他「放行后才该发生」的事情全被挡住。
  let _bootReleased = document.documentElement.classList.contains('boot-skip');
  let _bootCutShort = false;   // 时限兜底先放行了界面：本次预热不算完成
  let _bootFinalized = false;  // 收尾只许发生一次：时限兜底与正常收尾不得各跑一遍
  let _initOk = false;         // 初始化是否成功：失败时预热没有意义，也不得写完成标记
  // 遮罩期间的键盘隔离：遮罩挡得住鼠标，拦不住 Tab——焦点会落到被遮住的「开始」等
  // 按钮上，回车就把任务启动了。inert 把顶栏/正文/底栏一起移出焦点序列，收尾再放开。
  function setShellInert(on) {
    [document.getElementById('titlebar'), document.querySelector('main'), document.querySelector('footer')]
      .forEach((el) => { if (el) el.inert = on; });
  }
  // 热启动没有遮罩，不该禁任何东西
  if (!_bootReleased) setShellInert(true);
  // 预热面在收到回包后才开窗的，开窗前问一句：遮罩若已被时限兜底放行，
  // 迟到的回包不该再弹一张自己会关的窗到用户眼前抢焦点。
  window.MaaRM.warmupReleased = () => _bootReleased;

  function showOnlyPage(name) {
    TAB_ORDER.forEach((p) => {
      $('page-' + p).classList.toggle('hidden', p !== name);
    });
  }

  // 给单步套上总时限：到点即拒绝，避免某一步的等待（如看板查询）拖住遮罩
  function withDeadline(promise, deadline) {
    const remain = deadline - performance.now();
    if (remain <= 0) return Promise.reject(new Error('超出启动时限'));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('超出启动时限')), remain);
      promise.then(
        (v) => { clearTimeout(timer); resolve(v); },
        (e) => { clearTimeout(timer); reject(e); },
      );
    });
  }

  // 收尾只发生一次：正常走完与时限兜底是两条路径，谁先到谁收尾，另一条直接退出——
  // 否则晚到的初始化会再开一张启动体检，还会把用户已经打开的弹窗顺手收掉。
  function finishBoot(completed) {
    if (_bootFinalized) return;
    _bootFinalized = true;
    _bootReleased = true;
    // 遮罩撤下时不该还挂着预热开的窗：收掉它，别让弹窗在已经放行的界面上抢焦点
    if (window.MaaRM.closeAllModals) window.MaaRM.closeAllModals();
    // 启动体检放在遮罩撤下之后：它开的是给用户看的弹窗，埋在遮罩下面既看不见，
    // 也会被上面的 closeAllModals 顺手收掉
    checkRegistryOptimizations();
    setShellInert(false); // 界面已可用：焦点序列恢复
    const overlay = $('boot-overlay');
    if (!overlay) return;
    overlay.classList.add('boot-overlay--done');
    setTimeout(() => { overlay.hidden = true; }, 260);
    // 只有全部步骤都画完才通知壳落标记：失败、初始化失败或超时留在冷态，下次启动重来
    if (completed && window.chrome && window.chrome.webview) {
      window.chrome.webview.postMessage({ type: 'warmup-done' });
    } else if (!completed) {
      console.warn('[warmup] 本次预热未完成，不写完成标记');
    }
  }

  // 独立的时限兜底：即使初始化请求挂住、预热根本没开始，到点也必须放出界面。
  // 只放行界面，不写完成标记——没画完的预热下次启动还要重来。
  function armBootDeadline() {
    const overlay = $('boot-overlay');
    if (!overlay || document.documentElement.classList.contains('boot-skip')) return;
    setTimeout(() => {
      if (_bootReleased) return;
      _bootCutShort = true;
      const stepEl = overlay.querySelector('.boot-step');
      if (stepEl) stepEl.textContent = '启动较慢，先进入界面';
      console.warn('[warmup] 超出启动时限，先释放界面；本次不写完成标记');
      finishBoot(false);
    }, BOOT_TOTAL_MS);
  }

  async function runBootWarmup() {
    const overlay = $('boot-overlay');
    if (!overlay || document.documentElement.classList.contains('boot-skip')) {
      checkRegistryOptimizations(); // 热启动没有遮罩：启动体检现在就跑
      return;
    }
    // 时限兜底已经收过尾（初始化回来得太晚）：不再翻页、开弹窗或重开体检
    if (_bootFinalized) return;
    // 初始化失败：界面数据没到位，预热没有意义，也不能算画完——放出界面，下次重来
    if (!_initOk) {
      finishBoot(false);
      return;
    }
    const bar = overlay.querySelector('.boot-bar i');
    const stepEl = overlay.querySelector('.boot-step');
    const started = performance.now();
    const deadline = BOOT_T0 + BOOT_TOTAL_MS;
    let done = 0;
    const steps = [
      ['主控页', async () => { showOnlyPage('control'); await twoFrames(); return true; }],
      ['数据页', async () => {
        showOnlyPage('data');
        await twoFrames();
        // 看板内容只在页面可见时拉取（见 loadDashboard），这里等它真正画出来再走，
        // 否则冷启动后第一次点数据页仍会为看板内容付一次首访编译。
        // 回包失败（false）算本次预热未完成；没有候选看板（null）不算失败。
        const drawn = await pollDashboard();
        await twoFrames();
        await delay(80);
        return drawn !== false;
      }],
      ['设置页', async () => { showOnlyPage('settings'); await twoFrames(); await delay(80); return true; }],
      ['关于页', async () => { showOnlyPage('about'); await twoFrames(); await delay(80); return true; }],
      ...window.MaaRM.warmupSurfaces
        .slice()
        .sort((a, b) => (a.order || 0) - (b.order || 0))
        .map((s) => [s.label, () => s.run()]),
    ];
    const total = steps.length;
    const advance = (label) => {
      done += 1;
      bar.style.width = (done / total * BOOT_FILL * 100).toFixed(1) + '%';
      stepEl.textContent = label + '（' + done + ' / ' + total + '）';
    };
    // 任何一步抛出、返回 false（尝试了但没画出来）或超出总时限，都算本次预热未完成：
    // 界面照常释放，但不写完成标记，下次冷启动重试。
    let ok = true;
    for (const [label, run] of steps) {
      // 遮罩已被时限兜底放行、或已到总时限：不再启动新的一步。
      // 否则同步开窗的预热面会在用户已经看到界面时再闪一张弹窗。
      if (_bootReleased || performance.now() >= deadline) {
        ok = false;
        console.warn('[warmup] 遮罩已释放或超出总时限，跳过剩余步骤');
        break;
      }
      try {
        const ready = await withDeadline(run(), deadline);
        if (ready === false) ok = false;
      } catch (e) {
        // 单步失败不阻断后续预热与收尾（遮罩不能因此卡住）
        ok = false;
        console.error('[warmup]', e);
      }
      advance(label);
    }
    // 兜底已经收过尾：下面的翻页、文案与收尾一律不动，别打断用户刚接手的界面
    if (_bootFinalized) return;
    if (_bootCutShort) ok = false;
    showOnlyPage('control'); // 预热完回到启动时的主控页
    await twoFrames();
    const remain = BOOT_MIN_MS - (performance.now() - started);
    if (remain > 0) await delay(remain);
    // 等待期间可能正好撞上时限兜底：它已经收尾并放行，这里不能再改文案/再收一遍
    if (_bootFinalized) return;
    bar.style.width = '100%';
    stepEl.textContent = ok ? '完成' : '未完成，下次启动再试';
    await delay(200);
    finishBoot(ok && !_bootCutShort);
  }

  // ---------- 启动 ----------
  armBootDeadline(); // 独立兜底：初始化请求挂住也要按时放出界面（且不写完成标记）
  init().then(runBootWarmup, runBootWarmup);
})();
