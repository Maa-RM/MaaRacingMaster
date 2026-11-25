/* 首次引导：声明、过渡页、聚光灯步骤。
 * 状态由后端 profile 持有（get_onboarding / onboarding_event），本文件不落盘；
 * 每完成一步立即上报，强杀后最多重看一步。 */
(function () {
  'use strict';

  var READ_MS = 5000;
  // 与 sidecar._ONBOARDING_STEPS 一一对应
  var STEP_IDS = ['manage', 'add_start_game', 'add_close_game', 'confirm', 'open_settings', 'open_env'];

  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var delay = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };

  var state = {
    termsAccepted: false,
    launchSeen: false,
    stepsDone: [],
    finishedAt: null,
    existingUser: false
  };
  var countdown = { raf: 0, active: false, done: false };
  var tourIndex = 0;
  var tourActive = false;

  // ---------- 后端状态 ----------
  // 方法名保持字符串字面量：白名单测试按字面量扫前端调用点，抽不出名字就等于失守
  async function load() {
    if (!window.maarm || !maarm.call) throw new Error('RPC 不可用');
    var s = await maarm.call('get_onboarding', {});
    state.termsAccepted = !!s.terms_accepted;
    state.launchSeen = !!s.launch_seen;
    state.stepsDone = Array.isArray(s.steps_done) ? s.steps_done.slice() : [];
    state.finishedAt = s.finished_at || null;
    state.existingUser = !!s.existing_user;
  }

  function report(type, extra) {
    if (!window.maarm || !maarm.call) return Promise.reject(new Error('RPC 不可用'));
    return maarm.call('onboarding_event', Object.assign({ type: type }, extra || {}));
  }

  function termsAccepted() {
    return state.termsAccepted;
  }

  function stepDone(id) {
    if (state.stepsDone.indexOf(id) !== -1) return;
    state.stepsDone.push(id);
    report('step_done', { id: id }).catch(function (e) {
      console.warn('[onboarding] 步骤状态未保存', e);
    });
  }

  // ---------- 真实前端的页面操作 ----------
  function activeTab() {
    var el = $('.maarm-tab--active');
    return el ? el.dataset.tab : '';
  }
  function activateTab(name) {
    if (activeTab() === name) return;
    var btn = $('.maarm-tab[data-tab="' + name + '"]');
    if (btn) btn.click();
  }
  function closeModals() {
    // 复用真实前端的 Esc 关闭路径，保留它自己的清理与焦点归还
    var guard = 0;
    while ($('.maarm-modal-overlay') && guard < 4) {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      guard += 1;
      // 关窗动画 160ms；同步等不了，交给调用方的 delay 收尾
    }
  }
  function manageOpen() {
    return !!$('.task-gallery-cell[data-manage]');
  }
  function envOpen() {
    return !!$('.env-tabs');
  }

  // ---------- 声明门 ----------
  function setShellInert(on) {
    ['header', 'main', 'footer'].forEach(function (sel) {
      var el = document.querySelector(sel);
      if (el) el.inert = on;
    });
  }

  function startCountdown(btn, label) {
    if (countdown.active || countdown.done) return;
    countdown.active = true;
    var t0 = performance.now();
    function tick(now) {
      if (!countdown.active) return;
      var p = Math.min(1, (now - t0) / READ_MS);
      var left = Math.max(1, Math.ceil((1 - p) * READ_MS / 1000));
      label.textContent = p >= 1 ? '同意并继续' : '请阅读 ' + left + ' 秒';
      if (p >= 1) { unlock(btn, label); return; }
      countdown.raf = requestAnimationFrame(tick);
    }
    countdown.raf = requestAnimationFrame(tick);
  }
  function unlock(btn, label) {
    countdown.active = false;
    countdown.done = true;
    cancelAnimationFrame(countdown.raf);
    btn.disabled = false;
    label.textContent = '同意并继续';
  }

  // ---------- 声明页 hero：Line Waves（按 ReactBits 源码移植） ----------
  // 参数：lineCount=105 / speed=0.16 / glow=0.35 / warpIntensity=1.05 /
  // rotation=-55 / highlightWidth=0.4 / warpScale=0.6，颜色不循环、不接鼠标。
  // 线条数按画布高度折算，不同尺寸下保持同一视觉密度。
  var HERO_LINE_COUNT = 105;
  var HERO_BASE_HEIGHT = 380;   // 105 条线对应的参考画布高度

  function initHeroShader(canvas) {
    if (!canvas) return null;
    var gl = canvas.getContext('webgl2', {
      alpha: true, premultipliedAlpha: true, antialias: false,
      depth: false, stencil: false, powerPreference: 'high-performance'
    });
    if (!gl) return null;

    var vsSrc = '#version 300 es\n'
      + 'const vec2 corners[3] = vec2[3](vec2(-1.0,-1.0), vec2(3.0,-1.0), vec2(-1.0,3.0));\n'
      + 'void main(){ gl_Position = vec4(corners[gl_VertexID], 0.0, 1.0); }';

    var fsSrc = '#version 300 es\n' + [
      'precision highp float;',
      'uniform vec2 uResolution;',
      'uniform float uPixel;',
      'uniform float uTime;',
      'uniform float uLines;',
      'uniform float uWidth;',
      'uniform float uGlow;',
      'uniform float uHighlightWidth;',
      'uniform float uWarp;',
      'uniform float uScale;',
      'uniform float uRotation;',
      'out vec4 fragColor;',
      'vec2 rotate(vec2 p, float a){ float c=cos(a), s=sin(a); return vec2(c*p.x-s*p.y, s*p.x+c*p.y); }',
      'float hash(vec2 p){ p=fract(p*vec2(123.34,456.21)); p+=dot(p,p+45.32); return fract(p.x*p.y); }',
      'float noise(vec2 p){',
      '  vec2 i=floor(p), f=p-i;',
      '  vec2 w=f*f*f*(f*(f*6.0-15.0)+10.0);',
      '  return mix(mix(hash(i),hash(i+vec2(1.0,0.0)),w.x), mix(hash(i+vec2(0.0,1.0)),hash(i+vec2(1.0,1.0)),w.x), w.y);',
      '}',
      'float bend(vec2 p, float t){',
      '  vec2 q = p * 2.0 / uScale;',
      '  float w = 0.6*sin(q.x*0.55 + t*0.4 + 0.6*sin(q.y*0.9 - t*0.28));',
      '  w += 0.21*sin(q.x*1.3 - t*0.33 + q.y*0.8 + 1.3);',
      '  w += 0.12*sin(q.y*2.3 + q.x*0.9 - t*0.37 + 0.4);',
      '  return w * uWarp * uScale * 0.46;',
      '}',
      'float river(vec2 p, float y, float t){',
      '  float drift = 0.7*(0.18*sin(p.x*1.6 - t*0.33 + 0.7) + 0.1*sin(p.x*3.1 + t*0.21));',
      '  float center = 0.5*(0.2*sin(t*0.17) + 0.08*sin(t*0.29 + 1.3));',
      '  float width = 0.13 * uHighlightWidth;',
      '  float near = (y - center - drift) / width;',
      '  float far = (y + 0.22*sin(t*0.13 + 2.0) - drift*0.6 + 0.3) / (width*0.55);',
      '  return max(exp(-near*near), exp(-far*far)*0.7);',
      '}',
      'void main(){',
      '  vec2 p = rotate((gl_FragCoord.xy - 0.5*uResolution)/uResolution.y, uRotation);',
      '  float t = uTime;',
      '  float w = bend(p, t);',
      '  float y = p.y + w;',
      '  float phi = y * uLines;',
      '  float fw = max(fwidth(phi), 1e-5);',
      '  float crowd = smoothstep(0.18, 0.5, fw);',
      '  float fold = smoothstep(1.3, 2.2, fw * uResolution.y / uLines);',
      '  float shimmer = mix(1.0, smoothstep(0.15,0.85, noise(vec2(y*1.6 + t*0.07, p.x*0.55 - t*0.22))), 0.5);',
      '  float lit = max(river(p, y, t), fold*0.9) * shimmer;',
      // 关键：亮带外只有 7% 亮度，其余线条几乎不可见
      '  float light = 0.07 + 0.93*lit;',
      '  float dist = abs(fract(phi + 0.5) - 0.5) / fw;',
      '  float width = uWidth * uPixel * (1.0 + 1.5*lit);',
      '  float cover = clamp(min(dist + width*0.5, 0.5) - max(dist - width*0.5, -0.5), 0.0, 1.0);',
      '  cover = mix(cover, min(width*fw, 1.0), crowd);',
      '  float halo = exp(-dist/(2.2*uPixel)) * (1.0-crowd) * uGlow;',
      '  float thread = mix(1.0, 0.55 + 0.45*hash(vec2(floor(phi+0.5), 7.3)), 0.45*(1.0-crowd));',
      '  float strength = (cover + halo) * thread * light;',
      '  vec3 color = vec3(strength);',
      '  float alpha = max(color.r, max(color.g, color.b));',
      '  fragColor = vec4(min(color, vec3(alpha)), alpha);',
      '}'
    ].join('\n');

    function compile(type, src) {
      var s = gl.createShader(type);
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
        console.warn('[hero] shader 编译失败', gl.getShaderInfoLog(s));
        return null;
      }
      return s;
    }
    var vs = compile(gl.VERTEX_SHADER, vsSrc);
    var fs = compile(gl.FRAGMENT_SHADER, fsSrc);
    if (!vs || !fs) return null;
    var prog = gl.createProgram();
    gl.attachShader(prog, vs);
    gl.attachShader(prog, fs);
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      console.warn('[hero] program link 失败', gl.getProgramInfoLog(prog));
      return null;
    }
    gl.useProgram(prog);

    var vao = gl.createVertexArray();
    gl.bindVertexArray(vao);

    var u = {
      resolution: gl.getUniformLocation(prog, 'uResolution'),
      pixel: gl.getUniformLocation(prog, 'uPixel'),
      time: gl.getUniformLocation(prog, 'uTime'),
      lines: gl.getUniformLocation(prog, 'uLines'),
      width: gl.getUniformLocation(prog, 'uWidth'),
      glow: gl.getUniformLocation(prog, 'uGlow'),
      highlightWidth: gl.getUniformLocation(prog, 'uHighlightWidth'),
      warp: gl.getUniformLocation(prog, 'uWarp'),
      scale: gl.getUniformLocation(prog, 'uScale'),
      rotation: gl.getUniformLocation(prog, 'uRotation')
    };
    var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    var raf = 0;
    var running = true;
    var time = Math.random() * 60;
    var last = 0;
    var cssW = 1;
    var cssH = 1;
    var ratio = 1;

    function resize() {
      var rect = canvas.getBoundingClientRect();
      cssW = Math.max(1, rect.width);
      cssH = Math.max(1, rect.height);
      ratio = Math.min(window.devicePixelRatio || 1, 2, Math.sqrt(2400000 / (cssW * cssH)));
      var w = Math.max(1, Math.round(cssW * ratio));
      var h = Math.max(1, Math.round(cssH * ratio));
      if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w;
        canvas.height = h;
      }
    }

    function draw() {
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.uniform2f(u.resolution, canvas.width, canvas.height);
      gl.uniform1f(u.pixel, ratio);
      gl.uniform1f(u.time, time);
      // 线条数随框高折算：与参考画布保持同一视觉密度
      gl.uniform1f(u.lines, HERO_LINE_COUNT * (cssH / HERO_BASE_HEIGHT));
      gl.uniform1f(u.width, 1.3);
      gl.uniform1f(u.glow, 0.35);
      gl.uniform1f(u.highlightWidth, 0.4);
      gl.uniform1f(u.warp, 1.05);
      gl.uniform1f(u.scale, 0.6);
      gl.uniform1f(u.rotation, -55 * Math.PI / 180);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }

    function frame(now) {
      if (!running) return;
      var dt = last ? Math.min(Math.max((now - last) / 1000, 0), 0.05) : 0;
      last = now;
      time += dt * 0.16 * 2.5 * (reduce ? 0.25 : 1);
      resize();
      draw();
      raf = requestAnimationFrame(frame);
    }

    resize();
    draw();
    if (!reduce) raf = requestAnimationFrame(frame);

    return {
      stop: function () { running = false; cancelAnimationFrame(raf); }
    };
  }

  function showGate() {
    var gate = document.createElement('div');
    gate.className = 'onb-gate';
    gate.id = 'onb-gate';
    gate.innerHTML =
      '<div class="onb-gate-inner">'
      + '<div class="onb-hero">'
      + '<canvas class="onb-hero-canvas" id="onb-hero-canvas"></canvas>'
      + '<div class="onb-hero-mark"><img src="maarm_icon.png" alt=""><span>MaaRM</span></div>'
      + '</div>'
      + '<h1>使用声明</h1>'
      + '<div class="onb-gate-text">'
      + '<p>本程序仅用于个人学习与技术研究，禁止任何形式的商业使用，包括但不限于售卖、租赁、付费代跑与商业展示。</p>'
      + '<p>使用本程序即表示你已了解：自动化操作可能违反游戏服务条款，账号风险与由此产生的后果由使用者自行承担。</p>'
      + '<p>本程序按「现状」提供，不对可用性、稳定性或特定用途适用性作任何明示或暗示的担保。</p>'
      + '<p>程序内使用的开源组件、字体与图标依其各自许可分发，完整清单见「关于」页的第三方许可说明。</p>'
      + '<p class="onb-gate-note">继续前请确认：你已年满法定年龄，并自行判断所在地区的法律与服务条款要求。</p>'
      + '</div>'
      + '<div class="onb-gate-actions">'
      + '<button class="tour-text-btn" type="button" id="onb-decline">不同意并退出</button>'
      + '<button class="tour-btn tour-btn--primary" type="button" id="onb-accept" disabled>'
      + '<span id="onb-accept-label">请阅读 5 秒</span>'
      + '</button>'
      + '</div>'
      + '</div>';
    document.body.appendChild(gate);
    var hero = initHeroShader($('#onb-hero-canvas'));
    setShellInert(true);

    var btn = $('#onb-accept');
    var label = $('#onb-accept-label');
    btn.addEventListener('click', async function () {
      if (btn.disabled) return;
      // 立刻锁住：保存回包期间再点一次会发两笔请求、叠出两层过渡页
      btn.disabled = true;
      try {
        await report('accept_terms');
      } catch (e) {
        console.warn('[onboarding] 声明状态保存失败', e);
        btn.disabled = false;
        label.textContent = '保存失败，请重试';
        return;
      }
      state.termsAccepted = true;
      countdown.active = false;
      cancelAnimationFrame(countdown.raf);
      if (hero) hero.stop();
      // 老用户只补声明；已完成过引导的人（例如声明改版重签）同样不重放
      if (state.existingUser || state.finishedAt) {
        gate.classList.add('is-leaving');
        setShellInert(false);
        setTimeout(function () { gate.remove(); }, 320);
        return;
      }
      // 声明页内容先淡出，过渡页从同色背景上淡入，背景始终不透明
      gate.classList.add('is-leaving');
      setShellInert(false);
      showLaunch(function () { gate.remove(); });
    });
    $('#onb-decline').addEventListener('click', function () {
      countdown.active = false;
      cancelAnimationFrame(countdown.raf);
      if (hero) hero.stop();
      showExit();
      // 「不同意并退出」就是要关程序：交给宿主关窗；浏览器直开时退回下面的提示页
      try { window.MaaRM.postWindowAction('close'); } catch (e) { /* 无宿主 */ }
    });

    startCountdown(btn, label);
  }

  function showExit() {
    var exit = document.createElement('div');
    exit.className = 'onb-gate';
    exit.style.background = '#15181F';
    exit.innerHTML =
      '<div class="onb-gate-inner" style="max-width:360px;text-align:center">'
      + '<h1 style="color:#F3F4F6;font-size:19px">已退出</h1>'
      + '<p style="color:#9CA3AF;font-size:12.5px;line-height:1.75;margin-bottom:18px">'
      + '未同意使用声明，程序无法继续。窗口正在关闭；重新打开后仍会看到这份声明。</p>'
      + '<button class="tour-btn tour-btn--primary" type="button" id="onb-restart">重新打开</button>'
      + '</div>';
    document.body.appendChild(exit);
    // 宿主没接住关窗指令时（浏览器直开）给一个自恢复入口
    $('#onb-restart').addEventListener('click', function () {
      var win = $('#onb-gate');
      if (win) win.remove();
      exit.remove();
      setShellInert(false);
      showGate();
    });
  }

  // ---------- 聚光灯步骤 ----------
  var STEPS = [
    {
      target: '#btn-manage',
      title: '先把要跑的任务加进来',
      body: '点右上角的按钮，选这次要自动做的事。',
      prepare: async function () { closeModals(); activateTab('control'); },
      wait: 260,
      done: function () { return manageOpen(); }
    },
    {
      target: '.task-gallery-cell[data-manage="launch_game"]',
      title: '加上「启动游戏」',
      body: '它会打开游戏，等到进入大厅后，再交给下一个任务。',
      prepare: async function () { activateTab('control'); if (!manageOpen()) { var b = $('#btn-manage'); if (b) b.click(); } },
      wait: 320,
      // 加任务要等 set_run_queue 回包并重绘，慢机器上可能超过 1.8 秒
      resultMs: 6000,
      done: function () { return window.MaaRM.state.queue.order.indexOf('launch_game') !== -1; }
    },
    {
      target: '.task-gallery-cell[data-manage="close_game"]',
      title: '再加上「关闭游戏」',
      body: '所有任务都跑完后，它才会关掉游戏。',
      prepare: async function () { activateTab('control'); if (!manageOpen()) { var b = $('#btn-manage'); if (b) b.click(); } },
      wait: 320,
      resultMs: 6000,
      done: function () { return window.MaaRM.state.queue.order.indexOf('close_game') !== -1; }
    },
    {
      target: '.maarm-modal-overlay .maarm-modal-btn--primary',
      title: '确认后关掉这个窗口',
      body: '列表从上往下依次执行；「完成」只是关闭窗口，不会开始运行。',
      prepare: async function () { activateTab('control'); if (!manageOpen()) { var b = $('#btn-manage'); if (b) b.click(); } },
      wait: 320,
      done: function () { return !$('.maarm-modal-overlay'); }
    },
    {
      target: '.maarm-tab[data-tab="settings"]',
      title: '再去看设置页',
      body: '运行环境不在主控页，在顶部的「设置」里。',
      prepare: async function () { closeModals(); activateTab('control'); },
      wait: 480,
      done: function () { return activeTab() === 'settings'; }
    },
    {
      target: '.maarm-tool-btn[data-tool="env"]',
      title: '这里是运行环境',
      body: '系统权限、手柄驱动和运行组件都放在这里。不改也能先跑启动和关闭。',
      prepare: async function () { activateTab('settings'); },
      wait: 520,
      // 环境中心首次打开要查一遍本机组件，RPC 往返可能超过 1.8 秒
      resultMs: 8000,
      done: function () { return envOpen(); }
    }
  ];

  function waitFor(fn, ms) {
    return new Promise(function (resolve) {
      var t0 = performance.now();
      (function loop() {
        var ok = false;
        try { ok = !!fn(); } catch (e) { ok = false; }
        if (ok) return resolve(true);
        if (performance.now() - t0 > ms) return resolve(false);
        setTimeout(loop, 60);
      })();
    });
  }

  function currentTarget() {
    var step = STEPS[tourIndex];
    return step ? document.querySelector(step.target) : null;
  }

  function setBox(el, box) {
    if (!el) return;
    el.style.left = box.left + 'px';
    el.style.top = box.top + 'px';
    el.style.width = box.width + 'px';
    el.style.height = box.height + 'px';
  }

  // ---------- 状态球向导 ----------
  // 复用底栏那颗会看人、会眨眼的状态球：引导时它从状态栏起飞，落到当前目标旁边；
  // 结束时飞回原位。原状态球与文字在它离开期间隐藏，避免出现两个球。
  var guideOrb = null;

  function ensureGuideOrb() {
    if (guideOrb) return guideOrb;
    var src = document.getElementById('status-dot');
    if (!src) return null;
    var wrap = document.createElement('div');
    wrap.className = 'tour-orb';
    wrap.id = 'onb-orb';
    wrap.setAttribute('aria-hidden', 'true');
    var clone = src.cloneNode(true);
    clone.removeAttribute('id');
    // 原球在引导开始时已被隐藏，克隆体会连内联 visibility 一起复制，必须清掉
    clone.style.visibility = '';
    // 克隆体里的渐变 / 裁剪 id 要改成唯一值，否则会和状态栏那颗互相引用
    clone.innerHTML = clone.innerHTML
      .replace(/status-orb-clip/g, 'status-orb-clip-onb')
      .replace(/status-orb-grad/g, 'status-orb-grad-onb');
    wrap.appendChild(clone);
    document.body.appendChild(wrap);
    if (window.StatusOrb) {
      try { window.StatusOrb.mount(clone); } catch (e) { /* 忽略 */ }
    }
    var r = src.getBoundingClientRect();
    wrap.style.left = (r.left - 6) + 'px';
    wrap.style.top = (r.top - 6) + 'px';
    void wrap.offsetWidth; // 先落一帧，后面的位移才有过渡
    guideOrb = { el: wrap, started: false };
    return guideOrb;
  }

  function hideStatusChrome(hide) {
    var t = document.getElementById('status-text');
    var d = document.getElementById('status-dot');
    if (t) t.style.visibility = hide ? 'hidden' : '';
    if (d) d.style.visibility = hide ? 'hidden' : '';
  }

  function moveOrbTo(r) {
    var orb = ensureGuideOrb();
    if (!orb) return;
    var size = 30;
    var pad = 10;
    function place() {
      var x = r.left - size - pad;
      var y = r.top - 6;
      if (x < 8) x = r.right + pad;
      if (y < 8) y = 8;
      if (y + size > window.innerHeight - 8) y = window.innerHeight - size - 8;
      orb.el.style.width = size + 'px';
      orb.el.style.height = size + 'px';
      orb.el.style.left = x + 'px';
      orb.el.style.top = y + 'px';
    }
    if (!orb.started) {
      orb.started = true;
      orb.el.classList.add('is-active');
    }
    place();
  }

  function orbLeave(completed) {
    var orb = guideOrb;
    if (!orb) {
      hideStatusChrome(false);
      if (completed) speech('好了，就这些。需要调系统权限或驱动时，在设置里找运行环境。');
      return;
    }
    var src = document.getElementById('status-dot');
    if (src) {
      var r = src.getBoundingClientRect();
      orb.el.classList.add('is-returning');
      orb.el.style.width = r.width + 'px';
      orb.el.style.height = r.height + 'px';
      orb.el.style.left = r.left + 'px';
      orb.el.style.top = r.top + 'px';
    }
    setTimeout(function () {
      if (guideOrb) {
        guideOrb.el.style.display = 'none';
        guideOrb.el.classList.remove('is-returning');
      }
      hideStatusChrome(false);
      if (completed) speech('好了，就这些。需要调系统权限或驱动时，在设置里找运行环境。');
    }, 620);
  }

  // 结束语贴在状态球上方，像球在说话
  function speech(text) {
    var dot = document.getElementById('status-dot');
    if (!dot) return toast(text);
    var r = dot.getBoundingClientRect();
    var el = document.createElement('div');
    el.className = 'tour-speech';
    el.textContent = text;
    document.body.appendChild(el);
    var left = Math.max(12, Math.min(r.left - 8, window.innerWidth - el.offsetWidth - 12));
    el.style.left = left + 'px';
    el.style.bottom = (window.innerHeight - r.top + 12) + 'px';
    setTimeout(function () { el.classList.add('is-out'); }, 3800);
    setTimeout(function () { el.remove(); }, 4300);
  }

  function layout(el) {
    if (!el) return;
    var r = el.getBoundingClientRect();
    var pad = 6;
    var l = Math.max(0, r.left - pad);
    var t = Math.max(0, r.top - pad);
    var w = r.width + pad * 2;
    var h = r.height + pad * 2;
    var vw = window.innerWidth;
    var vh = window.innerHeight;

    setBox($('[data-block="top"]'), { left: 0, top: 0, width: vw, height: Math.max(0, t) });
    setBox($('[data-block="left"]'), { left: 0, top: t, width: Math.max(0, l), height: h });
    setBox($('[data-block="right"]'), { left: l + w, top: t, width: Math.max(0, vw - l - w), height: h });
    setBox($('[data-block="bottom"]'), { left: 0, top: t + h, width: vw, height: Math.max(0, vh - t - h) });

    var ring = $('#onb-ring');
    ring.style.left = l + 'px';
    ring.style.top = t + 'px';
    ring.style.width = w + 'px';
    ring.style.height = h + 'px';

    var bubble = $('#onb-bubble');
    var bw = Math.min(300, Math.max(180, vw - 24));
    bubble.style.width = bw + 'px';
    var bh = bubble.offsetHeight || 150;
    var below = vh - (t + h);
    var above = below < bh + 28;
    var bTop = above ? (t - bh - 12) : (t + h + 12);
    var bLeft = Math.max(12, Math.min(Math.max(12, l), vw - bw - 12));
    if (bTop < 12) bTop = 12;
    var maxTop = vh - bh - 12;
    if (bTop > maxTop) bTop = Math.max(12, maxTop);
    bubble.style.left = bLeft + 'px';
    bubble.style.top = bTop + 'px';
    bubble.dataset.arrow = above ? 'down' : 'up';
    bubble.style.setProperty('--arrow-x', Math.max(14, Math.min(bw - 24, r.left + r.width / 2 - bLeft - 5)) + 'px');
    moveOrbTo(r);
  }

  var relayoutBound = false;
  function bindRelayout() {
    if (relayoutBound) return;
    relayoutBound = true;
    var pending = 0;
    function relayout() {
      if (!tourActive) return;
      var layer = $('#onb-layer');
      if (!layer || layer.hidden) return;
      var el = currentTarget();
      if (el) layout(el);
    }
    function schedule() {
      cancelAnimationFrame(pending);
      pending = requestAnimationFrame(relayout);
    }
    window.addEventListener('resize', schedule);
    // 捕获阶段监听，页面内部滚动容器也能触发
    document.addEventListener('scroll', schedule, true);
  }

  var targetClickBound = false;
  function bindTargetClick() {
    if (targetClickBound) return;
    targetClickBound = true;
    // 结果还在路上时，同目标的第二次点击必须挡住：加任务类步骤是「点一下加入、
    // 再点一下移出」，慢回包期间连点会把刚加上的任务又点掉，而推进判据读到的是
    // 移除前的队列，最终任务没了、步骤却记成完成。
    document.addEventListener('click', function (e) {
      if (!advancing) return;
      var el = currentTarget();
      if (!el || !el.contains(e.target)) return;
      e.stopImmediatePropagation();
      e.preventDefault();
    }, true);
    // 点击命中高亮目标才算数；命中后等结果状态出现再前进
    document.addEventListener('click', function (e) {
      if (!tourActive) return;
      var layer = $('#onb-layer');
      if (!layer || layer.hidden) return;
      var el = currentTarget();
      if (!el || !el.contains(e.target)) return;
      setTimeout(function () { advance(false); }, 40);
    });
    // 键盘也能把界面带离目标（Tab 切页、Esc 关弹窗）：目标不可见时把界面拉回本步，
    // 结果已经达成就直接前进，别让聚光灯停在不存在的位置上。
    document.addEventListener('keyup', function (e) {
      if (!tourActive) return;
      if (e.key !== 'Tab' && e.key !== 'Escape' && e.key !== 'Enter'
          && e.key !== ' ' && e.key !== 'Spacebar') return;
      setTimeout(function () {
        if (!tourActive) return;
        var step = STEPS[tourIndex];
        var ok = false;
        try { ok = step ? !!step.done() : false; } catch (err) { ok = false; }
        if (ok) { advance(false); return; }
        if (!isVisible(currentTarget())) showStep(tourIndex);
      }, 180);
    });
  }

  function isVisible(el) {
    if (!el) return false;
    var r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  function ensureLayer() {
    var layer = $('#onb-layer');
    if (layer) return layer;
    layer = document.createElement('div');
    layer.className = 'tour-layer';
    layer.id = 'onb-layer';
    layer.hidden = true;
    layer.innerHTML =
      '<div class="tour-block" data-block="top"></div>'
      + '<div class="tour-block" data-block="left"></div>'
      + '<div class="tour-block" data-block="right"></div>'
      + '<div class="tour-block" data-block="bottom"></div>'
      + '<div class="tour-ring" id="onb-ring"></div>'
      + '<div class="tour-bubble" id="onb-bubble" role="dialog" aria-live="polite">'
      + '<div class="tour-step" id="onb-step"></div>'
      + '<h3 id="onb-title"></h3>'
      + '<p id="onb-body"></p>'
      + '<div class="tour-actions">'
      + '<button class="tour-text-btn" type="button" id="onb-skip">跳过本步</button>'
      + '<button class="tour-text-btn" type="button" id="onb-exit">退出引导</button>'
      + '</div>'
      + '</div>';
    document.body.appendChild(layer);
    $$('.tour-block', layer).forEach(function (b) {
      b.addEventListener('click', nudge);
    });
    $('#onb-skip').addEventListener('click', function () { advance(true); });
    $('#onb-exit').addEventListener('click', function () { finish(false); });
    bindTargetClick();
    bindRelayout();
    return layer;
  }

  async function showStep(i) {
    if (i >= STEPS.length) return finish(true);
    var step = STEPS[i];
    // 页面状态已经满足本步（默认队列里已有、续做时提前做过）就不渲染，
    // 否则会闪一下「加上…」再自动跳过
    if (RESULT_CHECKABLE[STEP_IDS[i]]) {
      var satisfied = false;
      try { satisfied = !!step.done(); } catch (e) { satisfied = false; }
      if (satisfied) { stepDone(STEP_IDS[i]); return showStep(i + 1); }
    }
    tourIndex = i;
    var layer = ensureLayer();
    layer.hidden = true;
    try { await step.prepare(); } catch (e) { console.warn(e); }
    // 真实界面的弹窗 / 切页动画不受 snap 影响，必须等它落定再量位置
    await delay(step.wait || 300);
    await waitFor(function () { return !document.querySelector('.maarm-modal-card--enter'); }, 900);
    var el = currentTarget();
    if (!el) {
      console.warn('[tour] 目标不存在，跳过:', step.target);
      return showStep(i + 1);
    }
    $('#onb-step').textContent = '第 ' + (i + 1) + ' 步 / 共 ' + STEPS.length + ' 步';
    $('#onb-title').textContent = step.title;
    $('#onb-body').textContent = step.body;
    $('#onb-bubble').classList.remove('is-nudge');
    layer.hidden = false;
    tourActive = true;
    document.body.classList.add('onb-tour-active');
    layout(el);
    await delay(20);
    layout(el);
    // 弹窗内网格行高可能在进场后还会变一次，稍后再校正
    setTimeout(function () {
      if (tourActive && currentTarget() === el) layout(el);
    }, 220);
    try { el.focus({ preventScroll: true }); } catch (e) { /* 忽略 */ }
    // 页面状态已经满足本步（续做 / 提前做过）：稍等后自动前进，避免卡在已完成的一步
    var already = false;
    try { already = !!step.done(); } catch (e) { already = false; }
    if (already) setTimeout(function () { advance(false); }, 320);
  }

  var advancing = false;
  async function advance(force) {
    if (advancing) return;
    advancing = true;
    try {
      if (!force) {
        var step = STEPS[tourIndex];
        var el = currentTarget();
        if (!el) {
          stepDone(STEP_IDS[tourIndex]);
          showStep(tourIndex + 1);
          return;
        }
        var ok = await waitFor(step.done, step.resultMs || 1800);
        if (!ok) { nudge(); return; }
      }
      stepDone(STEP_IDS[tourIndex]);
      if (tourIndex + 1 >= STEPS.length) { finish(true); return; }
      showStep(tourIndex + 1);
    } finally {
      setTimeout(function () { advancing = false; }, 260);
    }
  }

  function nudge() {
    var b = $('#onb-bubble');
    if (!b) return;
    b.classList.remove('is-nudge');
    void b.offsetWidth;
    b.classList.add('is-nudge');
    setTimeout(function () { b.classList.remove('is-nudge'); }, 340);
  }

  // ---------- 初次教学过渡页 ----------
  // 声明之后、第一步之前：纯色底，大号状态球居中作主体；点「开始」后球缩到引导位置，
  // 背景同时淡出，直接接上第一步。
  function showLaunch(onShown) {
    var orb = ensureGuideOrb();
    if (!orb) {
      if (onShown) onShown();
      startTour(0, true);
      return;
    }

    var launch = document.createElement('div');
    launch.className = 'onb-launch is-entering';
    launch.id = 'onb-launch';
    launch.innerHTML =
      '<div class="onb-launch-stage">'
      + '<div class="onb-launch-spacer" id="onb-launch-spacer"></div>'
      + '<div class="onb-launch-copy">'
      + '<h1>第一次使用？</h1>'
      + '<p>接下来用几步，带你认识主界面。</p>'
      + '<button class="tour-btn tour-btn--primary" type="button" id="onb-launch-start">开始</button>'
      + '</div>'
      + '</div>';
    document.body.appendChild(launch);

    // 球放在占位块的位置，文案紧跟其下，整体作为一组居中
    var sp = $('#onb-launch-spacer').getBoundingClientRect();
    var big = Math.round(sp.width);
    orb.el.style.transition = 'none';
    orb.el.style.width = big + 'px';
    orb.el.style.height = big + 'px';
    orb.el.style.left = sp.left + 'px';
    orb.el.style.top = sp.top + 'px';
    orb.started = true;
    void orb.el.offsetWidth;
    orb.el.classList.add('is-launch');
    orb.el.style.transition = '';

    // 背景淡入
    setTimeout(function () { launch.classList.remove('is-entering'); }, 24);
    // 球稍晚一点浮现
    setTimeout(function () { orb.el.classList.add('is-active'); }, 160);
    if (onShown) setTimeout(onShown, 460);

    $('#onb-launch-start').addEventListener('click', function () {
      if (launch.dataset.leaving) return;
      launch.dataset.leaving = '1';
      report('seen_launch').catch(function (e) { console.warn('[onboarding] 过渡页状态未保存', e); });
      launch.classList.add('is-leaving');

      var target = document.querySelector(STEPS[0].target);
      var r = target ? target.getBoundingClientRect() : null;
      var vw = window.innerWidth;
      var vh = window.innerHeight;
      var size = 30;
      var pad = 10;
      var x = r ? r.left - size - pad : vw / 2 - size / 2;
      var y = r ? r.top - 6 : vh / 2 - size / 2;
      if (r && x < 8) x = r.right + pad;
      if (y < 8) y = 8;
      if (y + size > vh - 8) y = vh - size - 8;
      orb.el.style.width = size + 'px';
      orb.el.style.height = size + 'px';
      orb.el.style.left = x + 'px';
      orb.el.style.top = y + 'px';

      setTimeout(function () {
        launch.remove();
        orb.el.classList.remove('is-launch');
        startTour(0);
      }, 720);
    });
  }

  function startTour(from) {
    if (state.finishedAt) return;
    tourIndex = Math.max(0, from || 0);
    // 从过渡页接过来时，球可能还带着大尺寸 / 高层级
    if (guideOrb) {
      guideOrb.el.classList.remove('is-launch');
      guideOrb.el.style.width = '30px';
      guideOrb.el.style.height = '30px';
    }
    hideStatusChrome(true);
    showStep(tourIndex);
  }

  function finish(completed) {
    tourActive = false;
    document.body.classList.remove('onb-tour-active');
    var layer = $('#onb-layer');
    if (layer) {
      layer.classList.add('is-leaving');
      setTimeout(function () {
        layer.hidden = true;
        layer.classList.remove('is-leaving');
      }, 300);
    }
    orbLeave(completed);
    state.finishedAt = new Date().toISOString();
    report('finish').catch(function (e) { console.warn('[onboarding] 完成状态未保存', e); });
  }

  function toast(text) {
    var el = document.createElement('div');
    el.className = 'tour-toast';
    el.textContent = text;
    document.body.appendChild(el);
    setTimeout(function () { el.classList.add('is-out'); }, 2200);
    setTimeout(function () { el.remove(); }, 2600);
  }

  // ---------- 启动 ----------
  function whenReady() {
    return new Promise(function (resolve) {
      var t0 = performance.now();
      (function loop() {
        var m = window.MaaRM;
        // 预热遮罩撤下后再接管，否则声明页会被压在遮罩下面
        var ov = document.getElementById('boot-overlay');
        // 热启动靠 .boot-skip 隐藏遮罩（display:none），hidden 属性不会置位，
        // 只看 hidden 会白等满 8 秒才弹声明。
        var booted = !ov || ov.hidden || getComputedStyle(ov).display === 'none';
        if (booted && m && m.state && m.state.modules && m.state.modules.length && document.querySelector('#task-list')) {
          return resolve();
        }
        if (performance.now() - t0 > 8000) return resolve();
        setTimeout(loop, 60);
      })();
    });
  }

  // 第一个未完成的步骤：已上报过、或结果状态已满足的步骤直接跳过。
  // 只对「结果可独立检查」的步骤用 done() 判断——弹窗关闭这类初始即为真的不计。
  var RESULT_CHECKABLE = { add_start_game: true, add_close_game: true };
  function firstPendingStep() {
    for (var i = 0; i < STEPS.length; i++) {
      var id = STEP_IDS[i];
      if (state.stepsDone.indexOf(id) !== -1) continue;
      if (RESULT_CHECKABLE[id]) {
        var ok = false;
        try { ok = !!STEPS[i].done(); } catch (e) { ok = false; }
        if (ok) { stepDone(id); continue; }
      }
      return i;
    }
    return STEPS.length;
  }

  whenReady().then(async function () {
    try { await load(); } catch (e) { console.warn('[onboarding] 引导状态读取失败', e); }
    if (!termsAccepted()) {
      showGate();
      return;
    }
    // 老用户只补声明，不补引导
    if (state.existingUser && !state.finishedAt) return;
    if (state.finishedAt) return;
    var from = firstPendingStep();
    if (from >= STEPS.length) { finish(true); return; }
    if (!state.launchSeen && from === 0) { showLaunch(); return; }
    startTour(from);
  });
})();
