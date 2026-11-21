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

  function buildSwitch(entry, field) {
    var wrap = el('div', 'option-row');
    var sw = el('button', 'maarm-toggle');
    sw.type = 'button';
    sw.id = domId(entry, field, 'switch');
    sw.setAttribute('role', 'switch');
    sw.setAttribute('aria-checked', 'false');
    var main = el('div', 'option-main');
    var title = el('div', 'option-title', field.label);
    title.id = domId(entry, field, 'label');
    sw.setAttribute('aria-labelledby', title.id);
    main.appendChild(title);
    var desc = null;
    if (field.description) {
      desc = el('p', 'option-desc', field.description);
      desc.id = domId(entry, field, 'desc');
      sw.setAttribute('aria-describedby', desc.id);
      main.appendChild(desc);
    }
    wrap.appendChild(sw);
    wrap.appendChild(main);
    sw.addEventListener('click', function () {
      if (locked || entry.locked) return;
      var patch = {};
      patch[field.key] = !entry.cfg[field.key];
      savePatch(entry.mid, patch);
    });
    return {
      field: field, wrap: wrap, sw: sw, title: title, desc: desc,
    };
  }

  function buildSegmented(entry, field) {
    var wrap = el('div', 'module-options-row');
    var box = el('div', 'maarm-field');
    var label = el('label', 'maarm-field-label');
    label.textContent = field.label;
    if (field.hint) label.appendChild(el('span', 'field-hint', '（' + field.hint + '）'));
    var seg = el('div', 'maarm-segmented');
    seg.setAttribute('role', 'group');
    seg.setAttribute('aria-label', field.label);
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
    box.appendChild(label);
    box.appendChild(seg);
    var desc = null;
    if (field.description) {
      desc = el('p', 'option-desc', field.description);
      box.appendChild(desc);
    }
    wrap.appendChild(box);
    return {
      field: field, wrap: wrap, seg: seg, buttons: buttons, title: label, desc: desc,
    };
  }

  function buildSelect(entry, field) {
    var wrap = el('div', 'module-options-row');
    var box = el('div', 'maarm-field');
    var label = el('label', 'maarm-field-label');
    label.textContent = field.label;
    if (field.hint) label.appendChild(el('span', 'field-hint', '（' + field.hint + '）'));
    var control = el('div', 'maarm-field-control');
    var select = el('select');
    select.id = domId(entry, field, 'select');
    label.setAttribute('for', select.id);
    control.appendChild(select);
    box.appendChild(label);
    box.appendChild(control);
    var desc = null;
    if (field.description) {
      desc = el('p', 'option-desc', field.description);
      box.appendChild(desc);
    }
    wrap.appendChild(box);
    select.addEventListener('change', function () {
      if (locked || entry.locked) return;
      var option = select.options[select.selectedIndex];
      if (!option) return;
      var patch = {};
      patch[field.key] = option._maarmValue;
      savePatch(entry.mid, patch);
    });
    return {
      field: field, wrap: wrap, select: select, title: label, desc: desc,
      optSig: '',
    };
  }

  function buildNumber(entry, field) {
    var wrap = el('div', 'module-options-row');
    var box = el('div', 'maarm-field');
    var label = el('label', 'maarm-field-label');
    label.textContent = field.label;
    if (field.hint) label.appendChild(el('span', 'field-hint', '（' + field.hint + '）'));
    var control = el('div', 'maarm-field-control');
    var input = el('input');
    input.type = 'number';
    input.id = domId(entry, field, 'number');
    label.setAttribute('for', input.id);
    if (field.min != null) input.min = String(field.min);
    if (field.max != null) input.max = String(field.max);
    if (field.step != null) input.step = String(field.step);
    control.appendChild(input);
    if (field.suffix) control.appendChild(el('span', 'field-suffix', field.suffix));
    box.appendChild(label);
    box.appendChild(control);
    var desc = null;
    if (field.description) {
      desc = el('p', 'option-desc', field.description);
      box.appendChild(desc);
    }
    wrap.appendChild(box);
    function commit() {
      if (locked || entry.locked) return;
      var value = parseFloat(input.value);
      if (Number.isNaN(value)) value = 0;
      if (Number.isFinite(field.min) && value < field.min) value = field.min;
      if (Number.isFinite(field.max) && value > field.max) value = field.max;
      input.value = String(value);
      var patch = {};
      patch[field.key] = value;
      savePatch(entry.mid, patch);
    }
    input.addEventListener('change', commit);
    input.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') commit();
    });
    return {
      field: field, wrap: wrap, input: input, title: label, desc: desc,
    };
  }

  // ---------- 滚轮计数器（stepper 字段）----------
  // 行为移植自 21st.dev 的 Rolling Counter（MIT，作者 Syed；vanilla JS 重写、
  // 外观换本仓设计 token）：每列一个 rAF 弹簧模拟（高位错峰起步），速度越大
  // 模糊越重；± 可按住加速连滚；数字窗竖向拖拽带惯性甩动；键盘走 spinbutton
  // 语义；prefers-reduced-motion 全部瞬时落位。写后端的时机：一律等弹簧收敛
  // （动画落定）才 savePatch——连滚/拖拽期间只在本地滚动，不逐变化发请求。
  // unit_seconds 是模块下发的「每 1 单位配置值的预计秒数」，预计用时按当前值
  // 即时重算；值为 0 表示不限，不出估计。

  var CELL_H = 36;          // 数字窗高 = 单格高（滚动一行 = 一格）
  var STAGGER_MS = 32;      // 列间错峰：高位晚这么久起步，滚出层次感
  var BOUNCE = 40;          // 弹簧弹性（0~100，原控件默认）
  var BLUR = 24;            // 速度模糊强度（0~100，原控件默认）
  var HOLD_DELAY_MS = 320;  // 按住 ± 多久后开始连滚
  var REDUCED_COMMIT_MS = 300;  // 无动画档的落定等待：连点只写最后一次
  var FALLBACK_COMMIT_MS = 900; // 落定兜底：rAF 在窗口隐藏/遮挡时会暂停
  var DRAG_PX = 12;         // 拖拽每走一格的像素
  // 数字条 12 格：末尾补 0/1，9→0 向前绕回时不断带（减量走 mod 环绕同样成立）
  var DIGITS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 0, 1];

  function prefersReducedMotion() {
    return !!(window.matchMedia
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function formatEstimateSeconds(seconds) {
    var minutes = Math.round(seconds / 60);
    if (minutes < 1) return '不足 1 分钟';
    if (minutes < 60) return '约 ' + minutes + ' 分钟';
    var hours = Math.floor(minutes / 60);
    var rest = minutes % 60;
    return rest ? '约 ' + hours + ' 小时 ' + rest + ' 分钟'
                : '约 ' + hours + ' 小时';
  }

  function buildStepper(entry, field) {
    var min = field.min != null ? Number(field.min) : 0;
    var max = Number(field.max);
    var stepv = field.step != null ? Number(field.step) : 1;
    var unitSeconds = field.unit_seconds != null ? Number(field.unit_seconds) : null;
    var places = String(max).length;

    var wrap = el('div', 'module-options-row');
    var box = el('div', 'maarm-field');
    var label = el('label', 'maarm-field-label');
    label.textContent = field.label;
    if (field.hint) label.appendChild(el('span', 'field-hint', '（' + field.hint + '）'));
    var estimate = null;
    if (unitSeconds) {
      estimate = el('span', 'field-hint');
      label.appendChild(estimate);
    }

    var stepper = el('div', 'maarm-stepper');
    var dec = el('button', 'stepper-btn', '\u2212');
    dec.type = 'button';
    dec.tabIndex = -1;
    dec.setAttribute('aria-label', '减少');
    var inc = el('button', 'stepper-btn', '+');
    inc.type = 'button';
    inc.tabIndex = -1;
    inc.setAttribute('aria-label', '增加');

    var win = el('div', 'stepper-window');
    win.tabIndex = 0;
    win.setAttribute('role', 'spinbutton');
    win.setAttribute('aria-label', field.label);
    win.setAttribute('aria-valuemin', String(min));
    win.setAttribute('aria-valuemax', String(max));

    var columns = [];
    for (var i = 0; i < places; i += 1) {
      var p = places - 1 - i;   // DOM 从高位列到低位列；columns 仍按位权 p 索引
      var colBox = el('div', 'stepper-col');
      var strip = el('div', 'stepper-strip');
      DIGITS.forEach(function (d) {
        strip.appendChild(el('div', 'stepper-num', String(d)));
      });
      colBox.appendChild(strip);
      win.appendChild(colBox);
      columns[p] = { box: colBox, strip: strip, pos: 0, vel: 0, target: 0,
                     due: [], next: [] };
    }
    if (field.suffix) win.appendChild(el('span', 'stepper-suffix', field.suffix));

    stepper.appendChild(dec);
    stepper.appendChild(win);
    stepper.appendChild(inc);
    box.appendChild(label);
    box.appendChild(stepper);
    var desc = null;
    if (field.description) {
      desc = el('p', 'option-desc', field.description);
      box.appendChild(desc);
    }
    wrap.appendChild(box);

    var shown = null;        // 本地显示值；弹簧收敛前可与服务端配置暂时不同
    var rafId = 0;
    var lastTime = 0;
    var flingRaf = 0;
    var commitTimer = 0;

    function clamp(v) {
      if (Number.isNaN(v)) v = min;
      if (v < min) v = min;
      if (v > max) v = max;
      return Math.round(v);
    }

    function valueText(v) {
      return v <= 0 ? '不限' : v + (field.suffix ? ' ' + field.suffix : '');
    }

    function updateEstimate(v) {
      if (!estimate) return;
      estimate.textContent = v <= 0 ? '（不限）'
        : '（预计用时' + formatEstimateSeconds(v * unitSeconds) + '）';
    }

    function paint() {
      columns.forEach(function (col) {
        var mod = ((col.pos % 10) + 10) % 10;
        var smear = Math.min(2.2, Math.abs(col.vel) * 1.8) * (BLUR / 50);
        col.strip.style.transform = 'translate3d(0,' + (-mod * CELL_H) + 'px,0)';
        col.strip.style.filter = smear > 0.12
          ? 'blur(' + smear.toFixed(2) + 'px)' : 'none';
      });
    }

    function flushCommit() {
      if (!ST.isAlive(entry, generation) || locked || entry.locked) return;
      if (entry.cfg && entry.cfg[field.key] === shown) return;
      var patch = {};
      patch[field.key] = shown;
      savePatch(entry.mid, patch);
    }

    function tick(time) {
      if (!ST.isAlive(entry, generation)) {   // 面板重建：孤儿循环就地收
        rafId = 0;
        return;
      }
      var dt = lastTime ? Math.min(2.5, Math.max(0, (time - lastTime) / 16.67)) : 1;
      lastTime = time;
      // 弹簧参数随 BOUNCE 取（原控件同款：k 刚度 / d 阻尼）
      var k = 0.1 + (BOUNCE / 100) * 0.16;
      var damp = 0.64 + (BOUNCE / 100) * 0.18;
      var now = performance.now();
      var active = false;
      columns.forEach(function (col) {
        // 错峰队列：到点的目标才生效，快速连改在队列里排队不跳变
        while (col.due.length && col.due[0] <= now) {
          col.due.shift();
          col.target = col.next.shift();
        }
        if (col.due.length) active = true;
        var delta = col.target - col.pos;
        if (Math.abs(delta) > 20) {
          // 大跨度（Home/End、×10 连滚）：先按十位跳近，别让弹簧拉到失真
          col.pos += Math.round((delta - (delta > 0 ? 20 : -20)) / 10) * 10;
        }
        col.vel += (col.target - col.pos) * k * dt;
        col.vel *= Math.pow(damp, dt);
        col.pos += col.vel * dt;
        if (Math.abs(col.target - col.pos) < 0.003 && Math.abs(col.vel) < 0.003) {
          col.pos = col.target;
          col.vel = 0;
        } else {
          active = true;
        }
      });
      paint();
      if (active) {
        rafId = requestAnimationFrame(tick);
      } else {
        rafId = 0;
        lastTime = 0;
        flushCommit();   // 动画落定才写后端（连滚期间只滚不写）
      }
    }

    function runSimulation() {
      if (!rafId) {
        lastTime = 0;
        rafId = requestAnimationFrame(tick);
      }
    }

    function syncVisibility() {
      var sig = Math.max(1, String(shown).length);
      columns.forEach(function (col, p) {
        col.box.classList.toggle('stepper-col--hidden', p >= sig);
      });
    }

    function applyValue(nextVal) {
      nextVal = clamp(nextVal);
      var mount = shown == null;
      if (!mount && nextVal === shown) return;
      shown = nextVal;
      var reduced = prefersReducedMotion();
      columns.forEach(function (col, p) {
        var to = Math.floor(nextVal / Math.pow(10, p));
        if (mount || reduced) {
          col.pos = col.target = to;
          col.vel = 0;
          col.due = [];
          col.next = [];
        } else {
          col.due.push(performance.now() + p * STAGGER_MS);
          col.next.push(to);
        }
      });
      if (mount || reduced) {
        paint();
        if (!mount) {   // 无动画档也要攒一拍：连点只写最后一次
          clearTimeout(commitTimer);
          commitTimer = setTimeout(flushCommit, REDUCED_COMMIT_MS);
        }
      } else {
        runSimulation();   // 落定后的 flushCommit 在 tick 收敛分支里
        // 兜底：rAF 在窗口隐藏/遮挡时会暂停，模拟可能迟迟不收敛——定时器
        // 保证最终值一定落库（弹簧先收敛时本定时器到点即空转）。
        clearTimeout(commitTimer);
        commitTimer = setTimeout(flushCommit, FALLBACK_COMMIT_MS);
      }
      syncVisibility();
      win.setAttribute('aria-valuenow', String(nextVal));
      win.setAttribute('aria-valuetext', valueText(nextVal));
      updateEstimate(nextVal);
    }

    function bump(multiplier) {
      if (shown == null) return;
      applyValue(shown + multiplier * stepv);
    }

    function sync(value) {
      if (value == null || !Number.isFinite(value)) return;
      var target = clamp(value);
      if (shown !== target) applyValue(target);
      else updateEstimate(shown);   // _ui 重建带来新 unit_seconds 时重算估时
    }

    function bindHold(btn, dir) {
      var holdTimer = 0;
      var holdStart = 0;
      function repeat() {
        if (locked || entry.locked) { holdTimer = 0; return; }
        // 原控件同款加速曲线：0.7s 后 ×5、1.4s 后 ×10，节拍 85ms → 55ms
        var elapsed = performance.now() - holdStart;
        var speedUp = elapsed > 1400 ? 10 : elapsed > 700 ? 5 : 1;
        bump(dir * speedUp);
        holdTimer = setTimeout(repeat, elapsed > 700 ? 55 : 85);
      }
      function stop() {
        if (holdTimer) { clearTimeout(holdTimer); holdTimer = 0; }
      }
      btn.addEventListener('pointerdown', function (event) {
        if (locked || entry.locked) return;
        if (event.pointerType === 'mouse' && event.button !== 0) return;
        try { btn.setPointerCapture(event.pointerId); } catch (e) { /* 无碍 */ }
        bump(dir);
        holdStart = performance.now();
        holdTimer = setTimeout(repeat, HOLD_DELAY_MS);
        event.preventDefault();
      });
      ['pointerup', 'pointercancel', 'pointerleave'].forEach(function (type) {
        btn.addEventListener(type, stop);
      });
      // 键盘激活（Enter/Space 的 click 无 pointer 来源）：补一次步进
      btn.addEventListener('click', function (event) {
        if (event.detail === 0 && !locked && !entry.locked) bump(dir);
      });
    }
    bindHold(dec, -1);
    bindHold(inc, 1);

    win.addEventListener('keydown', function (event) {
      if (locked || entry.locked) return;
      var handled = true;
      switch (event.key) {
        case 'ArrowUp': case 'ArrowRight': bump(1); break;
        case 'ArrowDown': case 'ArrowLeft': bump(-1); break;
        case 'PageUp': bump(10); break;
        case 'PageDown': bump(-10); break;
        case 'Home': applyValue(min); break;
        case 'End': applyValue(max); break;
        default: handled = false;
      }
      if (handled) event.preventDefault();
    });

    win.addEventListener('wheel', function (event) {
      if (locked || entry.locked) return;
      event.preventDefault();
      bump(event.deltaY < 0 ? 1 : -1);
    }, { passive: false });

    // 竖向拖拽 + 惯性甩动（原控件同款：12px 一格，松手按速度衰减续滚）
    var drag = null;
    function offsetTicks(acc) {
      var ticks = acc > 0 ? Math.floor(acc / DRAG_PX) : Math.ceil(acc / DRAG_PX);
      return { ticks: ticks, rest: acc - ticks * DRAG_PX };
    }
    win.addEventListener('pointerdown', function (event) {
      if (locked || entry.locked) return;
      cancelAnimationFrame(flingRaf);
      try { win.setPointerCapture(event.pointerId); } catch (e) { /* 无碍 */ }
      drag = { y: event.clientY, acc: 0, vel: 0, time: performance.now() };
    });
    win.addEventListener('pointermove', function (event) {
      if (!drag) return;
      var now = performance.now();
      var dy = drag.y - event.clientY;
      drag.y = event.clientY;
      var dt = Math.max(1, now - drag.time);
      drag.time = now;
      drag.vel = drag.vel * 0.6 + (dy / dt) * 0.4;
      var move = offsetTicks(drag.acc + dy);
      if (move.ticks) bump(move.ticks);
      drag.acc = move.rest;
    });
    function endDrag(event) {
      if (!drag) return;
      try { win.releasePointerCapture(event.pointerId); } catch (e) { /* 已释放 */ }
      var track = drag;
      drag = null;
      if (prefersReducedMotion()) return;
      var v = (performance.now() - track.time > 80) ? 0 : track.vel * 16.67;
      if (Math.abs(v) < 1.5) return;
      var acc = 0;
      (function coast() {
        var move = offsetTicks(acc + v);
        if (move.ticks) bump(move.ticks);
        acc = move.rest;
        v *= 0.93;
        if (Math.abs(v) > 0.35) flingRaf = requestAnimationFrame(coast);
      })();
    }
    win.addEventListener('pointerup', endDrag);
    win.addEventListener('pointercancel', endDrag);

    function setLockedState(isLocked) {
      dec.disabled = isLocked;
      inc.disabled = isLocked;
      stepper.classList.toggle('maarm-stepper--locked', isLocked);
    }

    return {
      field: field, wrap: wrap, stepper: stepper, title: label, desc: desc,
      sync: sync, setLocked: setLockedState,
    };
  }

  function buildText(entry, field) {
    var wrap = el('div', 'module-options-row');
    var box = el('div', 'maarm-field');
    var label = el('label', 'maarm-field-label');
    label.textContent = field.label;
    if (field.hint) label.appendChild(el('span', 'field-hint', '（' + field.hint + '）'));
    var control = el('div', 'maarm-field-control');
    var input = el('input');
    input.type = 'text';
    input.id = domId(entry, field, 'text');
    if (field.placeholder) input.placeholder = field.placeholder;
    label.setAttribute('for', input.id);
    control.appendChild(input);
    box.appendChild(label);
    box.appendChild(control);
    var desc = null;
    if (field.description) {
      desc = el('p', 'option-desc', field.description);
      box.appendChild(desc);
    }
    wrap.appendChild(box);
    input.addEventListener('change', function () {
      if (locked || entry.locked) return;
      var patch = {};
      patch[field.key] = input.value;
      savePatch(entry.mid, patch);
    });
    return {
      field: field, wrap: wrap, input: input, title: label, desc: desc,
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
    var method = active ? 'install_asset_cancel' : 'install_asset_start';
    var label = (entry.installStatus && entry.installStatus.label) || '资产';
    try {
      await MaaRM.maarm.call(method, { module_id: entry.mid });
      ensureInstallPolling(entry);
      await refreshInstall(entry);
    } catch (e) {
      if (!ST.isAlive(entry, generation)) return;
      MaaRM.reportError('inline', label + '安装操作失败: ' + (e.message || e),
        { el: entry.nodes && entry.nodes.install });
    }
  }

  function buildField(entry, field) {
    switch (field.type) {
      case 'switch': return buildSwitch(entry, field);
      case 'segmented': return buildSegmented(entry, field);
      case 'select': return buildSelect(entry, field);
      case 'number': return buildNumber(entry, field);
      case 'stepper': return buildStepper(entry, field);
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
    // 标题只换文本；挂在标题上的元素子节点（field-hint / 滚轮估时 span）原样保留
    // ——textContent 整体赋值会连子节点一起抹掉，同步一跑提示就消失。
    var title = node.title;
    var spans = [];
    for (var child = title.firstChild; child; child = child.nextSibling) {
      if (child.nodeType === 1) spans.push(child);
    }
    title.textContent = '';
    title.appendChild(document.createTextNode(label));
    spans.forEach(function (s) { title.appendChild(s); });
    if (node.desc) {
      if (desc) {
        node.desc.textContent = desc;
        node.desc.style.display = '';
      } else {
        node.desc.style.display = 'none';
      }
    }
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
    } else if (node.stepper) {
      var raw = value == null ? null : Number(value);
      node.sync(raw);
    } else if (node.input && node.input.type === 'number') {
      if (document.activeElement !== node.input) {
        node.input.value = value == null ? '' : String(value);
      }
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

  // 分组标题与字段显隐联动：组内没有任何可见字段时标题一并藏起。
  // 不认模块字段，只认「组里还有没有看得见的控件」这一条通用规则。
  function applyGroups(entry) {
    if (!Array.isArray(entry.nodes.groups)) return;
    entry.nodes.groups.forEach(function (group) {
      var visible = group.keys.some(function (key) {
        var node = entry.nodes.fields.get(key);
        return !!(node && node.visible);
      });
      group.titleEl.style.display = visible ? '' : 'none';
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
      if (node.stepper) node.setLocked(locked);
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

  function build(entry, host, cfg) {
    host.textContent = '';
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
        var titleEl = null;
        if (group.title) {
          titleEl = el('div', 'task-panel-label', group.title);
          root.appendChild(titleEl);
        }
        var keys = [];
        group.fields.forEach(function (field) {
          var node = buildField(entry, field);
          if (!node) return;
          root.appendChild(node.wrap);
          node.noticeEl = field.notice ? buildNotice(entry, field.notice) : null;
          if (node.noticeEl) root.appendChild(node.noticeEl);
          nodes.fields.set(field.key, node);
          keys.push(field.key);
        });
        if (titleEl) nodes.groups.push({ titleEl: titleEl, keys: keys });
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
