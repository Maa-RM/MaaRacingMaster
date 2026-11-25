// MaaRM shell 前端 —— 关于页「版本与更新 / 公告」。
// 从 app.js 拆出：更新卡与公告的渲染、交互、宿主快照订阅都在这里；入口只留装配。
//
// 更新卡的版式：一行说清「现在怎样 + 能做什么」——图标 + 标题 + 一句说明 + 右侧动作；
// 附加内容（下载进度 / 稍后 / 提醒勾选）只在需要时出现在标题下方的 detail 区。相位到
// 视图的映射集中在 describe()，handler 只负责调 RPC 与渲染。动作分主次：推进流程的
// （下载 / 重启）是主按钮，检查 / 前往下载是次按钮。
//
// 远程元数据按不可信输入处理：文本走 textContent / DOM API，外链只报逻辑目标名。
// 共享物（maarm / state / $ / showError / mkEl）经 window.MaaRM 取用。
(function () {
  'use strict';

  const { maarm, state, $, showError, mkEl } = window.MaaRM;

  // ---------- 图标 ----------
  // icons.js 里没有的字形在这里内联（静态字面量，不接任何远程数据）。
  const INLINE_ICON = {
    download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
      + ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
      + '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M4 20h16"/></svg>',
    restart: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
      + ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
      + '<path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1"/><path d="M3 4.5v5h5"/></svg>',
    alert: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
      + ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
      + '<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5"/><path d="M12 16.4v.2"/></svg>',
    check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
      + ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
      + '<path d="m4.5 12.5 4.5 4.5L19.5 6.5"/></svg>',
  };

  function paintIcon(el, name) {
    if (!el) return;
    el.textContent = '';
    if (!name) return;
    if (name === 'spinner') { // 进行中：用页面已有的转圈样式，不占用图标表
      el.appendChild(mkEl('span', 'spinner'));
      return;
    }
    if (INLINE_ICON[name]) {
      el.innerHTML = INLINE_ICON[name];
      return;
    }
    // icons.js 的出口是 SVG 字符串（node() 返回的是变形动画用的节点数据，不是 DOM）
    if (window.MaaRMIcons && window.MaaRMIcons.svg) {
      try {
        const tpl = document.createElement('template');
        tpl.innerHTML = window.MaaRMIcons.svg(name);
        el.appendChild(tpl.content.firstElementChild);
      } catch (e) {
        // 未知图标：留空，不把异常抛到渲染路径上
      }
    }
  }

  // ---------- 更新设置（渠道 / 下载源 / 自动检查） ----------
  // 渠道/下载源的真源是 update_status 快照（偏好由壳存用户数据根、随升级保留）；
  // 前端只在点击后即时高亮，收到快照即以快照校正。
  let updateSource = 'cnb';
  let updateChannel = 'stable';
  let autoCheckEnabled = true;

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
    const autoCheck = $('upd-auto-check');
    if (autoCheck) autoCheck.checked = autoCheckEnabled;
  }

  // 浮窗键盘可达：焦点进出与 Tab 环绕
  const FOCUSABLE = 'button:not(:disabled), input:not(:disabled), [tabindex]:not([tabindex="-1"])';
  function focusablesIn(root) {
    return Array.prototype.filter.call(
      root.querySelectorAll(FOCUSABLE), (el) => el.offsetParent !== null);
  }

  function closeUpdateSettings() {
    const pop = $('update-settings-popover');
    const trigger = $('btn-update-settings');
    if (!pop) return;
    const focusInside = pop.contains(document.activeElement);
    pop.hidden = true;
    pop.removeAttribute('aria-modal');
    if (trigger) {
      trigger.setAttribute('aria-expanded', 'false');
      if (focusInside) trigger.focus(); // 关窗把焦点还给入口，不丢到 body 上
    }
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
    pop.setAttribute('aria-modal', 'true');
    trigger.setAttribute('aria-expanded', 'true');
    if (focusablesIn(pop)[0]) focusablesIn(pop)[0].focus();
  }

  async function setUpdateSource(source) {
    const value = source === 'github' ? 'github' : 'cnb';
    updateSource = value;
    renderUpdateSettings();
    closeUpdateSettings();
    try {
      const d = await maarm.call('update_set_source', { value });
      renderSnapshot(d);
      if (d && d.error) showError(d.error); // 临界区/非受管被拒：别让设置看起来生效了
    } catch (e) {
      showError('切换下载源失败：' + e.message);
      await refreshUpdateStatus();
    }
  }

  async function setUpdateChannel(channel) {
    const value = channel === 'preview' ? 'preview' : 'stable';
    updateChannel = value;
    renderUpdateSettings();
    closeUpdateSettings();
    try {
      const d = await maarm.call('update_set_channel', { value });
      renderSnapshot(d);
      if (d && d.error) showError(d.error);
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

  // ---------- 卡片：元素 ----------
  function els() {
    return {
      gear: $('btn-update-settings'),
      action: $('btn-update-action'),
      label: $('btn-update-action-label'),
      actionIcon: $('btn-update-action-icon'),
      icon: $('upd-icon'),
      text: $('upd-text'),
      title: $('upd-title'),
      meta: $('upd-meta'),
      detail: $('upd-detail'),
      tabDot: $('about-tab-dot'),
    };
  }

  // ---------- 卡片：相位 → 视图 ----------
  // 一张表决定「图标 / 标题 / 说明 / 主动作 / 附加区」，渲染只负责把表搬上屏。
  // 文案里的版本号一律来自快照，不在此处拼路径或地址。

  function currentVersion() {
    // 版本真源是 get_initial_state 的 version（app.js 落到共享 state），不从 DOM 里捞
    const version = state && state.version;
    return version ? 'v' + version : '';
  }

  function channelText(snapshot) {
    const channel = snapshot.channel === 'preview' ? '测试版' : '稳定版';
    const source = snapshot.source === 'github' ? 'GitHub' : 'CNB';
    return channel + ' · ' + source;
  }

  function describe(snapshot) {
    const target = snapshot.target_version ? ' v' + snapshot.target_version : '';
    switch (snapshot.phase) {
      case 'unavailable':
        return {
          icon: 'alert', tone: 'err',
          title: '自动升级当前不可用',
          meta: snapshot.error || '非受管安装，请使用手动下载入口',
          action: { label: '前往下载', icon: 'arrow-up-right', run: () => openTarget('download') },
        };
      case 'idle':
        if (snapshot.error) {
          // 非受管安装 / 来源未配置：自动升级这条路本来就不通，给手动入口而不是
          // 「重新检查」——后者只会把同一句错误再刷一遍，玩家找不到出口。
          const manual = snapshot.managed === false || /未配置/.test(snapshot.error);
          return manual
            ? {
                icon: 'alert', tone: 'err', title: '自动升级当前不可用', meta: snapshot.error,
                action: { label: '前往下载', icon: 'arrow-up-right', run: () => openTarget('download') },
              }
            : {
                icon: 'alert', tone: 'err', title: '检查更新失败', meta: snapshot.error,
                action: { label: '重新检查', icon: 'rotate-cw', run: checkUpdate },
              };
        }
        const version = currentVersion();
        return {
          icon: null, title: version ? '当前版本 ' + version : '当前版本',
          meta: channelText(snapshot),
          action: { label: '检查更新', icon: 'rotate-cw', run: checkUpdate },
        };
      case 'checking':
        return {
          icon: 'spinner', title: '正在检查更新…', meta: channelText(snapshot),
          action: { label: '检查中…', icon: 'rotate-cw', disabled: true },
        };
      case 'up_to_date':
        if (snapshot.awaiting_stable) {
          return {
            icon: null, title: '当前版本高于稳定渠道',
            meta: '等待下一稳定版，无需操作',
            action: { label: '检查更新', icon: 'rotate-cw', run: checkUpdate },
          };
        }
        return {
          icon: 'check', tone: 'ok', title: '已是最新版本',
          meta: [currentVersion(), channelText(snapshot)].filter(Boolean).join(' · '),
          action: { label: '检查更新', icon: 'rotate-cw', run: checkUpdate },
        };
      case 'available':
        // error 来自「取消下载 / 下载启动被拒」：宿主仍保留已确认的目标，所以这里
        // 给的是重试入口，而不是把玩家赶回「重新检查」。
        return {
          icon: snapshot.error ? 'alert' : 'download',
          tone: snapshot.error ? 'err' : 'new',
          title: snapshot.error ? '下载没有完成' : '发现新版本' + target,
          meta: snapshot.error || channelText(snapshot),
          detail: 'remind',
          action: { label: snapshot.error ? '重试下载' : '下载', icon: 'download',
                    run: downloadUpdate, primary: true },
        };
      case 'downloading':
        return {
          icon: 'spinner', title: '正在下载' + target, meta: null,
          detail: 'progress',
          action: { label: '下载中…', icon: 'download', disabled: true },
        };
      case 'ready':
        if (snapshot.error) {
          return {
            icon: 'alert', tone: 'err', title: '上一次升级没有成功', meta: snapshot.error,
            action: { label: '重试升级', icon: 'restart', run: applyUpdate, primary: true },
          };
        }
        return {
          icon: 'check', tone: 'ok', title: '已就绪，重启后完成升级',
          meta: [target.replace(' v', 'v'), '应用会关闭几秒'].filter(Boolean).join(' · '),
          detail: 'later',
          action: { label: '重启并升级', icon: 'restart', run: applyUpdate, primary: true },
        };
      case 'preparing':
      case 'applying':
        return {
          icon: 'spinner',
          title: snapshot.phase === 'applying' ? '即将关闭并完成升级…' : '正在准备升级…',
          meta: snapshot.phase === 'applying' ? null : '会先结束正在运行的任务',
          action: { label: snapshot.phase === 'applying' ? '正在升级…' : '准备中…', icon: 'restart', disabled: true },
        };
      default:
        return {
          icon: 'alert', tone: 'err', title: '状态未知',
          meta: snapshot.error || '请重新检查更新',
          action: { label: '检查更新', icon: 'rotate-cw', run: checkUpdate },
        };
    }
  }

  // ---------- 卡片：渲染 ----------
  let lastUpdateRevision = -1;
  let lastSnapshot = null; // 最近一次落地快照：版本号晚到时用它重画一次
  let lastPhase = null;
  let updatePollTimer = null;

  function setAction(view) {
    const { action, label, actionIcon } = els();
    if (!action || !label) return;
    const spec = view.action || {};
    label.textContent = spec.label || '';
    action.onclick = spec.run || null;
    action.disabled = !!spec.disabled || !spec.run;
    action.classList.toggle('maarm-btn--primary', spec.primary === true);
    if (actionIcon) paintIcon(actionIcon, spec.icon);
  }

  function renderDetail(kind, snapshot) {
    const detail = els().detail;
    if (!detail) return;
    // 下载进度就地更新：轮询每 500ms 一拍，重建容器会把「取消」从焦点里换掉
    //（Keyboard 用户 Tab 过去半秒就掉焦点，按住按钮也会因节点被换而不触发 click）
    if (kind === 'progress' && detail.dataset.kind === 'progress') {
      const track = detail.querySelector('[role="progressbar"]');
      const bar = detail.querySelector('.update-progress-bar');
      const num = detail.querySelector('.upd-progress-num');
      if (track) track.setAttribute('aria-valuenow', String(snapshot.progress));
      if (bar) bar.style.width = snapshot.progress + '%';
      if (num) num.textContent = snapshot.progress + '%';
      detail.hidden = false;
      return;
    }
    // 勾选「本次更新不再提示」会回一张新快照：同一目标原地更新勾选与说明，不重建节点——
    // 重建会把键盘焦点从复选框换到 body 上（下载进度的「取消」同理由）。
    if (kind === 'remind' && detail.dataset.kind === 'remind'
        && detail.dataset.target === String(snapshot.target_version)) {
      const dismissed = snapshot.dismissed_version === snapshot.target_version;
      const box = detail.querySelector('input[type="checkbox"]');
      if (box) box.checked = dismissed;
      const hint = detail.querySelector('.upd-remind-hint');
      if (hint) {
        hint.textContent = dismissed
          ? '关于页的提醒点已隐藏，下个版本会重新提示'
          : '仅对 v' + snapshot.target_version + ' 生效';
      }
      detail.hidden = false;
      return;
    }
    detail.dataset.kind = kind || '';
    detail.dataset.target = kind === 'remind' ? String(snapshot.target_version) : '';
    detail.textContent = '';
    detail.hidden = !kind;
    if (kind === 'progress') {
      const row = mkEl('div', 'upd-progress');
      const track = mkEl('div', 'update-progress-track');
      track.setAttribute('role', 'progressbar');
      track.setAttribute('aria-label', '更新下载进度');
      track.setAttribute('aria-valuemin', '0');
      track.setAttribute('aria-valuemax', '100');
      track.setAttribute('aria-valuenow', String(snapshot.progress));
      const bar = mkEl('div', 'update-progress-bar');
      bar.style.width = snapshot.progress + '%';
      track.appendChild(bar);
      row.appendChild(track);
      row.appendChild(mkEl('span', 'upd-progress-num', snapshot.progress + '%'));
      const cancel = mkEl('button', 'upd-link', '取消');
      cancel.type = 'button';
      cancel.addEventListener('click', cancelDownload);
      row.appendChild(cancel);
      detail.appendChild(row);
    } else if (kind === 'later') {
      const later = mkEl('button', 'upd-link', '稍后');
      later.type = 'button';
      later.addEventListener('click', () => {
        // 稍后：保留下载结果，收起附加区；重开关于页可再次恢复
        const { detail: box, title, meta } = els();
        if (box) { box.textContent = ''; box.hidden = true; }
        if (title) title.textContent = '已就绪，重启后完成升级';
        if (meta) meta.textContent = '可在关于页随时重启升级';
      });
      detail.appendChild(later);
    } else if (kind === 'remind') {
      const target = snapshot.target_version;
      const dismissed = snapshot.dismissed_version === target;
      const row = mkEl('label', 'upd-remind');
      const box = mkEl('input');
      box.type = 'checkbox';
      box.checked = dismissed;
      box.addEventListener('change', () => setDismissed(box.checked));
      row.appendChild(box);
      row.appendChild(mkEl('span', 'upd-remind-text', '本次更新不再提示'));
      row.appendChild(mkEl('span', 'upd-remind-hint',
        dismissed ? '关于页的提醒点已隐藏，下个版本会重新提示' : '仅对 v' + target + ' 生效'));
      detail.appendChild(row);
    }
  }

  function renderSnapshot(snapshot) {
    if (!snapshot || typeof snapshot.revision !== 'number') return;
    if (snapshot.revision < lastUpdateRevision) return; // 修订守卫：旧响应不回退进度
    lastUpdateRevision = snapshot.revision;
    lastSnapshot = snapshot;
    applySettingsSnapshot(snapshot);
    const { title, meta, icon, text, gear } = els();
    if (!title || !meta || !text) return;

    const phase = snapshot.phase;
    const critical = phase === 'preparing' || phase === 'applying';

    if (phase !== 'downloading') stopUpdatePolling();

    // 只有相位切换才播报：下载的 500ms 轮询只改百分比，读屏每拍念一遍等于刷屏。
    text.setAttribute('aria-live', phase === lastPhase ? 'off' : 'polite');
    lastPhase = phase;

    // 提交临界区不许改设置：与其让用户白点一次再被拒，不如先关掉入口
    if (critical) closeUpdateSettings();
    if (gear) {
      gear.disabled = critical;
      gear.title = critical ? '升级提交中，不能切换设置' : '更新设置';
    }

    const view = describe(snapshot);
    paintIcon(icon, view.icon);
    if (icon) icon.className = 'upd-icon' + (view.tone ? ' upd-icon--' + view.tone : '');
    title.textContent = view.title;
    title.className = 'upd-title' + (view.tone === 'err' ? ' upd-title--err' : '');
    meta.textContent = view.meta || '';
    meta.className = 'upd-meta' + (view.tone === 'err' ? ' upd-meta--err' : '');
    meta.hidden = !view.meta;

    setAction(view);
    renderDetail(view.detail, snapshot);
    renderUpdateDot(snapshot);

    if (phase === 'downloading') startUpdatePolling();
  }

  async function refreshUpdateStatus() {
    // 关于页初始化/重开时恢复快照（下载任务在壳内跨页面持续）
    try {
      renderSnapshot(await maarm.call('update_status'));
    } catch (e) {
      // 协调器不可用（极端情况）：保留初始的空态，主按钮仍是「检查更新」
    }
  }

  // RPC 直接失败（不是快照里的 error）：把这一条写在说明行上，标题保持中性
  function paintFailure(message) {
    const { title, meta, icon } = els();
    if (title) title.textContent = '操作没有完成';
    if (meta) {
      meta.textContent = message;
      meta.className = 'upd-meta upd-meta--err';
      meta.hidden = false;
    }
    if (icon) {
      icon.className = 'upd-icon upd-icon--err';
      paintIcon(icon, 'alert');
    }
  }

  // ---------- 轮询（下载进度） ----------
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
        renderSnapshot(await maarm.call('update_status'));
      } catch (e) {
        // 状态轮询失败不打断下载任务本身，下一拍重试
      }
    }, 500);
  }

  // ---------- 动作 ----------
  async function checkUpdate() {
    closeUpdateSettings();
    const { title, meta, icon } = els();
    if (title) title.textContent = '正在检查更新…';
    if (meta) meta.hidden = true;
    if (icon) { icon.className = 'upd-icon'; paintIcon(icon, 'spinner'); }
    setAction({ action: { label: '检查中…', icon: 'rotate-cw', disabled: true } });
    renderDetail(null);
    try {
      renderSnapshot(await maarm.call('update_check'));
    } catch (e) {
      console.error(e);
      await refreshUpdateStatus();
      paintFailure('检查失败：' + e.message);
    }
    fetchAnnouncement({ force: true }); // 手动检查收尾：公告与版本一起刷新
  }

  async function downloadUpdate() {
    try {
      // 带上玩家看到目标时的修订号：后台已换目标则后端拒绝，不替旧界面下载新版本
      renderSnapshot(await maarm.call('update_start_download', { revision: lastUpdateRevision }));
    } catch (e) {
      console.error(e);
      paintFailure('下载启动失败：' + e.message);
    }
  }

  async function cancelDownload() {
    try {
      renderSnapshot(await maarm.call('update_cancel_download'));
    } catch (e) {
      console.error(e);
      paintFailure('取消失败：' + e.message);
    }
  }

  async function setDismissed(value) {
    try {
      // 同下载：勾选只对玩家看到的目标版本生效，快照过期就由后端拒绝
      renderSnapshot(await maarm.call('update_set_dismissed', { value, revision: lastUpdateRevision }));
    } catch (e) {
      showError('更新提醒设置失败：' + e.message);
      await refreshUpdateStatus();
    }
  }

  async function applyUpdate() {
    try {
      const d = await maarm.call('update_apply');
      if (d.phase === 'preparing' || d.phase === 'applying') {
        // 提交临界区：壳接管收尾与计划内关窗，界面只如实显示，不提供取消
        renderSnapshot(d);
        return;
      }
      renderSnapshot(d); // 闸门拒绝：状态回 ready，原因与重启入口都保留
    } catch (e) {
      console.error(e);
      paintFailure('提交失败：' + e.message);
    }
  }

  // ---------- 公告 ----------
  // 刷新时机：启动拉一次，之后每次检查收尾（手动检查、后台自动检查推送）再拉一次；
  // 切进关于页也会补一次（同受下面的最小间隔约束）——普通 ZIP 没有后台检查，启动时
  // 网络不通就再没有触发点，切页是它唯一的重试入口。内容与已落地的一致时不重建 DOM。
  const ANNOUNCE_MIN_GAP_MS = 60 * 1000;
  const ANNOUNCE_RETRY_GAP_MS = 5 * 1000; // 上次失败的补拉间隔：进入关于页时可尽快重试
  let announceFetchedAt = 0;
  let announceFailed = false;
  let announceInFlight = null;
  let announceShown = null;

  function announceSignature(d) {
    if (!d || d.level === 'none' || !d.title) return 'none';
    return JSON.stringify([d.level, d.title, d.date, d.body, d.url, d.url_text]);
  }

  function renderAnnouncement(bodyEl, d) {
    const frag = document.createDocumentFragment();
    if (announceSignature(d) === 'none') {
      frag.appendChild(mkEl('div', 'about-announce-empty', '暂无公告'));
    } else {
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
      frag.appendChild(card);
    }
    bodyEl.textContent = '';
    bodyEl.appendChild(frag);
  }

  // opts.force：检查更新收尾的主动刷新，不受间隔约束；其余调用在间隔内直接跳过。
  async function fetchAnnouncement(opts) {
    const bodyEl = $('about-announce-body');
    if (!bodyEl) return;
    if (announceInFlight) return announceInFlight;
    const gap = announceFailed ? ANNOUNCE_RETRY_GAP_MS : ANNOUNCE_MIN_GAP_MS;
    if (!(opts && opts.force) && Date.now() - announceFetchedAt < gap) return;
    announceFetchedAt = Date.now();
    announceInFlight = (async () => {
      try {
        const d = await maarm.call('fetch_announcement');
        announceFailed = false;
        const sig = announceSignature(d);
        if (sig === announceShown) return;
        announceShown = sig;
        renderAnnouncement(bodyEl, d);
      } catch (e) {
        announceFailed = true;
        console.error(e);
        if (announceShown === null) {
          announceShown = 'none';
          renderAnnouncement(bodyEl, null);
        }
      } finally {
        announceInFlight = null;
      }
    })();
    return announceInFlight;
  }

  // 关于标签上的更新提醒点：只在「发现可更新目标且本次未选择不再提示」时亮起。
  function renderUpdateDot(snapshot) {
    const { tabDot } = els();
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

  // ---------- 装配 ----------
  maarm.onNativeMessage((msg) => {
    if (!msg || msg.type !== 'update-status') return;
    renderSnapshot(msg.data);
    fetchAnnouncement(); // 后台检查收尾：公告顺带刷到最新（受最小间隔约束，不叠加请求）
  });

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
      // Tab 留在浮窗内：首尾环绕，别让焦点穿到卡片背后的按钮上
      pop.addEventListener('keydown', (event) => {
        if (event.key !== 'Tab') return;
        const items = focusablesIn(pop);
        if (!items.length) return;
        const first = items[0];
        const last = items[items.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      });
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
    const autoCheck = $('upd-auto-check');
    if (autoCheck) {
      autoCheck.addEventListener('change', async () => {
        const value = autoCheck.checked;
        try {
          // 开关回包是快照：经同一渲染入口回显，而不是只改本地勾选
          renderSnapshot(await maarm.call('update_set_auto_check', { value }));
        } catch (e) {
          autoCheck.checked = !value;
          showError('自动检查设置失败：' + e.message);
        }
      });
    }
    refreshUpdateStatus();
    fetchAnnouncement();
  }

  // 版本号在 get_initial_state 之后才到，可能晚于 update_status：app.js 落到共享 state 后
  // 调这里重画一次，避免标题栏已有版本、卡片却停在「当前版本」。
  window.MaaRM.refreshUpdateCard = () => {
    if (lastSnapshot) renderSnapshot(lastSnapshot);
  };
  // 切进关于页补拉公告的入口（间隔约束在 fetchAnnouncement 内）
  window.MaaRM.fetchAnnouncement = fetchAnnouncement;

  initAbout();
})();
