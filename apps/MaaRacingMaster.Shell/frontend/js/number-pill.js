/* 数字胶囊（NumberPill）：宿主全部数字字段的唯一控件实现。
 *
 * 外观是一颗胶囊：− / 读数 / + 一体，外高与单行输入框齐平。尺寸只写在 style.css 的
 * token 里（--maarm-control-h / --maarm-num-*），JS 不另存一份像素：列位移交给
 * CSS 的 --slot 换算，编辑框宽度用 `序号 × var(--maarm-num-col-w)` 拼，改 token 两边同时跟。
 *
 * 三种输入方式并存：
 *   1. 点 ± 步进；按住 320ms 后连滚，0.7s 后 ×5、1.4s 后 ×10（节拍 85→55ms）；
 *   2. 点读数进入原地编辑（回车/失焦落定，Esc 撤销），输入框宽度随位数增长；
 *   3. 数字窗竖向拖拽（12px 一格，松手带惯性甩动）、滚轮、方向键 / PageUp·Down / Home·End，
 *      键盘走 spinbutton 语义。
 * 动效：整数位逐列滚动（rAF 弹簧，高位错峰 32ms 起步，速度越大模糊越重），位数变化时
 * 列宽收展；prefers-reduced-motion 下一律瞬时落位。
 * 写后端的时机：一律等动效落定才 onCommit——连滚/拖拽期间只在本地滚，不逐变化发请求。
 * 小数步骤、缺 max 或 min<0 的字段退化为「常驻输入框」档：同一副胶囊、同样的 ± 按住
 * 步进与写入时机，只是数字不滚（列位表达不了小数与负号）。
 *
 * 为什么单独成文件：数字字段出现在两处渲染点（插件面板 `js/config-panel.js` 与内置步骤
 * `js/run.js`），控件放一处两边才有同一副外观、同一套交互与同一种写入时机，改尺寸或
 * 改手感只动这份文件与 style.css 的 token。
 * 纯逻辑（夹取 / 列数 / 能否滚 / 读屏文本）挂在 MaaRM.numberPill.model 上，由
 * tests/js/number_pill_model.test.mjs 覆盖；本文件其余部分碰 DOM，不参与 node 测试。
 */
