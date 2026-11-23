// 对照实验注入器（实验区 page_slide_first_visit）。
// 页面侧注入三个对照钩子，不改动仓库 frontend 源文件：
//   A predecode  —— 点击前把齿轮弹窗将用到的封面 URL 全部 Image+decode() 预热
//   B delay      —— get_module_dashboard 响应人为延迟 N ms（请求时机保持自然）
//   C preready   —— 注入时预取当前看板 payload，之后同参调用零等待返回缓存
// 附加与指纹校验逻辑复制自 apps/MaaRacingMaster.Shell/tools/gui-debug.cjs（同仓同源），
// evaluate 增加 awaitPromise 以支持异步注入。
// 用法：
//   node inject_control.js status <port>
//   node inject_control.js predecode <port>
//   node inject_control.js delay <port> <ms>
//   node inject_control.js preready <port>
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
  }, 3000);
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

// 与 gui-debug.cjs 相同的指纹校验：页面加载的 app.js 必须与工作区一致。
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
  const loaded = hash(Buffer.from(content, base64Encoded ? 'base64' : 'utf8'));
  const disk = hash(await fs.readFile(entry));
  if (loaded !== disk) throw new Error('窗口中的 app.js 与工作区不同，请重启源码窗口。');
  return { cdp, url: frameTree.frame.url };
}

async function evaluate(cdp, fn, ...args) {
  const expression = '(' + fn.toString() + ')(...' + JSON.stringify(args) + ')';
  const reply = await cdp.send('Runtime.evaluate', {
    expression,
    returnByValue: true,
    awaitPromise: true,
  });
  if (reply.exceptionDetails) {
    throw new Error(reply.exceptionDetails.exception?.description || reply.exceptionDetails.text);
  }
  return reply.result.value;
}

// ---------- 页面侧注入体（均在被检页面内执行） ----------

function pageStatus() {
  const M = window.MaaRM;
  if (!M) return { error: 'MaaRM 未就绪' };
  const byId = new Map(M.state.modules.filter((m) => m && m.id).map((m) => [m.id, m]));
  const dashIds = M.state.queue.order
    .map((mid) => byId.get(mid))
    .filter((m) => !!m && m.kind === 'module' && m.has_dashboard === true)
    .map((m) => m.id);
  return {
    modules: M.state.modules.length,
    coverDefault: M.state.coverDefault || '',
    covers: M.state.modules.filter((m) => m && m.cover).map((m) => m.cover),
    queueOrder: M.state.queue.order,
    dashCandidates: dashIds,
    backendSeen: !!document.querySelector('.maarm-tab'),
    alreadyHooked: {
      delay: !!window.__maarmControlDelay,
      pre: !!window.__maarmControlPre,
      predecoded: window.__maarmControl ? window.__maarmControl.predecoded.length : 0,
    },
  };
}

// A：预取并解码全部封面 URL（含默认封面）。解码位图进 Chromium 内存缓存，
// 弹窗插入同 URL <img> 时应零取数、零解码。
function pagePredecode() {
  const M = window.MaaRM;
  const urls = [
    ...new Set([M.state.coverDefault, ...M.state.modules.map((m) => m && m.cover)].filter(Boolean)),
  ];
  const t0 = performance.now();
  const results = [];
  return (async () => {
    for (const u of urls) {
      const img = new Image();
      const loaded = await new Promise((res) => {
        img.onload = () => res(true);
        img.onerror = () => res(false);
        img.src = u;
      });
      let decoded = false;
      if (loaded) {
        try {
          await img.decode();
          decoded = true;
        } catch {
          /* 解码失败按未就绪计 */
        }
      }
      results.push({ url: u, loaded, decoded });
    }
    window.__maarmControl = { predecoded: results, ms: performance.now() - t0 };
    return {
      urls: urls.length,
      decoded: results.filter((r) => r.decoded).length,
      ms: Math.round(performance.now() - t0),
    };
  })();
}

// B：get_module_dashboard 响应延迟 ms 毫秒派发（请求照常发出，时机自然）。
function pageDelay(ms) {
  const M = window.MaaRM;
  if (window.__maarmControlDelay) return 'already-hooked';
  const orig = M.maarm.call.bind(M.maarm);
  window.__maarmControlDelay = { ms, calls: [] };
  M.maarm.call = (method, params) => {
    const p = orig(method, params);
    if (method !== 'get_module_dashboard') return p;
    window.__maarmControlDelay.calls.push(Math.round(performance.now()));
    return new Promise((res, rej) => {
      p.then(
        (v) => setTimeout(() => res(v), ms),
        (e) => setTimeout(() => rej(e), ms),
      );
    });
  };
  return 'hooked-delay-' + ms + 'ms';
}

