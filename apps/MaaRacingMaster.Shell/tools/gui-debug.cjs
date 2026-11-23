const { spawn } = require('node:child_process');
const { createHash } = require('node:crypto');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { fileURLToPath } = require('node:url');
const { setTimeout: delay } = require('node:timers/promises');

const frontend = path.resolve(__dirname, '../frontend');
const comparisonWindowMs = 2500;
const scenarios = {
  manage: {
    trigger: '#btn-manage',
    region: '#manage-grid',
    ready: '#manage-grid [data-manage]',
    animationRoot: '.maarm-modal-overlay',
    escape: true,
  },
  data: {
    trigger: '.maarm-tab[data-tab="data"]',
    region: '#page-data',
    ready: '#page-data .dash-panel',
    reset: '.maarm-tab[data-tab="control"]',
  },
};

function log(value) {
  console.log(JSON.stringify(value));
}

function fileMatches(url, filename) {
  try {
    return path.relative(filename, fileURLToPath(url)) === '';
  } catch {
    return false;
  }
}

async function findPage(port) {
  let pages;
  try {
    const response = await fetch('http://127.0.0.1:' + port + '/json/list', {
      signal: AbortSignal.timeout(1200),
    });
    if (!response.ok) throw new Error('HTTP ' + response.status);
    pages = await response.json();
    if (!Array.isArray(pages)) throw new Error('目标列表格式错误');
  } catch (error) {
    throw new Error('CDP 127.0.0.1:' + port + ' 不可用，请通过 dev-shell.cmd --gui-debug 启动。' + error.message);
  }
  const filename = path.join(frontend, 'index.html');
  const matches = pages.filter((page) => page.type === 'page' && fileMatches(page.url, filename));
  if (matches.length !== 1) {
    throw new Error('需要唯一的本仓库主页面，找到 ' + matches.length + ' 个。目标：' + pages.map((page) => page.url).join(', '));
  }
  const page = matches[0];
  const socketUrl = new URL(page.webSocketDebuggerUrl);
  if (socketUrl.protocol !== 'ws:' || socketUrl.hostname !== '127.0.0.1' || socketUrl.port !== String(port)) {
    throw new Error('CDP 返回的 WebSocket 与本机调试端口不一致。');
  }
  return page;
}

async function connect(url) {
  const socket = new WebSocket(url);
  const opened = Promise.withResolvers();
  const pending = new Map();
  let nextId = 0;
  socket.addEventListener('open', opened.resolve, { once: true });
  socket.addEventListener('message', ({ data }) => {
    const reply = JSON.parse(data);
    pending.get(reply.id)?.(reply);
  });
  socket.addEventListener('close', () => {
    opened.reject(new Error('CDP 连接已关闭。'));
    for (const finish of pending.values()) finish({ error: { message: 'CDP 连接已关闭。' } });
  });
  socket.addEventListener('error', () => opened.reject(new Error('CDP WebSocket 无法连接。')), { once: true });
  const timer = setTimeout(() => {
    opened.reject(new Error('打开 CDP WebSocket 超时。'));
    socket.close();
  }, 2000);
  try {
    await opened.promise;
  } finally {
    clearTimeout(timer);
  }
  return {
    close: () => socket.close(),
    send(method, params = {}) {
      return new Promise((resolve, reject) => {
        const id = ++nextId;
        if (socket.readyState !== WebSocket.OPEN) {
          reject(new Error('CDP 连接已关闭。'));
          return;
        }
        const timer = setTimeout(() => finish({ error: { message: '超时' } }), 5000);
        function finish(reply) {
          clearTimeout(timer);
          pending.delete(id);
          if (reply.error) reject(new Error(method + ': ' + reply.error.message));
          else resolve(reply.result);
        }
        pending.set(id, finish);
        try {
          socket.send(JSON.stringify({ id, method, params }));
        } catch (error) {
          finish({ error });
        }
      });
    },
  };
}