(function () {
  'use strict';

  // ---------- 纯逻辑（不碰 DOM，node 可直接测） ----------
  var NUM = {
    /** 夹取：非数值回落到 min（min 由调用方保证是数字） */
    clamp: function (value, min, max) {
      var v = Number(value);
      if (!Number.isFinite(v)) v = min;
      if (v < min) v = min;
      if (max != null && v > max) v = max;
      return v;
    },
    /** 滚动列数 = max 的整数位数：没有上限就不知道该留几位，所以 max 缺失不给列、只给输入框 */
    placesOf: function (max) {
      if (max == null) return 0;   // null 会被 Number() 变成 0，先挡掉
      var v = Number(max);
      if (!Number.isFinite(v)) return 0;
      return String(Math.max(0, Math.floor(v))).length;
    },
    /** 能否按整数位滚动：上下限与步长都要是整数、且 min 非负（列位里没有符号位） */
    canRoll: function (min, max, step) {
      return Number.isInteger(min) && min >= 0
        && Number.isInteger(max) && max >= min
        && Number.isInteger(step) && step > 0;
    },
    /** 读屏文本：值为 0 且调用方给了 zeroLabel 时读语义（如轮数的「不限」） */
    valueText: function (value, suffix, zeroLabel) {
      if (zeroLabel && value <= 0) return zeroLabel;
      return String(value) + (suffix ? ' ' + suffix : '');
    },
  };

  // ---------- 动效与交互常数（行为参数，不是设计 token：视觉尺寸都在 style.css） ----------
  var CELL_COUNT = 12;          // 数字条 12 格：末尾补 0/1，9→0 向前绕回时不断带
  var STAGGER_MS = 32;          // 列间错峰：高位晚这么久起步，滚出层次感
  var BOUNCE = 40;              // 弹簧弹性（0~100，原控件默认）
  var BLUR = 24;                // 速度模糊强度（0~100，原控件默认）
  var HOLD_DELAY_MS = 320;      // 按住 ± 多久开始连滚
  var SETTLE_COMMIT_MS = 300;   // 无动画档的落定等待：连点只写最后一次
  var FALLBACK_COMMIT_MS = 900; // 落定兜底：rAF 在窗口隐藏/遮挡时会暂停
  var DRAG_PX = 12;             // 拖拽每走一格的像素
  // 单列宽取自 style.css 的同名 token（em 相对读数字号解析，编辑框字号也取同一档）；
  // 不写回退值——回退等于把列宽在这条文件里再存一份
  var COL_W = 'var(--maarm-num-col-w)';

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function stepperBtn(glyph, label) {
    var btn = el('button', 'stepper-btn', glyph);
    btn.type = 'button';
    btn.tabIndex = -1;
    btn.setAttribute('aria-label', label);
    return btn;
  }

  function prefersReducedMotion() {
    return !!(window.matchMedia
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  // opts: value / min / max / step / suffix / label / zeroLabel / locked /
  //       onLive(value) 本地值每变一次就调（说明行的预计用时靠它即时重算）/
  //       onCommit(value) 动效落定才调，同一值只调一次 /
  //       alive() 调用方的代际守卫（面板重建后旧实例就地收）
  function create(opts) {
    var o = opts || {};
    var min = Number.isFinite(Number(o.min)) ? Number(o.min) : 0;
    var max = o.max != null && Number.isFinite(Number(o.max)) ? Number(o.max) : null;
    var step = Number.isFinite(Number(o.step)) && Number(o.step) > 0 ? Number(o.step) : 1;
    var suffix = o.suffix ? String(o.suffix) : '';
    var zeroLabel = o.zeroLabel ? String(o.zeroLabel) : '';
    var labelText = o.label ? String(o.label) : '';
    var roll = NUM.canRoll(min, max, step);

    var pill = el('div', 'maarm-stepper');
    var dec = stepperBtn('\u2212', '减少');
    var inc = stepperBtn('+', '增加');
    var win = el('div', 'stepper-window');
    var edit = el('input', 'stepper-edit');
    edit.type = 'text';
    edit.inputMode = 'numeric';
    if (labelText) edit.setAttribute('aria-label', labelText);
    win.appendChild(edit);

    var columns = [];
    if (roll) {
      win.tabIndex = 0;
      win.setAttribute('role', 'spinbutton');
      if (labelText) win.setAttribute('aria-label', labelText);
      win.setAttribute('aria-valuemin', String(min));
      win.setAttribute('aria-valuemax', String(max));
      var places = NUM.placesOf(max);
      for (var i = 0; i < places; i += 1) {
        var p = places - 1 - i;   // DOM 从高位列到低位列；columns 仍按位权 p 索引
        var colBox = el('div', 'stepper-col');
        var strip = el('div', 'stepper-strip');
        for (var d = 0; d < CELL_COUNT; d += 1) {
          strip.appendChild(el('div', 'stepper-num', String(d % 10)));
        }
        colBox.appendChild(strip);
        win.appendChild(colBox);
        columns[p] = { box: colBox, strip: strip, pos: 0, vel: 0, target: 0,
                       due: [], next: [] };
      }
    } else {
      win.classList.add('stepper-window--plain');
    }
    if (suffix) win.appendChild(el('span', 'stepper-suffix', suffix));

    pill.appendChild(dec);
    pill.appendChild(win);
    pill.appendChild(inc);

    var shown = null;        // 本地显示值；落定前可与后端配置暂时不同
    var committed = null;    // 已写/已知的后端值：落定时相同就不再发请求
    var destroyed = false;
    var rafId = 0;
    var lastTime = 0;
    var flingRaf = 0;
    var commitTimer = 0;
    var editing = false;

    function alive() {
      if (destroyed) return false;
      return typeof o.alive === 'function' ? !!o.alive() : true;
    }
    function lockedNow() { return !!o.locked; }
    function live() {
      if (typeof o.onLive === 'function' && shown != null) o.onLive(shown);
    }
    function valueOf(raw) {
      var v = NUM.clamp(raw, min, max);
      return roll ? Math.round(v) : v;
    }

    function fitEditWidth() {
      var len = Math.max(1, String(edit.value == null ? '' : edit.value).length);
      edit.style.width = 'calc(' + len + ' * ' + COL_W + ' + 8px)';
    }

    function paint() {
      columns.forEach(function (col) {
        var mod = ((col.pos % 10) + 10) % 10;
        var smear = Math.min(2.2, Math.abs(col.vel) * 1.8) * (BLUR / 50);
        col.strip.style.setProperty('--slot', String(mod));
        col.strip.style.filter = smear > 0.12
          ? 'blur(' + smear.toFixed(2) + 'px)' : 'none';
      });
    }

    function syncVisibility(value) {
      var sig = Math.max(1, String(value).length);
      columns.forEach(function (col, p) {
        col.box.classList.toggle('stepper-col--hidden', p >= sig);
      });
    }

    function syncAria() {
      if (!roll || shown == null) return;
      win.setAttribute('aria-valuenow', String(shown));
      win.setAttribute('aria-valuetext', NUM.valueText(shown, suffix, zeroLabel));
    }

    function flush() {
      commitTimer = 0;
      if (!alive() || lockedNow() || shown == null || shown === committed) return;
      committed = shown;
      if (typeof o.onCommit === 'function') o.onCommit(shown);
    }
    function flushNow() {
      clearTimeout(commitTimer);
      commitTimer = 0;
      flush();
    }
    function scheduleCommit(ms) {
      clearTimeout(commitTimer);
      commitTimer = setTimeout(flush, ms);
    }

    // 把数字列推到 value：instant=false 走弹簧（高位错峰、快速连改在队列里排队不跳变）
    function updateColumns(value, animate) {
      var instant = !animate || prefersReducedMotion();
      columns.forEach(function (col, p) {
        var to = Math.floor(value / Math.pow(10, p));
        if (instant) {
          col.pos = to;
          col.target = to;
          col.vel = 0;
          col.due = [];
          col.next = [];
        } else {
          col.due.push(performance.now() + p * STAGGER_MS);
          col.next.push(to);
        }
      });
      if (instant) paint();
      else runSimulation();
      return instant;
    }

    function tick(time) {
      if (!alive()) {   // 面板重建：孤儿循环就地收
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
        flush();   // 动画落定才写后端（连滚期间只滚不写）
      }
    }

    function runSimulation() {
      if (!rafId) {
        lastTime = 0;
        rafId = requestAnimationFrame(tick);
      }
    }

    // 用户驱动的改值（± / 拖拽 / 滚轮 / 键盘 / 原地编辑落定）
    function applyUser(raw) {
      var next = valueOf(raw);
      if (next === shown) {
        if (!roll) { edit.value = String(next); fitEditWidth(); }
        return;
      }
      shown = next;
      if (!roll) {
        edit.value = String(next);
        fitEditWidth();
        scheduleCommit(SETTLE_COMMIT_MS);
      } else {
        var instant = updateColumns(next, true);
        syncVisibility(next);
        scheduleCommit(instant ? SETTLE_COMMIT_MS : FALLBACK_COMMIT_MS);
      }
      syncAria();
      live();
    }

    // 后端值（面板刷新 / 轮询）：以它为落定基线，动效走完也不回写
    function setValue(raw) {
      if (raw == null || !Number.isFinite(Number(raw))) return;
      var next = valueOf(raw);
      committed = next;
      var mount = shown == null;
      if (mount) {
        shown = next;
        if (!roll) {
          edit.value = String(next);
        } else {
          updateColumns(next, false);
          syncVisibility(next);
        }
        fitEditWidth();
        syncAria();
        live();
        return;
      }
      if (next === shown) {
        syncAria();
        live();
        return;
      }
      shown = next;
      if (!roll) {
        if (document.activeElement !== edit) edit.value = String(next);
        fitEditWidth();
      } else {
        var instant = updateColumns(next, true);
        syncVisibility(next);
        if (!instant) scheduleCommit(FALLBACK_COMMIT_MS);
      }
      syncAria();
      live();
    }

    function bump(multiplier) {
      if (shown == null) return;
      applyUser(shown + multiplier * step);
    }

    // 落成输入框里的值并立即写（回车 / 失焦）。解析不出来就退回显示值。
    // 小数步骤的字段（number 允许浮点）走这里，所以按浮点解析、只顺带容忍逗号小数点。
    function commitTyped() {
      var text = String(edit.value == null ? '' : edit.value).trim().replace(',', '.');
      var value = text === '' ? NaN : Number(text);
      if (!Number.isFinite(value)) {
        edit.value = String(shown == null ? min : shown);
        fitEditWidth();
        return;
      }
      applyUser(value);
      edit.value = String(shown);
      fitEditWidth();
      flushNow();
    }

    // 原地编辑（滚动档）：点读数把滚动的列换成输入框，输入宽度随位数增长。
    // 编辑期间不参与拖拽/滚轮/方向键，避免两套输入同时改值。
    function startEdit() {
      if (!roll || editing || lockedNow() || shown == null) return;
      editing = true;
      win.classList.add('stepper-window--editing');
      edit.value = String(shown);
      fitEditWidth();
      // 先让新样式落地再聚焦：display:none 期间的 focus() 会被浏览器丢掉，
      // 点一下数字却没拿到输入焦点，后续键入就落空了
      void edit.offsetWidth;
      edit.focus();
      edit.select();
    }
    function stopEdit(apply) {
      if (!editing) return;
      editing = false;
      win.classList.remove('stepper-window--editing');
      if (apply) commitTyped();
      else if (shown != null) edit.value = String(shown);
      fitEditWidth();
      syncVisibility(shown == null ? min : shown);
    }

    edit.addEventListener('input', fitEditWidth);
    edit.addEventListener('keydown', function (event) {
      if (!roll) return;   // 常驻输入档的 Enter/Esc 由窗口统一处理（方向键留给光标）
      event.stopPropagation();
      if (event.key === 'Enter') {
        event.preventDefault();
        stopEdit(true);
      } else if (event.key === 'Escape') {
        event.preventDefault();
        stopEdit(false);
      }
    });
    edit.addEventListener('blur', function () {
      if (roll && editing) stopEdit(true);
      else if (!roll) commitTyped();
    });
    // 点进输入框即全选：改值不用先把旧值删掉
    edit.addEventListener('focus', function () { edit.select(); });

    // ± 按住连滚：0.7s 后 ×5、1.4s 后 ×10，节拍 85ms → 55ms
    function bindHold(btn, dir) {
      var holdTimer = 0;
      var holdStart = 0;
      function repeat() {
        if (lockedNow()) { holdTimer = 0; return; }
        var elapsed = performance.now() - holdStart;
        var speedUp = elapsed > 1400 ? 10 : elapsed > 700 ? 5 : 1;
        bump(dir * speedUp);
        holdTimer = setTimeout(repeat, elapsed > 700 ? 55 : 85);
      }
      function stop() {
        if (holdTimer) { clearTimeout(holdTimer); holdTimer = 0; }
      }
      btn.addEventListener('pointerdown', function (event) {
        if (lockedNow()) return;
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
        if (event.detail === 0 && !lockedNow()) bump(dir);
      });
    }
    bindHold(dec, -1);
    bindHold(inc, 1);

    win.addEventListener('keydown', function (event) {
      if (lockedNow() || shown == null) return;
      if (!roll) {
        // 常驻输入档：Enter 落定、Esc 撤销，上下键步进（左右键留给光标）
        if (event.key === 'Enter') {
          event.preventDefault();
          commitTyped();
        } else if (event.key === 'Escape') {
          event.preventDefault();
          edit.value = String(shown);
          fitEditWidth();
          edit.blur();
        } else if (event.key === 'ArrowUp') {
          event.preventDefault();
          bump(1);
        } else if (event.key === 'ArrowDown') {
          event.preventDefault();
          bump(-1);
        }
        return;
      }
      if (editing) return;
      if (event.key === 'Enter' || event.key === 'F2') {
        event.preventDefault();
        startEdit();
        return;
      }
      var handled = true;
      switch (event.key) {
        case 'ArrowUp': case 'ArrowRight': bump(1); break;
        case 'ArrowDown': case 'ArrowLeft': bump(-1); break;
        case 'PageUp': bump(10); break;
        case 'PageDown': bump(-10); break;
        case 'Home': applyUser(min); break;
        case 'End': applyUser(max); break;
        default: handled = false;
      }
      if (handled) event.preventDefault();
    });

    if (roll) {
      win.addEventListener('wheel', function (event) {
        if (lockedNow() || editing) return;
        event.preventDefault();
        bump(event.deltaY < 0 ? 1 : -1);
      }, { passive: false });
    }

    // 竖向拖拽 + 惯性甩动（12px 一格，松手按速度衰减续滚）；
    // 位移不足 4px 视为点击，进入原地编辑。常驻输入档不接管指针，输入框自己选字。
    var drag = null;
    function offsetTicks(acc) {
      var ticks = acc > 0 ? Math.floor(acc / DRAG_PX) : Math.ceil(acc / DRAG_PX);
      return { ticks: ticks, rest: acc - ticks * DRAG_PX };
    }
    if (roll) {
      win.addEventListener('pointerdown', function (event) {
        if (lockedNow() || editing) return;
        cancelAnimationFrame(flingRaf);
        try { win.setPointerCapture(event.pointerId); } catch (e) { /* 无碍 */ }
        drag = { y: event.clientY, acc: 0, vel: 0, time: performance.now(),
                 moved: false, dist: 0 };
      });
      win.addEventListener('pointermove', function (event) {
        if (!drag) return;
        var now = performance.now();
        var dy = drag.y - event.clientY;
        drag.y = event.clientY;
        drag.dist += Math.abs(dy);
        if (drag.dist > 4) drag.moved = true;
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
        if (!track.moved) {
          startEdit();
          return;
        }
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
    }

    function setLocked(isLocked) {
      o.locked = !!isLocked;
      if (o.locked && editing) stopEdit(false);
      dec.disabled = o.locked;
      inc.disabled = o.locked;
      edit.disabled = o.locked;
      pill.classList.toggle('maarm-stepper--locked', o.locked);
    }

    function destroy() {
      destroyed = true;
      if (rafId) cancelAnimationFrame(rafId);
      rafId = 0;
      if (flingRaf) cancelAnimationFrame(flingRaf);
      flingRaf = 0;
      clearTimeout(commitTimer);
      commitTimer = 0;
    }

    edit.value = '';
    // 建出来就带锁（面板锁定时整颗胶囊先呈灰态，不等第一次 setLocked）
    setLocked(!!o.locked);
    // 初值走 setValue：它同时把后端基线记下，挂载时不会回写一次
    if (o.value != null) setValue(o.value);

    return {
      el: pill,
      setValue: setValue,
      setLocked: setLocked,
      destroy: destroy,
    };
  }

  window.MaaRM = window.MaaRM || {};
  window.MaaRM.numberPill = { create: create, model: NUM };
})();
