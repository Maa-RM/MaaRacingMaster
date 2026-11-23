// 停顿归因探针（实验区 page_slide_first_visit）：抓一次数据页自然冷首访的
// CDP trace + rAF 采样，输出帧停顿窗口内的渲染事件分布。
// 连接与点击逻辑复制自 gui-debug.cjs / inject_control.js（同仓同源）。
// 用法：node trace_visit.js <port>
'use strict';

const { createHash } = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');
const { fileURLToPath } = require('node:url');
const { setTimeout: delay } = require('node:timers/promises');

const frontend = path.resolve(__dirname, '../../../apps/MaaRacingMaster.Shell/frontend');

function fileMatches(url, filename) {
  try {
    return path.relative(filename, fileURLToPath(url)) === '';
  } catch {
    return false;
  }
}

async function findPage(port) {
  const response = await fetch('http://127.0.0.1:' + port + '/json/list', {
    signal: AbortSignal.timeout(1500),
  });
  if (!response.ok) throw new Error('HTTP ' + response.status);
  const pages = await response.json();
  const filename = path.join(frontend, 'index.html');
  const matches = pages.filter((page) => page.type === 'page' && fileMatches(page.url, filename));
  if (matches.length !== 1) throw new Error('需要唯一的本仓库主页面，找到 ' + matches.length + ' 个');
  return matches[0];
}