async function evaluate(cdp, fn, ...args) {
  const expression = '(' + fn.toString() + ')(...' + JSON.stringify(args) + ')';
  const reply = await cdp.send('Runtime.evaluate', { expression, returnByValue: true });
  if (reply.exceptionDetails) {
    throw new Error(reply.exceptionDetails.exception?.description || reply.exceptionDetails.text);
  }
  return reply.result.value;
}

async function fingerprint(cdp, target) {
  await cdp.send('Page.enable');
  const { frameTree } = await cdp.send('Page.getResourceTree');
  const entry = path.join(frontend, 'app.js');
  const resource = frameTree.resources.find(({ url }) => fileMatches(url, entry));
  if (!resource) throw new Error('主页面尚未加载仓库的 app.js。');
  const { content, base64Encoded } = await cdp.send('Page.getResourceContent', {
    frameId: frameTree.frame.id,
    url: resource.url,
  });
  const hash = (buffer) => createHash('sha256').update(buffer).digest('hex');
  const loadedAppSha256 = hash(Buffer.from(content, base64Encoded ? 'base64' : 'utf8'));
  const diskAppSha256 = hash(await fs.readFile(entry));
  if (loadedAppSha256 !== diskAppSha256) throw new Error('窗口中的 app.js 与工作区不同。请重新启动源码窗口。');
  return { targetId: target.id, url: frameTree.frame.url, loadedAppSha256 };
}

async function attach(port, wait) {
  const deadline = Date.now() + (wait ? 15000 : 0);
  for (;;) {
    let cdp;
    try {
      const target = await findPage(port);
      cdp = await connect(target.webSocketDebuggerUrl);
      return { cdp, identity: await fingerprint(cdp, target) };
    } catch (error) {
      cdp?.close();
      if (Date.now() >= deadline) throw error;
      await delay(300);
    }
  }
}

// This function runs in the inspected page. One bounded capture per page.
function startCapture(scenario) {
  if (window.__maarmGuiDebugCapture) throw new Error('该页面已有采集任务。');
  const clicks = [];
  const frames = [];
  const animations = [];
  const entries = [];
  const observers = [];
  const animationRoot = (region) => scenario.animationRoot ? region?.closest(scenario.animationRoot) : region;
  const supported = PerformanceObserver.supportedEntryTypes;
  for (const type of ['resource', 'longtask', 'event']) {
    if (!supported.includes(type)) continue;
    const observer = new PerformanceObserver((list) => {
      entries.push(...list.getEntries().map((entry) => entry.toJSON()));
    });
    observer.observe(type === 'event' ? { type, durationThreshold: 16 } : { type });
    observers.push(observer);
  }
  const events = new AbortController();
  const listen = (type, listener) => document.addEventListener(type, listener, {
    capture: true,
    signal: events.signal,
  });
  listen('click', (event) => {
    if (event.target.closest(scenario.trigger)) clicks.push({ time: performance.now(), trusted: event.isTrusted });
  });
  for (const type of ['animationstart', 'animationend', 'animationcancel', 'transitionrun', 'transitionend', 'transitioncancel']) {
    listen(type, (event) => {
      if (!animationRoot(document.querySelector(scenario.region))?.contains(event.target)) return;
      animations.push({ type, time: performance.now(), name: event.animationName || event.propertyName });
    });
  }
  let frame;
  const sample = (time) => {
    const region = document.querySelector(scenario.region);
    const ready = document.querySelector(scenario.ready);
    const active = animationRoot(region)?.getAnimations({ subtree: true }) ?? [];
    frames.push({
      time,
      ready: !!ready?.getClientRects().length,
      imagesPending: region ? Array.from(region.querySelectorAll('img')).filter((img) => !img.complete).length : null,
      animations: active.map((animation) => ({
        name: animation.animationName || animation.transitionProperty || animation.id,
        time: typeof animation.currentTime === 'number' ? animation.currentTime : null,
      })),
    });
    frame = requestAnimationFrame(sample);
  };
  frame = requestAnimationFrame(sample);
  const startedAt = performance.now();
  const deadline = setTimeout(() => window.__maarmGuiDebugCapture?.(), 20000);
  window.__maarmGuiDebugCapture = () => {
    clearTimeout(deadline);
    cancelAnimationFrame(frame);
    events.abort();
    for (const observer of observers) {
      entries.push(...observer.takeRecords().map((entry) => entry.toJSON()));
      observer.disconnect();
    }
    delete window.__maarmGuiDebugCapture;
    return { timeOrigin: performance.timeOrigin, startedAt, clicks, frames, animations, entries };
  };
}

