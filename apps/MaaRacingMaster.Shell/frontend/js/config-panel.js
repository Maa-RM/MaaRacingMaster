/* 模块配置面板：按插件声明的 schema 通用渲染（契约见 core/config_ui_contract.py）。
 *
 * 渲染器只认识控件类型与通用规则（显隐、按值换文案、动态候选），不认识任何模块字段；
 * 所有动态文本走 textContent，不拼 HTML。
 *
 * 并发口径（与 config-panel-state.js 配套）：
 * - 每卡 lastRev：比已应用版本旧的响应丢弃（后端每次 set 成功 _rev+1）；
 * - 同卡保存串行，保存中再改合并成 pending，当前请求完成后补发；
 * - 列表重建时 reset() 递增代际，旧回调作废，不写已卸载节点。
 */
(function () {
  'use strict';

  var MaaRM = window.MaaRM;
  var ST = window.MaaRMConfigPanelState;
  var entries = new Map();
  var generation = 0;
  var locked = false;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function panelEl(mid) {
    return document.querySelector('#task-list [data-id="' + mid + '"] [data-options]');
  }

  function signature(cfg) {
    if (!cfg) return '';
    return JSON.stringify({ state: cfg._ui_state || '', ui: cfg._ui || null });
  }

  function sameValue(a, b) {
    return a === b;
  }

  function evalCond(cond, cfg) {
    if (!cond) return true;
    var value = cfg ? cfg[cond.key] : undefined;
    if (cond.op === 'equals') return sameValue(value, cond.value);
    if (cond.op === 'not_equals') return !sameValue(value, cond.value);
    if (cond.op === 'in') return Array.isArray(cond.value) && cond.value.some(function (v) {
      return sameValue(v, value);
    });
    return true;
  }

  function pickVariant(field, cfg) {
    var list = field.variants || [];
    for (var i = 0; i < list.length; i += 1) {
      if (evalCond(list[i].when, cfg)) return list[i];
    }
    return null;
  }

  function choicesFor(field, cfg) {
    if (field.choices) {
      var all = (cfg && cfg._choices) || {};
      return Array.isArray(all[field.choices]) ? all[field.choices] : [];
    }
    return Array.isArray(field.options) ? field.options : [];
  }

  function domId(entry, field, suffix) {
    var mid = String(entry.mid).replace(/[^a-zA-Z0-9_-]/g, '_');
    return 'cfgp-' + mid + '-' + field.key + '-' + suffix;
  }

  // ---------- 字段构建 ----------
  // 宿主统一行结构：标签 / 控件 / 说明。说明独占一行，跨满字段栏；
  // 插件只声明字段语义，行布局与控件尺寸由 style.css 的 .cfg-row 系列决定。

  function rowShell(labelNode, controlNode) {
    var wrap = el('div', 'cfg-row');
    var desc = el('p', 'cfg-row-desc');
    wrap.appendChild(labelNode);
    wrap.appendChild(controlNode);
    wrap.appendChild(desc);
    return { wrap: wrap, desc: desc };
  }

  function buildSwitch(entry, field) {
    var label = el('div', 'cfg-row-label', field.label);
    label.id = domId(entry, field, 'label');
    var control = el('div', 'cfg-row-control');
    var sw = el('button', 'maarm-toggle');
    sw.type = 'button';
    sw.id = domId(entry, field, 'switch');
    sw.setAttribute('role', 'switch');
    sw.setAttribute('aria-checked', 'false');
    sw.setAttribute('aria-labelledby', label.id);
    control.appendChild(sw);
    var shell = rowShell(label, control);
    shell.wrap.classList.add('cfg-row--switch');
    if (field.description) {
      shell.desc.id = domId(entry, field, 'desc');
      sw.setAttribute('aria-describedby', shell.desc.id);
    }
    sw.addEventListener('click', function () {
      if (locked || entry.locked) return;
      var patch = {};
      patch[field.key] = !entry.cfg[field.key];
      savePatch(entry.mid, patch);
    });
    return {
      field: field, wrap: shell.wrap, sw: sw, title: label, desc: shell.desc,
    };
  }

  function buildSegmented(entry, field) {
    var label = el('div', 'cfg-row-label', field.label);
    label.id = domId(entry, field, 'label');
    var control = el('div', 'cfg-row-control');
    var seg = el('div', 'maarm-segmented');
    seg.setAttribute('role', 'group');
    seg.setAttribute('aria-labelledby', label.id);
    var buttons = (field.options || []).map(function (option, index) {
      var btn = el('button', 'seg-btn');
      btn.appendChild(el('span', 'seg-label', option.label));
      if (option.badge && option.badge.text) {
        var badge = el('span', 'seg-badge', option.badge.text);
        if (option.badge.level) badge.classList.add('seg-badge--' + option.badge.level);
        btn.appendChild(badge);
      }
      btn.type = 'button';
      btn.addEventListener('click', function () {
        if (locked || entry.locked) return;
        var patch = {};
        patch[field.key] = option.value;
        savePatch(entry.mid, patch);
      });
      btn.dataset.index = String(index);
      seg.appendChild(btn);
      return btn;
    });
    control.appendChild(seg);
    var shell = rowShell(label, control);
    return {
      field: field, wrap: shell.wrap, seg: seg, buttons: buttons,
      title: label, desc: shell.desc,
    };
  }

  function buildSelect(entry, field) {
    var label = el('label', 'cfg-row-label');
    label.textContent = field.label;
    var control = el('div', 'cfg-row-control');
    var select = el('select');
    select.id = domId(entry, field, 'select');
    label.setAttribute('for', select.id);
    control.appendChild(select);
    var shell = rowShell(label, control);
    select.addEventListener('change', function () {
      if (locked || entry.locked) return;
      var option = select.options[select.selectedIndex];
      if (!option) return;
      var patch = {};
      patch[field.key] = option._maarmValue;
      savePatch(entry.mid, patch);
    });
    return {
      field: field, wrap: shell.wrap, select: select, title: label, desc: shell.desc,
      optSig: '',
    };
  }

  // 数字字段（number / stepper 两型共用一处）：胶囊本身由 js/number-pill.js 提供——
  // 输入、按住步进、滚动动效与写后端的时机都在那里，这里只接字段语义：
  // 标签、锁、以及 stepper 的 unit_seconds（每 1 单位配置值的预计秒数，数值来自插件）。
  // 说明行前缀的「预计用时约 X」按当前值即时乘算；值为 0 表示不限，不出估计。
  function formatEstimateSeconds(seconds) {
    var minutes = Math.round(seconds / 60);
    if (minutes < 1) return '不足 1 分钟';
    if (minutes < 60) return '约 ' + minutes + ' 分钟';
    var hours = Math.floor(minutes / 60);
    var rest = minutes % 60;
    return rest ? '约 ' + hours + ' 小时 ' + rest + ' 分钟'
                : '约 ' + hours + ' 小时';
  }

  function buildNumeric(entry, field) {
    var min = field.min != null ? Number(field.min) : 0;
    var max = field.max != null ? Number(field.max) : null;
    var stepv = field.step != null ? Number(field.step) : 1;
    var unitSeconds = field.unit_seconds != null ? Number(field.unit_seconds) : null;

    var label = el('div', 'cfg-row-label', field.label);
    var control = el('div', 'cfg-row-control');

    function hintFor(v) {
      if (!unitSeconds) return field.hint || '';
      if (!v) return '不限轮数';
      return '预计用时' + formatEstimateSeconds(v * unitSeconds);
    }

    var node = null;
    var pill = MaaRM.numberPill.create({
      min: min,
      max: max,
      step: stepv,
      suffix: field.suffix,
      label: field.label,
      zeroLabel: field.type === 'stepper' ? '不限' : '',
      locked: locked || entry.locked,
      alive: function () { return ST.isAlive(entry, generation); },
      onLive: function (v) {
        // 连滚期间就重算说明行的预计用时：等落定再算，读到的是旧值
        if (node && node.descHint && unitSeconds) node.descHint.textContent = hintFor(v);
      },
      onCommit: function (v) {
        if (locked || entry.locked) return;
        var patch = {};
        patch[field.key] = v;
        savePatch(entry.mid, patch);
      },
    });
    control.appendChild(pill.el);
    var shell = rowShell(label, control);

    node = {
      field: field, wrap: shell.wrap, pill: pill,
      title: label, desc: shell.desc,
    };
    node.hintFn = function (cfg) {
      var raw = cfg ? cfg[field.key] : null;
      return hintFor(raw == null ? min : Number(raw));
    };
    node.sync = function (value) {
      pill.setValue(value == null || !Number.isFinite(Number(value)) ? min : Number(value));
    };
    node.setLocked = function (isLocked) { pill.setLocked(isLocked); };
    return node;
  }

  function buildText(entry, field) {
    var label = el('label', 'cfg-row-label');
    label.textContent = field.label;
    var control = el('div', 'cfg-row-control');
    var input = el('input');
    input.type = 'text';
    input.id = domId(entry, field, 'text');
    if (field.placeholder) input.placeholder = field.placeholder;
    label.setAttribute('for', input.id);
    control.appendChild(input);
    var shell = rowShell(label, control);
    input.addEventListener('change', function () {
      if (locked || entry.locked) return;
      var patch = {};
      patch[field.key] = input.value;
      savePatch(entry.mid, patch);
    });
    return {
      field: field, wrap: shell.wrap, input: input, title: label, desc: shell.desc,
    };
  }

  function runNoticeAction(entry, action) {
    var call;
    if (action.action === 'download_release') {
      call = MaaRM.maarm.call('open_external_url', { target: 'download' });
    } else if (action.action === 'open_user_data_folder') {
      var params = action.subpath ? { subpath: action.subpath } : {};
      call = MaaRM.maarm.call('open_user_data_folder', params);
    } else {
      return;
    }
    Promise.resolve(call).catch(function (err) {
      var text = err && err.message ? err.message : String(err);
      flashStatus(entry, '打开失败: ' + text, 'error', 3000);
    });
  }

  function buildNotice(entry, notice) {
    var row = el('div', 'field-notice');
    if (notice.level) row.classList.add('field-notice--' + notice.level);
    row.appendChild(el('span', 'field-notice-text', notice.text));
    var actions = Array.isArray(notice.actions) ? notice.actions : [];
    if (actions.length) {
      var box = el('div', 'field-notice-actions');
      actions.forEach(function (action) {
        var btn = el('button', 'field-notice-btn', action.label);
        btn.type = 'button';
        btn.addEventListener('click', function () { runNoticeAction(entry, action); });
        box.appendChild(btn);
      });
      row.appendChild(box);
    }
    return row;
  }

  // ---------- 插件自有资产安装（能力由插件 manifest 声明，宿主只渲染） ----------
  var INSTALL_ACTIVE = {
    downloading: true, verifying: true, extracting: true, committing: true,
  };
  // idle 由声明名词拼出（「<label>已就绪 / 未安装」），宿主不认识任何模块语义
  var INSTALL_PHASE_TEXT = {
    downloading: '下载中', verifying: '校验中',
    extracting: '解压中', committing: '安装中', done: '安装完成',
    failed: '安装失败', cancelled: '已取消',
  };

  function stopInstallPolling(entry) {
    if (entry.installTimer) {
      clearInterval(entry.installTimer);
      entry.installTimer = null;
    }
  }

  function ensureInstallPolling(entry) {
    if (!entry || !ST.isAlive(entry, generation) || entry.installTimer) return;
    entry.installTimer = setInterval(function () { refreshInstall(entry); }, 1000);
  }

  async function refreshInstall(entry) {
    if (!entry || !ST.isAlive(entry, generation)) return;
    var nodes = entry.nodes || {};
    if (!nodes.install) return;
    var resp;
    try {
      resp = await MaaRM.maarm.call('install_asset_status', { module_id: entry.mid });
    } catch (e) {
      return; // 轮询失败不刷屏，下轮再取
    }
    if (!ST.isAlive(entry, generation) || !entry.nodes || !entry.nodes.install) return;
    if (!resp || !resp.supported) {
      nodes.install.style.display = 'none';
      stopInstallPolling(entry);
      return;
    }
    if (typeof resp.revision === 'number' &&
        typeof entry.installRev === 'number' && resp.revision < entry.installRev) return;
    if (typeof resp.revision === 'number') entry.installRev = resp.revision;
    entry.installStatus = resp;
    var label = resp.label || '资产';
    var phase = resp.phase || 'idle';
    // 空闲且资产已就绪：整行隐藏，缺失提示消失，不留一个永远可点的安装入口。
    // 缺不缺模型只认后端 needs_install（可刷新查询）。必须放在 revision 守卫之后——
    // 旧响应的 needs_install 不能覆盖在途安装进度。
    if (phase === 'idle' && !resp.needs_install) {
      nodes.install.style.display = 'none';
      stopInstallPolling(entry);
      return;
    }
    nodes.install.style.display = '';
    var text;
    if (phase === 'idle') {
      text = resp.missing_hint || (label + '未安装');
    } else {
      text = INSTALL_PHASE_TEXT[phase] || phase;
      if (phase === 'downloading' && resp.total) {
        text += ' ' + Math.round(resp.downloaded * 100 / resp.total) + '%';
      }
      if (resp.error) text += ' · ' + resp.error;
    }
    nodes.installText.textContent = text;
    nodes.installBtn.textContent = INSTALL_ACTIVE[phase] ? '取消' : ('一键安装' + label);
    nodes.installBtn.disabled = !!(locked || entry.locked);
    if (INSTALL_ACTIVE[phase]) ensureInstallPolling(entry);
    else stopInstallPolling(entry);
  }

  async function runInstallAction(entry) {
    var active = entry.installStatus && INSTALL_ACTIVE[entry.installStatus.phase];
    var label = (entry.installStatus && entry.installStatus.label) || '资产';
    try {
      // 方法名保持字面量：白名单测试按字面量扫调用点（见 tests/test_rpc_allowlist.py）
      if (active) {
        await MaaRM.maarm.call('install_asset_cancel', { module_id: entry.mid });
      } else {
        await MaaRM.maarm.call('install_asset_start', { module_id: entry.mid });
      }
      ensureInstallPolling(entry);
      await refreshInstall(entry);
    } catch (e) {
      if (!ST.isAlive(entry, generation)) return;
      // 落点必须是文字节点：传整行容器会把「一键安装 / 取消」按钮一起 textContent 清掉
      MaaRM.reportError('inline', label + '安装操作失败: ' + (e.message || e),
        { el: entry.nodes && entry.nodes.installText });
    }
  }

  function buildField(entry, field) {
    switch (field.type) {
      case 'switch': return buildSwitch(entry, field);
      case 'segmented': return buildSegmented(entry, field);
      case 'select': return buildSelect(entry, field);
      case 'number': return buildNumeric(entry, field);
      case 'stepper': return buildNumeric(entry, field);
      case 'text': return buildText(entry, field);
      default: return null;
    }
  }

  // ---------- 字段更新 ----------

  function applyText(node, field, cfg) {
    var variant = pickVariant(field, cfg);
    var label = (variant && variant.label) || field.label;
    var desc = variant && variant.description !== undefined
      ? variant.description : field.description;
    node.title.textContent = label;
    if (!node.desc) return;
    // 提示（含滚轮预计用时）与说明共处一行；提示只占前缀，说明是正文。
    var hint = node.hintFn ? node.hintFn(cfg) : (field.hint || '');
    node.desc.textContent = '';
    node.descHint = null;
    if (hint) {
      node.descHint = el('span', 'cfg-desc-hint', hint);
      node.desc.appendChild(node.descHint);
    }
    if (hint && desc) node.desc.appendChild(document.createTextNode(' · '));
    if (desc) node.desc.appendChild(document.createTextNode(desc));
    node.desc.style.display = (hint || desc) ? '' : 'none';
  }

  function syncSelect(node, cfg) {
    var options = choicesFor(node.field, cfg);
    var sig = JSON.stringify(options.map(function (o) { return [o.value, o.label]; }));
    if (node.optSig !== sig) {
      node.select.textContent = '';
      options.forEach(function (option) {
        var item = el('option', null, option.label);
        item._maarmValue = option.value;
        node.select.appendChild(item);
      });
      node.optSig = sig;
    }
    var value = cfg ? cfg[node.field.key] : undefined;
    var found = false;
    Array.prototype.forEach.call(node.select.options, function (item) {
      var hit = sameValue(item._maarmValue, value);
      item.selected = hit;
      if (hit) found = true;
    });
    if (!found && value !== undefined && value !== null && value !== '') {
      var missing = el('option', null, '无（原选项已失效）');
      missing._maarmValue = value;
      node.select.appendChild(missing);
      missing.selected = true;
    }
  }

  function applyField(node, cfg) {
    var field = node.field;
    var visible = evalCond(field.visible_if, cfg);
    node.visible = visible;
    node.wrap.style.display = visible ? '' : 'none';
    if (node.noticeEl) node.noticeEl.style.display = visible ? '' : 'none';
    applyText(node, field, cfg);
    var value = cfg ? cfg[field.key] : undefined;
    if (node.sw) {
      var on = !!value;
      node.sw.classList.toggle('maarm-toggle--active', on);
      node.sw.setAttribute('aria-checked', String(on));
    } else if (node.buttons) {
      node.buttons.forEach(function (btn, index) {
        var option = field.options[index];
        btn.classList.toggle('seg-btn--selected', sameValue(option.value, value));
      });
    } else if (node.select) {
      syncSelect(node, cfg);
    } else if (node.pill) {
      node.sync(value);
    } else if (node.input) {
      if (document.activeElement !== node.input) {
        node.input.value = value == null ? '' : String(value);
      }
    }
  }

  function applyAll(entry, cfg) {
    entry.nodes.fields.forEach(function (node) { applyField(node, cfg); });
    applyGroups(entry);
  }

  // 分组显隐联动：组内没有任何可见字段时整组（标题、分隔线与字段）一并藏起。
  // 不认模块字段，只认「组里还有没有看得见的控件」这一条通用规则。
  function applyGroups(entry) {
    if (!Array.isArray(entry.nodes.groups)) return;
    entry.nodes.groups.forEach(function (group) {
      var visible = group.keys.some(function (key) {
        var node = entry.nodes.fields.get(key);
        return !!(node && node.visible);
      });
      group.el.style.display = visible ? '' : 'none';
    });
  }

  function applyNote(entry, cfg) {
    var note = entry.nodes.note;
    var text = cfg && cfg._ui_note;
    note.textContent = text || '';
    note.classList.toggle('maarm-status--warn', !!text);
    note.style.display = text ? '' : 'none';
  }

  function applyLock(entry) {
    if (!entry.nodes) return;
    entry.nodes.fields.forEach(function (node) {
      if (node.sw) node.sw.disabled = locked;
      if (node.buttons) node.buttons.forEach(function (b) { b.disabled = locked; });
      if (node.select) node.select.disabled = locked;
      if (node.input) node.input.disabled = locked;
      if (node.setLocked) node.setLocked(locked);
    });
  }

  // ---------- 状态行 ----------

  function setStatus(entry, text, level) {
    if (!entry.nodes || !entry.nodes.status) return;
    MaaRM.setInlineStatus(entry.nodes.status, text, level);
  }

  function renderStatus(entry) {
    if (!entry.cfg || entry.localStatus) return;
    var status = entry.cfg._status;
    setStatus(entry, status ? status.text : '', status ? status.level : '');
  }

  function showSaving(entry) {
    clearTimeout(entry._statusTimer);
    entry.localStatus = true;
    setStatus(entry, '保存中...', '');
  }

  function flashStatus(entry, text, level, ms) {
    clearTimeout(entry._statusTimer);
    entry.localStatus = true;
    setStatus(entry, text, level);
    entry._statusTimer = setTimeout(function () {
      entry.localStatus = false;
      renderStatus(entry);
    }, ms || 1600);
  }

  // ---------- 构建与渲染 ----------

  // 面板重建 / 整表重建时收掉数字胶囊的弹簧循环与落定定时器：DOM 一换，
  // 旧实例没必要再转，pending 的写入也不该落到已经消失的字段上。
  function destroyFields(nodes) {
    if (!nodes || !nodes.fields) return;
    nodes.fields.forEach(function (node) {
      if (node.pill) node.pill.destroy();
    });
  }

  function build(entry, host, cfg) {
    host.textContent = '';
    destroyFields(entry.nodes);
    var root = el('div', 'config-panel');
    var note = el('p', 'module-options-status config-panel-note');
    note.style.display = 'none';
    root.appendChild(note);
    var nodes = { host: host, root: root, note: note, status: null,
                  fields: new Map(), groups: [] };
    entry.nodes = nodes;
    var ui = cfg && cfg._ui;
    if (ui && Array.isArray(ui.groups)) {
      ui.groups.forEach(function (group) {
        if (!group || !Array.isArray(group.fields)) return;
        var groupEl = el('section', 'cfg-group');
        if (group.title) groupEl.appendChild(el('div', 'cfg-group-title', group.title));
        var body = el('div', 'cfg-group-body');
        var keys = [];
        group.fields.forEach(function (field) {
          var node = buildField(entry, field);
          if (!node) return;
          body.appendChild(node.wrap);
          node.noticeEl = field.notice ? buildNotice(entry, field.notice) : null;
          if (node.noticeEl) body.appendChild(node.noticeEl);
          nodes.fields.set(field.key, node);
          keys.push(field.key);
        });
        groupEl.appendChild(body);
        root.appendChild(groupEl);
        nodes.groups.push({ el: groupEl, keys: keys });
      });
    }
    var status = el('p', 'module-options-status');
    root.appendChild(status);
    nodes.status = status;

    var install = el('div', 'field-notice config-install');
    install.style.display = 'none';
    var installText = el('span', 'field-notice-text', '');
    var installBtn = el('button', 'field-notice-btn', '一键安装');
    installBtn.type = 'button';
    installBtn.addEventListener('click', function () { runInstallAction(entry); });
    install.appendChild(installText);
    install.appendChild(installBtn);
    root.appendChild(install);
    nodes.install = install;
    nodes.installText = installText;
    nodes.installBtn = installBtn;

    host.appendChild(root);
    entry.sig = signature(cfg);
  }

  function render(mid, cfg) {
    if (!cfg) return;
    var host = panelEl(mid);
    if (!host) return;
    var entry = entries.get(mid);
    var rebuild = !entry || !ST.isAlive(entry, generation)
      || entry.sig !== signature(cfg)
      || !entry.nodes || entry.nodes.host !== host;
    if (rebuild) {
      if (entry) {
        ST.kill(entry);
        clearTimeout(entry._statusTimer);
        stopInstallPolling(entry);
      }
      entry = ST.createEntry(mid, generation);
      entries.set(mid, entry);
      build(entry, host, cfg);
    }
    entry.cfg = cfg;
    entry.locked = locked;
    applyNote(entry, cfg);
    applyAll(entry, cfg);
    applyLock(entry);
    renderStatus(entry);
    refreshInstall(entry);
  }

  // 运行中轮询：只更新状态行与摘要，不重渲表单、不回写输入框。
  function updateLive(mid, cfg) {
    var entry = entries.get(mid);
    if (!entry || !ST.isAlive(entry, generation) || !cfg) return;
    if (entry.saving) return;                  // 保存期间不接受轮询覆盖
    if (!ST.acceptResponse(entry, cfg)) return; // 旧响应丢弃
    var before = entry.cfg && entry.cfg._summary;
    entry.cfg = Object.assign({}, entry.cfg || {}, cfg);
    // 只有摘要变化才轻刷卡片；状态行每秒更新不值得重建 chips/阶段列表
    if (JSON.stringify(before) !== JSON.stringify(cfg._summary) && MaaRM.noteConfigSaved) {
      MaaRM.noteConfigSaved(mid, cfg);
    }
    renderStatus(entry);
  }

  function reset() {
    generation += 1;
    entries.forEach(function (entry) {
      destroyFields(entry.nodes);
      ST.kill(entry);
      clearTimeout(entry._statusTimer);
    });
    entries.clear();
  }

  function setLocked(running) {
    locked = !!running;
    entries.forEach(function (entry) {
      entry.locked = locked;
      applyLock(entry);
      if (entry.nodes && entry.nodes.installBtn) {
        entry.nodes.installBtn.disabled = !!running;
      }
    });
  }

  function has(mid) {
    return entries.has(mid);
  }

  // ---------- 保存 ----------

  function applyOptimistic(entry, patch) {
    entry.cfg = Object.assign({}, entry.cfg || {}, patch);
    applyAll(entry, entry.cfg);
  }

  async function runSave(entry, patch) {
    var mid = entry.mid;
    var snapshot = Object.assign({}, entry.cfg);
    var failed = false;
    applyOptimistic(entry, patch);
    showSaving(entry);
    try {
      var resp = await MaaRM.maarm.call('set_module_config', {
        module_id: mid,
        config: patch,
      });
      if (!ST.isAlive(entry, generation)) return;
      if (!ST.acceptResponse(entry, resp)) return;
      entry.cfg = resp;
      applyNote(entry, resp);
      applyAll(entry, resp);
      if (MaaRM.noteConfigSaved) MaaRM.noteConfigSaved(mid, resp);
      // 成功不刷回执：状态行回落到模块自己的运行实况（界面只报阶段进度与影响结果的异常），
      // 失败仍走行内红字停留。这里必须解锁，否则状态行会停在「保存中」。
      entry.localStatus = false;
    } catch (e) {
      if (!ST.isAlive(entry, generation)) return;
      failed = true;
      entry.cfg = snapshot;
      applyNote(entry, snapshot);
      applyAll(entry, snapshot);
      MaaRM.reportError('inline', '保存失败: ' + (e.message || e),
        { el: entry.nodes && entry.nodes.status });
    } finally {
      if (ST.isAlive(entry, generation)) {
        var pending = ST.finishSave(entry);
        if (pending) {
          savePatch(mid, pending);
        } else if (!failed && !entry.localStatus) {
          renderStatus(entry);
        }
      }
    }
  }

  function savePatch(mid, patch) {
    var entry = entries.get(mid);
    if (!entry || !ST.isAlive(entry, generation) || locked || entry.locked) return;
    if (entry.saving) {
      entry.pending = ST.mergePatch(entry.pending, patch);
      applyOptimistic(entry, patch);
      return;
    }
    if (!ST.beginSave(entry)) return;
    runSave(entry, patch);
  }

  Object.assign(MaaRM, {
    configPanel: {
      render: render,
      updateLive: updateLive,
      reset: reset,
      setLocked: setLocked,
      has: has,
    },
  });
})();