// C：预取当前看板 payload；之后同参 get_module_dashboard 零等待返回缓存。
function pagePreready() {
  const M = window.MaaRM;
  if (window.__maarmControlPre) return 'already-hooked';
  const byId = new Map(M.state.modules.filter((m) => m && m.id).map((m) => [m.id, m]));
  const ids = M.state.queue.order
    .map((mid) => byId.get(mid))
    .filter((m) => !!m && m.kind === 'module' && m.has_dashboard === true);
  const target = ids[0];
  if (!target) return { error: 'no dashboard candidate' };
  const t0 = performance.now();
  return (async () => {
    const payload = await window.maarm.call('get_module_dashboard', { module_id: target.id });
    const warmMs = Math.round(performance.now() - t0);
    const orig = M.maarm.call.bind(M.maarm);
    window.__maarmControlPre = { moduleId: target.id, warmMs, served: 0 };
    M.maarm.call = (method, params) => {
      if (method === 'get_module_dashboard' && params && params.module_id === target.id) {
        window.__maarmControlPre.served += 1;
        return Promise.resolve(payload);
      }
      return orig(method, params);
    };
    return {
      moduleId: target.id,
      warmMs,
      payloadBytes: JSON.stringify(payload).length,
    };
  })();
}

// D：首访着色器来源定位——注入一次性 <style> 削平/隐藏目标效果面。
// 预设只影响首绘路径，不改仓库文件；规则用 !important 保证盖过内联样式。
function pageStrip(preset) {
  const PRESETS = {
    // 数据页：隐藏整列（性能卡 vs 看板中心+实时预览）
    'data-no-perf': '#page-data .col-left{display:none!important}',
    'data-no-right': '#page-data .col-right{display:none!important}',
    'data-bare': '#page-data .col-left,#page-data .col-right{display:none!important}',
    // 齿轮弹窗：隐藏网格（遮罩+卡壳仍首绘）；或削平效果（描边/扫描/squircle/阴影）
    'manage-no-grid': '.maarm-modal-overlay .task-gallery-wrap{display:none!important}',
    'manage-flat':
      '.maarm-modal-overlay .task-gallery-stroke{display:none!important}' +
      '.maarm-modal-overlay .task-gallery-scan{display:none!important}' +
      '.maarm-modal-overlay .task-gallery-cell{corner-shape:auto!important;box-shadow:none!important}' +
      '.maarm-modal-card{box-shadow:none!important}',
  };
  const css = PRESETS[preset];
  if (!css) return { error: 'unknown preset: ' + preset };
  const id = 'maarm-strip-style';
  if (document.getElementById(id)) return { error: 'already injected' };
  const style = document.createElement('style');
  style.id = id;
  style.textContent = css;
  document.head.appendChild(style);
  return { preset, injected: true };
}

// ---------- 主流程 ----------

async function main() {
  const [mode, portText, extra] = process.argv.slice(2);
  const port = Number(portText);
  if (!mode || !Number.isInteger(port) || port < 1 || port > 65535) {
    console.error(JSON.stringify({ error: '用法: node inject_control.js <status|predecode|delay|preready> <port> [ms]' }));
    process.exitCode = 1;
    return;
  }
  const { cdp } = await attach(port);
  try {
    let result;
    if (mode === 'status') result = await evaluate(cdp, pageStatus);
    else if (mode === 'predecode') result = await evaluate(cdp, pagePredecode);
    else if (mode === 'delay') {
      const ms = Number(extra);
      if (!Number.isFinite(ms) || ms < 0) throw new Error('delay 需要毫秒数');
      result = await evaluate(cdp, pageDelay, ms);
    } else if (mode === 'preready') result = await evaluate(cdp, pagePreready);
    else if (mode === 'strip') {
      if (!extra) throw new Error('strip 需要预设名（data-no-perf|data-no-right|data-bare|manage-no-grid|manage-flat）');
      result = await evaluate(cdp, pageStrip, extra);
    } else if (mode === 'jsclick') {
      if (!extra) throw new Error('jsclick 需要选择器');
      result = await evaluate(cdp, (sel) => {
        const el = document.querySelector(sel);
        if (!el) return { error: 'no element: ' + sel };
        el.click();
        return { clicked: sel };
      }, extra);
    } else if (mode === 'key') {
      if (!extra) throw new Error('key 需要 key 名（如 Escape）');
      await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: extra, code: extra, windowsVirtualKeyCode: extra === 'Escape' ? 27 : 0 });
      await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: extra, code: extra, windowsVirtualKeyCode: extra === 'Escape' ? 27 : 0 });
      result = { keyed: extra };
    }
    else throw new Error('未知模式: ' + mode);
    console.log(JSON.stringify(result));
  } finally {
    cdp.close();
  }
}

main().catch((error) => {
  console.error(JSON.stringify({ error: error.message }));
  process.exitCode = 1;
});