async function connect(url) {
  const socket = new WebSocket(url);
  const opened = Promise.withResolvers();
  const pending = new Map();
  const eventListeners = [];
  let nextId = 0;
  socket.addEventListener('open', opened.resolve, { once: true });
  socket.addEventListener('message', ({ data }) => {
    const reply = JSON.parse(data);
    if (reply.id !== undefined) {
      pending.get(reply.id)?.(reply);
      return;
    }
    for (const fn of eventListeners) fn(reply);
  });
  socket.addEventListener('close', () => {
    opened.reject(new Error('CDP 连接已关闭。'));
    for (const finish of pending.values()) finish({ error: { message: 'CDP 连接已关闭。' } });
  });
  socket.addEventListener('error', () => opened.reject(new Error('CDP WebSocket 无法连接。')), { once: true });
  const timer = setTimeout(() => {
    opened.reject(new Error('打开 CDP WebSocket 超时。'));
    socket.close();
  }, 3000);
  try {
    await opened.promise;
  } finally {
    clearTimeout(timer);
  }
  return {
    close: () => socket.close(),
    onEvent: (fn) => eventListeners.push(fn),
    send(method, params = {}) {
      return new Promise((resolve, reject) => {
        const id = ++nextId;
        if (socket.readyState !== WebSocket.OPEN) {
          reject(new Error('CDP 连接已关闭。'));
          return;
        }
        const timer = setTimeout(() => finish({ error: { message: '超时: ' + method } }), 30000);
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

async function attach(port) {
  const target = await findPage(port);
  const cdp = await connect(target.webSocketDebuggerUrl);
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
  if (hash(Buffer.from(content, base64Encoded ? 'base64' : 'utf8')) !== hash(await fs.readFile(entry))) {
    throw new Error('窗口中的 app.js 与工作区不同，请重启源码窗口。');
  }
  return cdp;
}

async function evaluate(cdp, fn, ...args) {
  const expression = '(' + fn.toString() + ')(...' + JSON.stringify(args) + ')';
  const reply = await cdp.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (reply.exceptionDetails) {
    throw new Error(reply.exceptionDetails.exception?.description || reply.exceptionDetails.text);
  }
  return reply.result.value;
}

// 页面侧：最小 rAF 采样器（帧时刻 + 入口动画时钟），供停顿窗口定位。
// 停止器挂 window（函数不可跨 evaluate 序列化）。
function armSampler(kind) {
  const frames = [];
  let frame;
  const sample = (time) => {
    let clock = null;
    if (kind === 'manage') {
      const overlay = document.querySelector('.maarm-modal-overlay');
      const a = overlay?.getAnimations({ subtree: true }).find((x) => x.animationName === 'modal-overlay-in');
      clock = a && typeof a.currentTime === 'number' ? Math.round(a.currentTime) : null;
    } else {
      const page = document.querySelector('#page-data');
      const slide = page?.classList.contains('page-slide-in')
        ? (page.getAnimations({ subtree: true }).find((a) => a.animationName === 'page-slide') || {}).currentTime
        : null;
      clock = typeof slide === 'number' ? Math.round(slide) : null;
    }
    frames.push({ time, clock });
    frame = requestAnimationFrame(sample);
  };
  frame = requestAnimationFrame(sample);
  window.__maarmTraceStop = () => {
    cancelAnimationFrame(frame);
    return frames;
  };
  return 'sampler-armed';
}

async function click(cdp, selector) {
  const point = await evaluate(cdp, (sel) => {
    const el = document.querySelector(sel);
    if (!el || el.disabled) return null;
    const rect = el.getBoundingClientRect();
    const x = rect.x + rect.width / 2;
    const y = rect.y + rect.height / 2;
    return el.contains(document.elementFromPoint(x, y)) ? { x, y } : null;
  }, selector);
  if (!point) throw new Error('目标不可点击：' + selector);
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', button: 'left', clickCount: 1, ...point });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', button: 'left', clickCount: 1, ...point });
}

async function main() {
  const port = Number(process.argv[2]);
  const kind = process.argv[3] === 'manage' ? 'manage' : 'data';
  const trigger = kind === 'manage' ? '#btn-manage' : '.maarm-tab[data-tab="data"]';
  // 可选第 4 参：点击前先静置 N ms 再触发（默认 0），用于覆盖启动期磁盘缓存预取窗口。
  const preDelay = Number(process.argv[4]) || 0;
  if (!Number.isInteger(port)) throw new Error('用法: node trace_visit.js <port> [data|manage] [preDelayMs]');
  const cdp = await attach(port);
  const chunks = [];
  let complete = null;
  cdp.onEvent((msg) => {
    if (msg.method === 'Tracing.dataCollected') chunks.push(...msg.params.value);
    if (msg.method === 'Tracing.tracingComplete') complete = msg.params;
  });
  try {
    const armed = await evaluate(cdp, armSampler, kind);
    await cdp.send('Tracing.start', {
      traceConfig: {
        recordMode: 'recordUntilFull',
        includedCategories: [
          'devtools.timeline', 'toplevel', 'disabled-by-default-devtools.timeline.frame',
          'gpu', 'gpu.service', 'skia', 'disabled-by-default-gpu.service',
        ],
      },
    });
    // 时间锚点：renderer trace 的时间基是单调钟（µs），与 performance.now 差一个恒定偏移。
    // console.timeStamp 在 trace 流里落一枚 TimeStamp 事件，与页内 performance.now 同刻采样，
    // 两者相减即偏移。
    const clickPerf = await evaluate(cdp, () => {
      const t = performance.now();
      console.timeStamp('maarm-click');
      return t;
    });
    if (preDelay > 0) await delay(preDelay);
    const clickDelay = await evaluate(cdp, () => performance.now());
    await click(cdp, trigger);
    console.log(JSON.stringify({ note: 'anchor-before-idle', clickPerf, clickAfterIdle: clickDelay, preDelay }));
    await delay(2500);
    await cdp.send('Tracing.end');
    for (let i = 0; i < 100 && !complete; i++) await delay(100);
    const frames = await evaluate(cdp, () => window.__maarmTraceStop());
    // 停顿窗口 = 相邻 rAF 最大间隔
    let gapStart = null;
    let maxGap = 0;
    for (let i = 1; i < frames.length; i++) {
      const g = frames[i].time - frames[i - 1].time;
      if (g > maxGap) {
        maxGap = g;
        gapStart = frames[i - 1].time;
      }
    }
    const gapEnd = gapStart + maxGap;
    const anchors = chunks.filter((ev) => ev.name === 'TimeStamp' && ev.args?.data?.message === 'maarm-click');
    if (!anchors.length) throw new Error('trace 中未找到 maarm-click 锚点（chunks=' + chunks.length + '）');
    const offsetMs = anchors[0].ts / 1000 - clickPerf; // traceMs = perfMs + offsetMs
    const inWindow = (ev) =>
      ev.ph === 'X' && ev.dur != null &&
      ev.ts / 1000 - offsetMs >= gapStart - 5 && ev.ts / 1000 - offsetMs <= gapEnd + 5;
    // 按线程分桶，识别主线程（出现事件分发/布局/绘制）
    const byThread = new Map();
    for (const ev of chunks) {
      if (!inWindow(ev)) continue;
      const key = ev.pid + ':' + ev.tid;
      if (!byThread.has(key)) byThread.set(key, []);
      byThread.get(key).push(ev);
    }
    const report = [];
    for (const [thread, events] of byThread) {
      const names = events.map((e) => e.name);
      const isMain = names.some((n) => ['UpdateLayoutTree', 'Layout', 'Paint', 'PrePaint', 'FunctionCall', 'EventDispatch'].includes(n));
      // 完整落在窗口内的事件按耗时排序，前 25
      const spans = events
        .filter((e) => {
          const a = e.ts / 1000 - offsetMs;
          return a >= gapStart - 2 && a + e.dur / 1000 <= gapEnd + 2 && e.dur > 300;
        })
        .sort((a, b) => b.dur - a.dur)
        .slice(0, 25)
        .map((e) => ({ name: e.name, durMs: +(e.dur / 1000).toFixed(1), at: +(e.ts / 1000 - offsetMs - gapStart).toFixed(1), cat: e.cat }));
      report.push({ thread, isMain, eventCount: events.length, topSpans: spans });
    }
    const out = {
      clickAt: +clickPerf.toFixed(1),
      traceOffsetMs: +offsetMs.toFixed(2),
      maxGapMs: +maxGap.toFixed(1),
      windowMs: [+gapStart.toFixed(1), +gapEnd.toFixed(1)],
      slideClockBefore: frames.find((f) => f.time >= gapStart - 30)?.clock ?? null,
      slideClockAfter: frames.find((f) => f.time >= gapEnd)?.clock ?? null,
      threads: report.filter((r) => r.eventCount > 0),
    };
    const file = path.join(__dirname, '_out', kind === 'manage' ? 'trace-cold-manage.json' : 'trace-cold-data.json');
    await fs.mkdir(path.dirname(file), { recursive: true });
    await fs.writeFile(file, JSON.stringify({ report: out }, null, 2));
    await fs.writeFile(path.join(__dirname, '_out', kind === 'manage' ? 'trace-cold-manage-raw.json' : 'trace-cold-data-raw.json'), JSON.stringify(chunks));
    console.log(JSON.stringify(out, null, 1));
  } finally {
    cdp.close();
  }
}

main().catch((error) => {
  console.error(JSON.stringify({ error: error.message }));
  process.exitCode = 1;
});