async function click(cdp, selector) {
  const point = await evaluate(cdp, (selector) => {
    const element = document.querySelector(selector);
    if (!element || element.disabled) return null;
    const rect = element.getBoundingClientRect();
    const x = rect.x + rect.width / 2;
    const y = rect.y + rect.height / 2;
    return element.contains(document.elementFromPoint(x, y)) ? { x, y } : null;
  }, selector);
  if (!point) throw new Error('目标不可点击，请检查页面和弹窗状态：' + selector);
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', button: 'left', clickCount: 1, ...point });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', button: 'left', clickCount: 1, ...point });
}

async function compare(cdp, scenario) {
  await click(cdp, scenario.trigger);
  await delay(comparisonWindowMs);
  if (scenario.escape) {
    await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
    await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
  } else {
    await click(cdp, scenario.reset);
  }
  await delay(600);
  const stillOpen = await evaluate(cdp, (selector) => !!document.querySelector(selector)?.getClientRects().length, scenario.region);
  if (stillOpen) throw new Error('第一次操作后未回到起始状态：' + scenario.region);
  await click(cdp, scenario.trigger);
  await delay(comparisonWindowMs);
}

function recordDesktop(directory) {
  const file = path.join(directory, 'desktop.mkv');
  const process = spawn('ffmpeg', [
    '-hide_banner', '-f', 'gdigrab', '-framerate', '30', '-draw_mouse', '1',
    '-use_wallclock_as_timestamps', '1', '-i', 'desktop', '-copyts',
    '-vf', 'showinfo,setpts=PTS-STARTPTS', '-fps_mode', 'vfr', '-t', '15',
    '-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '2', file,
  ], { windowsHide: true, stdio: ['pipe', 'ignore', 'pipe'] });
  const started = Promise.withResolvers();
  const done = Promise.withResolvers();
  let diagnostics = '';
  const timestamps = [];
  let rest = '';
  let timeBase = null;
  process.stdin.on('error', () => {});
  process.stderr.setEncoding('utf8').on('data', (chunk) => {
    diagnostics = (diagnostics + chunk).slice(-3500);
    const lines = (rest + chunk).split(/[\r\n]+/);
    rest = lines.pop();
    for (const line of lines) {
      const base = line.match(/config in time_base: (\d+)\/(\d+)/);
      if (base) timeBase = Number(base[1]) / Number(base[2]);
      const frame = line.match(/\bn:\s*(\d+)\s+pts:\s*(\d+)/);
      if (frame && timeBase) {
        timestamps.push({ inputFrame: Number(frame[1]), epochMs: Number(frame[2]) * timeBase * 1000 });
        started.resolve();
      }
    }
  });
  const timer = setTimeout(() => started.reject(new Error('等待桌面录制的第一帧超时。')), 5000);
  process.on('error', (error) => {
    started.reject(error);
    done.resolve({ error: error.message });
  });
  process.on('close', (code) => {
    started.reject(new Error('桌面录制提前结束。' + diagnostics));
    done.resolve(code === 0 && timestamps.length
      ? { file, timestamps }
      : { error: '桌面录制未完成，退出码 ' + code + '。' + diagnostics });
  });
  return {
    async ready() {
      try { await started.promise; } finally { clearTimeout(timer); }
    },
    async stop() {
      process.stdin.end('q\n');
      const timeout = setTimeout(() => process.kill(), 3000);
      try { return await done.promise; } finally { clearTimeout(timeout); }
    },
  };
}

