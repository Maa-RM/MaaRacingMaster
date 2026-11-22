// 被动探针：attach 真实 App 的 WebView2（CDP 9223），只装观察器、不模拟交互。
//
// 用法（node 需可用；playwright-core 取自全局 @playwright/mcp，运行时解析，不写死路径）：
//   node probe_attach.js install    —— 装记录器 + 前端指纹（App 启动后执行一次）
//   node probe_attach.js dump       —— 导出记录到 _out/dump-<时间戳>.json
//   node probe_attach.js check      —— 只看记录器是否已装、待导出条数
//
// 记录内容（判决依据，全部是 Web 标准接口，独立于待验证实现）：
//   - tab 点击时刻（capture 捕获）
//   - page-slide 的 animationstart / animationcancel / animationend
//   - animationstart 自激活 900ms 逐帧采样：动画时钟 currentTime vs 墙钟 rAF 时刻
//     （判决「快进」的量：相邻两帧 ct 增量超出墙钟增量）
//   - longtask、事件计时（click 处理耗时）
//   - 前端指纹：帧资源 URL（仓库源码目录还是打包副本）+ 关键标记
//     （app.js 是否含 prewarmPages / settings.js 是否含 dash-panel--skeleton）
const { execSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const CDP_PORT = process.env.MAARM_CDP_PORT || '9223';
const OUT_DIR = path.join(__dirname, '_out');
const MODE = process.argv[2] || 'check';

// playwright-core 从全局 @playwright/mcp 解析（本机调试仪器，不进依赖清单）
function loadPlaywrightCore() {
  const npmRoot = execSync('npm root -g', { encoding: 'utf8' }).trim();
  return require(path.join(npmRoot, '@playwright', 'mcp', 'node_modules', 'playwright-core'));
}

async function findPage(browser) {
  const ctx = browser.contexts()[0];
  return (ctx ? ctx.pages() : []).find((p) => p.url().includes('index.html')) || null;
}

// 前端指纹：帧 URL + 关键源码标记（判断 App 实际加载的是哪一版前端）
async function fingerprint(page) {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Page.enable').catch(() => {});
  const tree = await cdp.send('Page.getResourceTree');
  const frames = [];
  const walk = (node) => {
    if (!node) return;
    frames.push({ url: node.frame.url, resources: (node.resources || []).map((r) => r.url) });
    (node.childFrames || []).forEach(walk);
  };
  walk(tree.frameTree);
  const out = { frames: frames.map((f) => f.url), markers: {} };
  const grab = async (suffix) => {
    for (const node of walkAll(tree.frameTree)) {
      const res = (node.resources || []).find((r) => r.url.endsWith(suffix));
      if (res) {
        try {
          const c = await cdp.send('Page.getResourceContent', { frameId: node.frame.id, url: res.url });
          return c.content || '';
        } catch (e) { return ''; }
      }
    }
    return '';
  };
  function* walkAll(node) {
    if (!node) return;
    yield node;
    for (const child of node.childFrames || []) yield* walkAll(child);
  }
  const appJs = await grab('app.js');
  const settingsJs = await grab('settings.js');
  out.markers.appJs_prewarmPages = appJs.includes('prewarmPages');
  out.markers.settingsJs_skeleton = settingsJs.includes('dash-panel--skeleton');
  out.markers.appJs_len = appJs.length;
  return out;
}

const INSTRUMENT = () => `
(() => {
  if (window.__maarmAnimInstalled) return 'already';
  window.__maarmAnimInstalled = true;
  window.__anim = [];
  window.__longtasks = [];
  window.__events = [];
  ['control', 'data', 'settings', 'about'].forEach((id) => {
    const el = document.getElementById('page-' + id);
    if (!el) return;
    ['animationstart', 'animationcancel', 'animationend'].forEach((t) => {
      el.addEventListener(t, (e) => {
        if (e.animationName !== 'page-slide') return;
        const rec = { ev: e.type, page: id, t: Math.round(performance.now()), elapsed: Math.round(e.elapsedTime * 1000) / 1000, frames: [] };
        window.__anim.push(rec);
        if (e.type === 'animationstart') {
          const t0 = performance.now();
          const tick = () => {
            const a = el.getAnimations().find((x) => x.animationName === 'page-slide');
            rec.frames.push([Math.round(performance.now() - t0), a ? Math.round(a.currentTime) : null]);
            if (performance.now() - t0 < 900) requestAnimationFrame(tick);
          };
          requestAnimationFrame(tick);
        }
      });
    });
  });
  document.querySelectorAll('.maarm-tab').forEach((btn) => {
    btn.addEventListener('click', () => window.__anim.push({ ev: 'click', page: btn.dataset.tab, t: Math.round(performance.now()) }), true);
  });
  try {
    new PerformanceObserver((l) => { for (const e of l.getEntries()) window.__longtasks.push({ s: Math.round(e.startTime), d: Math.round(e.duration) }); }).observe({ entryTypes: ['longtask'] });
  } catch (e) {}
  try {
    new PerformanceObserver((l) => { for (const e of l.getEntries()) window.__events.push({ name: e.name, s: Math.round(e.startTime), d: Math.round(e.duration) }); }).observe({ type: 'event', durationThreshold: 16 });
  } catch (e) {}
  return 'installed';
})()
`;

(async () => {
  const { chromium } = loadPlaywrightCore();
  let browser;
  try {
    browser = await chromium.connectOverCDP('http://127.0.0.1:' + CDP_PORT);
  } catch (e) {
    console.log(JSON.stringify({ error: 'CDP ' + CDP_PORT + ' 不可达：App 是否带调试端口启动？', detail: String(e.message || e) }));
    process.exit(2);
  }
  const page = await findPage(browser);
  if (!page) {
    console.log(JSON.stringify({ error: '没找到 index.html 页面', urls: browser.contexts()[0].pages().map((p) => p.url()) }));
    process.exit(2);
  }

  if (MODE === 'install') {
    const inst = await page.evaluate(INSTRUMENT());
    const fp = await fingerprint(page);
    fs.mkdirSync(OUT_DIR, { recursive: true });
    fs.writeFileSync(path.join(OUT_DIR, 'fingerprint.json'), JSON.stringify(fp, null, 1));
    console.log(JSON.stringify({ install: inst, fingerprint: fp }, null, 1));
  } else if (MODE === 'dump') {
    const r = await page.evaluate(() => ({
      anim: window.__anim ? window.__anim.splice(0) : null,
      longtasks: window.__longtasks ? window.__longtasks.splice(0) : null,
      events: window.__events ? window.__events.splice(0) : null,
      installed: !!window.__maarmAnimInstalled,
    }));
    fs.mkdirSync(OUT_DIR, { recursive: true });
    const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    const file = path.join(OUT_DIR, 'dump-' + stamp + '.json');
    fs.writeFileSync(file, JSON.stringify(r, null, 1));
    const anims = (r.anim || []).filter((a) => a.ev !== 'click');
    const summary = (r.anim || []).map((a) => {
      if (a.ev === 'click') return 'click:' + a.page;
      if (a.ev === 'animationstart') {
        const cts = a.frames.map((f) => f[1]).filter((v) => v != null);
        let skip = 0;
        for (let i = 1; i < a.frames.length; i++) {
          const dw = a.frames[i][0] - a.frames[i - 1][0];
          const da = (a.frames[i][1] ?? 0) - (a.frames[i - 1][1] ?? 0);
          if (da - dw > skip) skip = da - dw;
        }
        return 'start:' + a.page + '(frames=' + a.frames.length + ',maxSkip=' + Math.round(skip) + ',ctTail=' + (cts[cts.length - 1] ?? '?') + ')';
      }
      return a.ev + ':' + a.page;
    });
    console.log(JSON.stringify({ dump: file, timeline: summary, longtasks: r.longtasks, events: (r.events || []).slice(0, 12) }, null, 1));
  } else {
    const r = await page.evaluate(() => ({ installed: !!window.__maarmAnimInstalled, pending: window.__anim ? window.__anim.length : null }));
    console.log(JSON.stringify(r));
  }
  process.exit(0);
})().catch((e) => { console.log(JSON.stringify({ error: String((e && e.message) || e) })); process.exit(1); });
