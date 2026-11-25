// MaaRM shell 前端 —— 环境中心与注册表权限体检。
// 从 settings.js 拆出：启动体检、权限优化 / 可选依赖 / 运行环境三个页签及其弹窗。
// 共享物（maarm / showError / openModal / escHtml）经 window.MaaRM 取用。
(function () {
  'use strict';

  const { maarm, showError, openModal, escHtml, waitFor, twoFrames, delay } = window.MaaRM;

  // ---------- 注册表权限优化（启动体检 + 环境中心「权限优化」tab） ----------
  // 渲染单个优化项（三态行式卡片）：available===false→无需处理；optimized→已优化；否则待优化。
  // 状态 pill 与操作按钮同处底部一行紧邻；值/路径/后果等技术细节收进 <details> 默认折叠。
  // 样式统一走 style.css 的 .opt-* 类（复用设计令牌，不再堆内联样式）。
  function optimizerItemHtml(it) {
    const na = it.available === false;
    const state = na ? 'na' : (it.optimized ? 'done' : 'todo');
    const pillText = na ? '无需处理' : (it.optimized ? '已优化' : '待优化');
    const optsText = it.options
      ? Object.keys(it.options).map((k) => k + ' = ' + it.options[k]).join(' · ')
      : '';
    const currentTxt = (it.current === null || it.current === undefined)
      ? '未设置（系统默认）'
      : (it.options && it.options[String(it.current)]
          ? it.options[String(it.current)] + '（值 ' + it.current + '）'
          : String(it.current));
    // protocol_command 的动作落在子键 shell\open\command（+ NoOpenWith 标记），路径行按 kind 拼装
    const pathLines = (it.paths && it.paths.length ? it.paths : [it.path])
      .map((p) => it.kind === 'protocol_command'
        ? it.hive + '\\' + p + '\\shell\\open\\command（默认值 = 空）<br>'
          + it.hive + '\\' + p + '\\' + it.value_name + '（标记值）'
        : it.hive + '\\' + p + '\\' + it.value_name)
      .join('<br>');
    let actions = '';
    if (!na) {
      if (!it.optimized) {
        actions += '<button class="opt-btn opt-btn--primary opt-apply" data-id="' + it.id + '">'
          + (it.apply_label || '优化') + '</button>';
      }
      actions += '<button class="opt-btn opt-btn--ghost opt-restore" data-id="' + it.id + '">'
        + (it.restore_label || '恢复默认') + '</button>';
    }
    if (it.prompt_ignored) {
      actions += '<button class="opt-btn opt-btn--ghost opt-unignore" data-id="' + it.id + '">恢复启动提醒</button>';
    }
    const note = (na && it.unavailable_note)
      ? '<p class="opt-item-note">' + it.unavailable_note + '</p>' : '';
    return '<div class="opt-item opt-item--' + state + '">'
      + '<div class="opt-item-title">' + it.name + '</div>'
      + '<div class="opt-item-effect">' + it.effect + '</div>'
      + note
      + '<details class="opt-details"><summary>技术细节</summary><div class="opt-details-body">'
      + '<p>值：<b>' + it.value_name + '</b>　可选值：' + optsText + '　当前：' + currentTxt + '</p>'
      + '<p class="opt-path">' + pathLines + '</p>'
      + '<p>后果：' + it.detail + '</p>'
      + '</div></details>'
      + '<div class="opt-item-foot"><span class="opt-pill opt-pill--' + state + '">' + pillText + '</span>'
      + '<span class="opt-actions">' + actions + '</span></div>'
      + '</div>';
  }

  // 写入单个优化项，返回错误信息（null = 成功）
  async function applyOptimization(id, value) {
    try {
      await maarm.call('set_registry_optimization', { id: id, value: value });
      return null;
    } catch (e) {
      return e.message;
    }
  }

  // 设置单个优化项的启动提醒忽略状态（true=忽略，false=恢复），返回错误信息
  async function applyPromptIgnore(id, ignored) {
    try {
      await maarm.call('set_optimization_prompt_ignored', { id: id, ignored: ignored });
      return null;
    } catch (e) {
      return e.message;
    }
  }

  // ---------- 环境中心：可选依赖 tab ----------
  // 收录判据：缺失不阻断核心功能、降级路径一句话说得清、修复动作可引导；硬前置不进。
  // 条目两类来源：字体由前端自检（document.fonts.check 是渲染层真源）；
  // 驱动由侧车检测（get_optional_dependencies，与启动拦截 VIGEM_BUS_MISSING 同源）。
  // 驱动类只引导到官方发布页，不代装；外链走逻辑目标名白名单（open_external_url）。
  function fontDepItemHtml(ready) {
    const actions = ready
      ? '<span class="opt-item-note" style="margin-top:0">当前界面正在使用该字体</span>'
      : '<button type="button" class="opt-btn opt-btn--primary dep-guide" data-target="noto_font">打开下载页</button>';
    return '<div class="opt-item ' + (ready ? 'opt-item--done' : 'opt-item--todo') + '">'
      + '<div class="opt-item-title">界面字体 Noto Sans SC（思源黑体）</div>'
      + '<div class="opt-item-effect">思源字形：小字号更锐利，500/600 为真实字重，界面文字更清爽。'
      + '未安装时界面回落微软雅黑，功能不受影响。</div>'
      + '<details class="opt-details"><summary>技术细节</summary><div class="opt-details-body">'
      + '<p>检测方式：页面内 document.fonts.check（渲染层真源，当前状态即实探结果）。</p>'
      + '<p>降级路径：字体栈回落 Microsoft YaHei UI。</p>'
      + '<p>后果：仅影响观感；安装到系统后重启本程序生效。</p>'
      + '</div></details>'
      + '<div class="opt-item-foot"><span class="opt-pill opt-pill--' + (ready ? 'done' : 'todo') + '">'
      + (ready ? '已就绪' : '未检测到') + '</span>'
      + '<span class="opt-actions">' + actions + '</span></div>'
      + '</div>';
  }

  function depItemHtml(it) {
    const ready = it.state === 'ready';
    const actions = ready
      ? '<span class="opt-item-note" style="margin-top:0">当前可用</span>'
      : '<button type="button" class="opt-btn opt-btn--primary dep-guide" data-target="' + it.guide_target + '">'
        + (it.guide_label || '打开官方页') + '</button>';
    return '<div class="opt-item ' + (ready ? 'opt-item--done' : 'opt-item--todo') + '">'
      + '<div class="opt-item-title">' + it.name + '</div>'
      + '<div class="opt-item-effect">' + it.effect + '</div>'
      + '<details class="opt-details"><summary>技术细节</summary><div class="opt-details-body">'
      + '<p>降级路径：' + it.degrade + '</p>'
      + '<p class="opt-path">官方页：' + it.official + '</p>'
      + '<p>后果：' + it.detail + '</p>'
      + '</div></details>'
      + '<div class="opt-item-foot"><span class="opt-pill opt-pill--' + (ready ? 'done' : 'todo') + '">'
      + (ready ? '已就绪' : '未检测到') + '</span>'
      + '<span class="opt-actions">' + actions + '</span></div>'
      + '</div>';
  }

  async function renderDepList(depEl) {
    let items;
    try {
      const d = await maarm.call('get_optional_dependencies');
      items = d.items || [];
    } catch (e) {
      depEl.innerHTML = '<p class="maarm-modal-error">读取可选依赖失败: ' + e.message + '</p>';
      return;
    }
    let fontReady = false;
    try { fontReady = document.fonts.check('12px "Noto Sans SC"'); } catch (e) { fontReady = false; }
    const total = items.length + 1;
    const missing = items.filter((it) => it.state !== 'ready').length + (fontReady ? 0 : 1);
    const summary = missing
      ? '<b class="opt-num--todo">' + missing + '</b> 项缺失（可选项） · <b class="opt-num--done">' + (total - missing) + '</b> 项就绪'
      : '<b class="opt-num--done">' + total + '</b> 项可选依赖全部就绪';
    depEl.innerHTML = '<div class="opt-summary"><span class="opt-summary-text">' + summary + '</span></div>'
      + '<div class="dep-note">这里收录的都是可选项：缺失不影响核心功能，只影响体验与可选玩法；'
      + '安装动作只做引导，由你确认后自行完成。</div>'
      + fontDepItemHtml(fontReady)
      + items.map(depItemHtml).join('');
  }

  // ---------- 环境中心：运行环境 tab ----------
  // WebView2 是渲染硬前置（缺失即界面不可用），不放进「可选依赖」；这里只做版本展示
  // 与用户主动触发的更新。下载源与验签都在宿主侧，前端不持有地址。
  function runtimeItemHtml(st, msg) {
    const installed = st && st.installed ? String(st.installed) : '';
    const running = st && st.running ? String(st.running) : '';
    const ready = !!installed;
    const restart = ready && running && running !== installed;
    const state = !ready ? 'na' : (restart ? 'todo' : 'done');
    const pill = !ready ? '读取失败' : (restart ? '待重启' : '已就绪');
    const line = !ready
      ? '无法读取运行时版本。'
      : '已安装 ' + installed
        + (running && running !== installed ? ' · 当前界面仍在用 ' + running : '');
    const lastCheck = st && st.lastCheck ? String(st.lastCheck) : '';
    const ageDays = Number(st && st.checkAgeDays);
    const stale = !!(st && st.channelStale);
    const healthLine = lastCheck
      ? '自动更新上次成功检查：' + lastCheck
        + (Number.isFinite(ageDays) && ageDays >= 0 ? '（' + ageDays + ' 天前）' : '')
      : '自动更新检查时间未知';
    const healthNote = stale
      ? '<p class="opt-item-note" style="color:var(--maarm-warning,#f59e0b)">'
        + '超过 90 天未成功检查，自动更新通道可能已失效；建议在系统设置里修复 Edge。'
        + '</p>'
      : '';
    const note = msg ? '<p class="opt-item-note">' + escHtml(msg) + '</p>' : '';
    const official = '<button type="button" class="opt-btn opt-btn--ghost dep-guide" '
      + 'data-target="webview2">打开官方页</button>';
    // 三种状态给三种动作：待重启只该重启，已就绪只留轻量检查入口，不再常驻红色主按钮
    let actions;
    if (!ready) {
      actions = '<button type="button" class="opt-btn opt-btn--primary" data-wv2="update">'
        + '尝试修复</button>' + official;
    } else if (restart) {
      actions = '<button type="button" class="opt-btn opt-btn--primary" data-wv2="restart">'
        + '重启本程序</button>' + official;
    } else {
      actions = '<span class="opt-item-note" style="margin-top:0">当前界面正在使用该版本</span>'
        + '<button type="button" class="opt-btn opt-btn--ghost" data-wv2="update" '
        + 'title="下载微软官方安装包并安装（约 1.7MB）">更新运行时</button>'
        + official;
    }
    return '<div class="opt-item opt-item--' + state + '">'
      + '<div class="opt-item-title">Edge WebView2 运行时</div>'
      + '<div class="opt-item-effect">本程序界面的渲染引擎。版本过旧会影响新特性与安全修复；'
      + '更新只在你点击后进行，不在后台改动系统。</div>'
      + '<p class="opt-item-note">' + escHtml(line) + '</p>'
      + '<p class="opt-item-note">' + escHtml(healthLine) + '</p>'
      + healthNote
      + note
      + '<details class="opt-details"><summary>技术细节</summary><div class="opt-details-body">'
      + '<p>安装器：微软官方 WebView2 引导程序（约 1.7MB）。下载后先校验微软签名，通过才执行。</p>'
      + '<p>来源：固定为微软官方跳转；最终地址必须落在 microsoft.com 域，否则中止。</p>'
      + '<p>没有单独的轻量版本查询：微软只提供这个安装器，每次都会下载约 1.7MB 并幂等安装；'
      + '已是最新时安装器不会改动系统。</p>'
      + '<p>生效方式：安装完成需重启本程序，当前界面才会换用新版本。</p>'
      + '</div></details>'
      + '<div class="opt-item-foot"><span class="opt-pill opt-pill--' + state + '">' + pill + '</span>'
      + '<span class="opt-actions">' + actions + '</span></div>'
      + '</div>';
  }

  async function renderRuntimeList(el, msg) {
    const head = '<div class="dep-note">这是运行本程序所必需的浏览器内核。'
      + '更新动作只在你点击后执行，不会在后台改动系统。</div>';
    el.innerHTML = head + '<p class="opt-item-note">正在读取…</p>';
    try {
      const st = await maarm.call('webview2_status');
      el.innerHTML = head + runtimeItemHtml(st, msg);
    } catch (e) {
      el.innerHTML = head
        + '<p class="maarm-modal-error">读取 WebView2 状态失败：'
        + escHtml(e && e.message ? e.message : e) + '</p>';
    }
  }

  // 环境中心（设置页入口）：tab「权限优化」（注册表优化，三档汇总 + 一键全部）
  // + tab「可选依赖」（ViGEmBus 驱动 / 界面字体，检测 + 引导）
  // + tab「运行环境」（WebView2 硬前置：版本展示 + 用户主动触发的更新）。
  function openOptimizerCenter() {
    const modal = openModal({
      title: '环境中心',
      maxWidth: 540,
      bodyHtml:
        '<div class="env-tabs">'
        + '<button type="button" class="env-tab env-tab--on" data-pane="pane-perm">权限优化</button>'
        + '<button type="button" class="env-tab" data-pane="pane-dep">可选依赖</button>'
        + '<button type="button" class="env-tab" data-pane="pane-runtime">运行环境</button>'
        + '</div>'
        + '<div class="env-pane env-pane--on" id="pane-perm"><div id="opt-summary"></div><div id="opt-center-list"></div></div>'
        + '<div class="env-pane" id="pane-dep"><div id="dep-center-list"></div></div>'
        + '<div class="env-pane" id="pane-runtime"><div id="runtime-center-list"></div></div>',
      buttons: [{ text: '关闭', primary: true, onClick: (m) => m.close() }],
    });
    const summaryEl = modal.card.querySelector('#opt-summary');
    const listEl = modal.card.querySelector('#opt-center-list');
    const depEl = modal.card.querySelector('#dep-center-list');
    const runtimeEl = modal.card.querySelector('#runtime-center-list');
    // 汇总三档：待优化 / 已优化 / 无需处理。available===false 计入"无需处理"，不混入"已优化"
    function renderSummary(list) {
      const todo = list.filter((it) => it.available !== false && !it.optimized);
      const done = list.filter((it) => it.available !== false && it.optimized).length;
      const na = list.filter((it) => it.available === false).length;
      const parts = [];
      if (todo.length) parts.push('<b class="opt-num--todo">' + todo.length + '</b> 项待优化');
      if (done) parts.push('<b class="opt-num--done">' + done + '</b> 项已优化');
      if (na) parts.push('<b>' + na + '</b> 项无需处理');
      const text = parts.length ? parts.join(' · ') : '全部已优化';
      const btn = todo.length
        ? '<button class="opt-btn opt-btn--primary opt-apply-all">一键优化全部</button>' : '';
      summaryEl.innerHTML = '<div class="opt-summary"><span class="opt-summary-text">'
        + text + '</span>' + btn + '</div>';
    }
    async function refresh() {
      try {
        const d = await maarm.call('get_registry_optimizations');
        const its = d.items || [];
        if (!its.length) {
          summaryEl.innerHTML = '';
          listEl.innerHTML = '<p class="maarm-modal-error">当前系统没有可用的注册表优化项</p>';
          return;
        }
        renderSummary(its);
        listEl.innerHTML = its.map(optimizerItemHtml).join('');
      } catch (e) {
        listEl.innerHTML = '<p class="maarm-modal-error">刷新失败: ' + e.message + '</p>';
      }
    }
    // 事件委托到 modal.card：覆盖 tab 切换、依赖引导、汇总条(一键全部)与列表(单项)，
    // 列表 innerHTML 重渲染不影响委托
    modal.card.addEventListener('click', async (ev) => {
      const tabBtn = ev.target.closest('.env-tab');
      if (tabBtn) {
        modal.card.querySelectorAll('.env-tab').forEach((b) => b.classList.toggle('env-tab--on', b === tabBtn));
        modal.card.querySelectorAll('.env-pane').forEach((p) => p.classList.toggle('env-pane--on', p.id === tabBtn.dataset.pane));
        return;
      }
      const guideBtn = ev.target.closest('.dep-guide');
      if (guideBtn) {
        try {
          await maarm.call('open_external_url', { target: guideBtn.dataset.target });
        } catch (e) {
          showError('打开链接失败: ' + e.message);
        }
        return;
      }
      const wv2Btn = ev.target.closest('[data-wv2]');
      if (wv2Btn) {
        if (wv2Btn.disabled) return;
        const mode = wv2Btn.dataset.wv2;
        wv2Btn.disabled = true;
        if (mode === 'restart') {
          wv2Btn.textContent = '正在重启…';
          try {
            await maarm.call('app_restart', {});
          } catch (e) {
            showError('重启失败：' + (e && e.message ? e.message : e));
            await renderRuntimeList(runtimeEl, '重启未成功，可手动关闭后重新打开本程序。');
          }
          return;
        }
        wv2Btn.textContent = '下载安装中…';
        wv2Btn.title = '正在下载并安装微软官方安装包（约 1.7MB）';
        try {
          const res = await maarm.call('webview2_update', {});
          const after = res && res.after ? String(res.after) : '';
          const msg = res && res.changed
            ? '已更新到 ' + after + '，重启本程序后生效。'
            : '安装程序已完成；版本未变化，说明当前已是最新版本。';
          await renderRuntimeList(runtimeEl, msg);
        } catch (e) {
          showError('更新 WebView2 失败：' + (e && e.message ? e.message : e));
          await renderRuntimeList(runtimeEl, '更新未完成，可稍后重试，或打开官方页面手动安装。');
        }
        return;
      }
      const applyAllBtn = ev.target.closest('.opt-apply-all');
      if (applyAllBtn) {
        applyAllBtn.disabled = true;
        const its = ((await maarm.call('get_registry_optimizations')).items) || [];
        const todo = its.filter((it) => it.available !== false && !it.optimized);
        const fails = [];
        for (const it of todo) {
          const err = await applyOptimization(it.id, it.optimized_value);
          if (err) fails.push(it.name + '：' + err);
        }
        // 成功不播报：列表刷新后各项状态自己会说话；只有失败才需要打扰
        if (fails.length) showError('部分优化失败：' + fails.join('；'));
        await refresh();
        return;
      }
      const applyBtn = ev.target.closest('.opt-apply');
      const restoreBtn = ev.target.closest('.opt-restore');
      const unignoreBtn = ev.target.closest('.opt-unignore');
      const btn = applyBtn || restoreBtn || unignoreBtn;
      if (!btn) return;
      btn.disabled = true;
      let err;
      if (applyBtn) err = await applyOptimization(applyBtn.dataset.id, 0);
      else if (restoreBtn) err = await applyOptimization(restoreBtn.dataset.id, 1);
      else err = await applyPromptIgnore(unignoreBtn.dataset.id, false);
      if (err) showError(err);
      await refresh(); // 无论成败都刷新（失败项状态不变，按钮随重渲染恢复可用）
    });
    refresh();
    renderDepList(depEl);
    renderRuntimeList(runtimeEl);
    return modal; // 预热等调用方据此只关自己打开的这一张
  }

  // 启动体检：存在未优化且未忽略的项则弹一键优化引导（按项忽略，新增优化项不受影响）
  async function checkRegistryOptimizations() {
    let pending;
    try {
      const d = await maarm.call('get_registry_optimizations');
      // available === false = 目标应用已安装、该项无需处理，不进体检提醒
      pending = (d.items || []).filter((it) => !it.optimized && !it.prompt_ignored && it.available !== false);
    } catch (e) {
      console.error('权限体检失败:', e);
      return;
    }
    if (!pending.length) return;
    openModal({
      title: '检测到 ' + pending.length + ' 项系统权限可优化',
      titleColor: 'var(--maarm-warning,#f59e0b)',
      bodyHtml: pending.map((it) =>
        '<p class="maarm-modal-text">'
        + '<b>' + it.name + '</b>：' + it.effect + '</p>').join('')
        + '<p class="maarm-modal-text--sub">'
        + '详情与手动调整见 设置 → 运行环境。</p>',
      buttons: [
        {
          text: '一键优化（推荐）', primary: true,
          onClick: async (modal) => {
            const fails = [];
            for (const it of pending) {
              const err = await applyOptimization(it.id, it.optimized_value);
              if (err) fails.push(it.name + '：' + err);
            }
            if (fails.length) showError('部分优化失败：' + fails.join('；'));
            modal.close();
          }
        },
        {
          // 按项忽略（持久化 profile）：这些项不再弹启动提醒；以后新增的优化项照常提醒
          text: '下次不再提醒',
          onClick: async (modal) => {
            const fails = [];
            for (const it of pending) {
              const err = await applyPromptIgnore(it.id, true);
              if (err) fails.push(it.name + '：' + err);
            }
            // 关闭弹窗即反馈；恢复入口在 设置 → 运行环境 各项的「恢复启动提醒」按钮上
            if (fails.length) showError('部分忽略失败：' + fails.join('；'));
            modal.close();
          }
        },
        { text: '暂不', onClick: (modal) => modal.close() },
      ],
    });
  }

  // 本模块拥有的预热面：环境中心三个页签。占位文案「正在读取…」不算就绪。
  window.MaaRM.registerWarmup({
    label: '环境中心',
    order: 30,
    async run() {
      // 遮罩已被总时限放行：不再开窗。开完再检查会留下一张「用户眼前自己关掉」的窗
      if (window.MaaRM.warmupReleased && window.MaaRM.warmupReleased()) return null;
      const modal = openOptimizerCenter();
      if (!modal) return false;
      try {
        const opened = await waitFor(() => modal.card.querySelector('.env-tabs'), 700);
        if (!opened) return false;
        const paneReady = (el) => {
          const t = el.textContent.trim();
          return t.length > 0 && t.indexOf('正在读取') < 0;
        };
        let allReady = true;
        for (const pane of ['pane-perm', 'pane-dep', 'pane-runtime']) {
          const tab = modal.card.querySelector('.env-tab[data-pane="' + pane + '"]');
          const paneEl = modal.card.querySelector('#' + pane);
          if (!tab || !paneEl) continue;
          tab.click();
          const ready = await waitFor(() => paneReady(paneEl), 1200);
          allReady = allReady && ready;
          await twoFrames();
          await delay(60);
        }
        return allReady;
      } finally {
        modal.close();
      }
    },
  });

  Object.assign(window.MaaRM, { checkRegistryOptimizations, openOptimizerCenter });
})();