function summarize(capture) {
  return capture.clicks.map((click, index) => {
    const end = Math.min(click.time + comparisonWindowMs, capture.clicks[index + 1]?.time ?? Infinity);
    const frames = capture.frames.filter((frame) => frame.time >= click.time && frame.time < end);
    const ready = frames.find((frame) => frame.ready);
    const gap = frames.slice(1).reduce((max, frame, index) => Math.max(max, frame.time - frames[index].time), 0);
    return {
      clickEpochMs: capture.timeOrigin + click.time,
      firstRafMs: frames.length ? frames[0].time - click.time : null,
      domReadyMs: ready ? ready.time - click.time : null,
      maxConsecutiveRafGapMs: frames.length > 1 ? gap : null,
    };
  });
}

async function main() {
  const [mode, portText, scenarioName, ...flags] = process.argv.slice(2);
  if (!mode || mode === '--help') {
    console.log('node gui-debug.cjs check|wait <port>\nnode gui-debug.cjs record|compare <port> <manage|data|scenario.json> [--desktop-video]');
    return;
  }
  const port = Number(portText);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('请提供源码启动器打印的调试端口。');
  const captures = mode === 'record' || mode === 'compare';
  if (!captures && !['wait', 'check'].includes(mode)) throw new Error('未知命令：' + mode);
  if (flags.some((flag) => flag !== '--desktop-video') || (!captures && scenarioName)) throw new Error('命令参数无效，请查看 --help。');
  let scenario;
  if (captures) {
    scenario = Object.hasOwn(scenarios, scenarioName) ? scenarios[scenarioName] : JSON.parse(await fs.readFile(scenarioName, 'utf8'));
    for (const key of ['trigger', 'region', 'ready']) {
      if (typeof scenario[key] !== 'string' || !scenario[key]) throw new Error('场景需要 CSS 选择器：' + key);
    }
    if (mode === 'compare' && !scenario.escape && typeof scenario.reset !== 'string') {
      throw new Error('自动对照需要 reset 选择器或 escape: true。');
    }
  }

  const { cdp, identity } = await attach(port, mode === 'wait');
  let video;
  let captureStarted = false;
  try {
    log(identity);
    if (!captures) return;
    const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'MaaRM-gui-debug-'));
    let desktop = { status: '未采集桌面画面' };
    if (flags.includes('--desktop-video')) {
      video = recordDesktop(directory);
      try {
        await video.ready();
      } catch (error) {
        desktop = { status: '桌面录制不可用', error: error.message };
        await video.stop();
        video = null;
        log(desktop);
      }
    }
    if (mode === 'compare') {
      const alreadyOpen = await evaluate(cdp, (selector) => !!document.querySelector(selector)?.getClientRects().length, scenario.region);
      if (alreadyOpen) throw new Error('请先返回起始界面：' + scenario.region);
    }
    await evaluate(cdp, (selectors) => selectors.forEach((selector) => document.querySelector(selector)), [
      scenario.trigger, scenario.region, scenario.ready, scenario.animationRoot, scenario.reset,
    ].filter(Boolean));
    await evaluate(cdp, startCapture, scenario);
    captureStarted = true;
    if (mode === 'compare') {
      await compare(cdp, scenario);
    } else {
      log({ action: '现在操作窗口，采集持续 8 秒。' });
      await delay(8000);
    }
    const capture = await evaluate(cdp, () => window.__maarmGuiDebugCapture());
    captureStarted = false;
    if (video) {
      desktop = await video.stop();
      video = null;
    }
    const summary = summarize(capture);
    const file = path.join(directory, 'timeline.json');
    await fs.writeFile(file, JSON.stringify({ identity, mode, scenario: scenarioName, summary, desktop, ...capture }, null, 2));
    log({ file, summary, desktop: desktop.file || desktop.status || desktop.error });
    if (!summary.length) throw new Error('采集期间没有捕获场景入口的点击。');
    if (mode === 'compare' && summary.length !== 2) throw new Error('预期采集两次触发点击，实际 ' + summary.length + ' 次。');
  } finally {
    if (captureStarted) await evaluate(cdp, () => window.__maarmGuiDebugCapture?.()).catch(() => {});
    cdp.close();
    if (video) await video.stop();
  }
}

main().catch((error) => {
  console.error(JSON.stringify({ error: error.message }));
  process.exitCode = 1;
});
