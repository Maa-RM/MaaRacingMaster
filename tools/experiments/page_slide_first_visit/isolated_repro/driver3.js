// v3：多轮重复 + 逐帧时间轴 + will-change A/B。
// 用法：node driver3.js <dsf> <reps> <ab>   ab ∈ base | nowrap（nowrap 摘 .page-slide-in 的 will-change）
const { chromium } = require('C:/Users/yomen/AppData/Roaming/npm/node_modules/@playwright/mcp/node_modules/playwright-core');

const EXE_CANDIDATES = [
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
];
const EXE = EXE_CANDIDATES.find((p) => { try { return require('fs').existsSync(p); } catch (e) { return false; } });
if (!EXE) { console.error('FATAL: msedge.exe not found'); process.exit(1); }
const URL = 'file:///' + __dirname.replace(/\/g, '/') + '/frontend_copy/index.html';
const DSF = process.argv[2] || '1.5';
const REPS = parseInt(process.argv[3] || '3', 10);
const AB = process.argv[4] || 'base';

(async () => {
  const browser = await chromium.launch({
    executablePath: EXE,
    headless: true,
    args: ['--force-device-scale-factor=' + DSF, '--window-size=1280,800'],
  });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  await page.goto(URL);

  const install = () => page.evaluate(() => {
    window.__log = [];
    ['control', 'data', 'settings', 'about'].forEach((id) => {
      const el = document.getElementById('page-' + id);
      if (!el) return;
      ['animationstart', 'animationcancel', 'animationend'].forEach((t) => {
        el.addEventListener(t, (e) => {
          if (e.animationName !== 'page-slide') return;
          window.__log.push({ ev: e.type, t: performance.now() });
        });
      });
    });
  });
  await install();

  // 等待 init 完成（page-data 有子节点）；支持 reload 后重装仪器
  const waitInit = async () => {
    await page.waitForFunction(
      () => { const el = document.getElementById('page-data'); return el && el.children.length > 0; },
      null, { timeout: 5000 },
    );
    if (AB === 'nowrap') {
      await page.evaluate(() => {
        const st = document.createElement('style');
        st.textContent = '.page-slide-in { will-change: auto !important; }';
        document.head.appendChild(st);
      });
    }
    await install();
  };

  // 每次点击的采样：逐帧 (t, ct, op)，800ms 窗口
  const probe = (tab, fromTab) => page.evaluate(async ({ tab, fromTab }) => {
    const cur = document.querySelector('.maarm-tab--active');
    if (!cur || cur.dataset.tab !== fromTab) {
      document.querySelector('.maarm-tab[data-tab="' + fromTab + '"]').click();
      await new Promise((r) => setTimeout(r, 420));
    }
    const el = document.getElementById('page-' + tab);
    const t0 = performance.now();
    document.querySelector('.maarm-tab[data-tab="' + tab + '"]').click();
    const frames = [];
    await new Promise((res) => {
      const tick = () => {
        const t = performance.now() - t0;
        const a = el.getAnimations().find((x) => x.animationName);
        const ct = a ? a.currentTime : null;
        const op = getComputedStyle(el).opacity;
        frames.push([Math.round(t), ct == null ? null : Math.round(ct), op]);
        if (t < 800) requestAnimationFrame(tick); else res();
      };
      requestAnimationFrame(tick);
    });
    const events = window.__log.splice(0).map((e) => ({ ev: e.ev, rel: Math.round(e.t - t0) }))
      .filter((e) => e.rel >= -5);
    return { tab, frames, events };
  }, { tab, fromTab });

  // 摘要：帧产出停顿（rAF 间隔 >80ms）与动画时钟-墙钟脱钩量
  const summarize = (v) => {
    const fr = v.frames.filter((f) => f[1] != null);
    let maxWallGap = 0, maxSkip = 0, skipAt = null;
    for (let i = 1; i < fr.length; i++) {
      const dw = fr[i][0] - fr[i - 1][0];
      const da = fr[i][1] - fr[i - 1][1];
      if (dw > maxWallGap) maxWallGap = dw;
      if (da - dw > maxSkip) { maxSkip = da - dw; skipAt = fr[i - 1]; }
    }
    return { firstFrameT: fr.length ? fr[0][0] : null, maxWallGap, maxSkip: Math.round(maxSkip), skipAt,
             events: v.events.map((e) => e.ev + '@+' + e.rel).join(',') };
  };

  const out = { dsf: DSF, ab: AB, reps: [] };
  for (let rep = 1; rep <= REPS; rep++) {
    await page.reload();
    await waitInit();
    const repOut = { rep, pages: {} };
    for (const tab of ['data', 'settings', 'about']) {
      const first = await probe(tab, 'control');
      const second = await probe(tab, 'control');
      repOut.pages[tab] = { first: summarize(first), second: summarize(second) };
    }
    out.reps.push(repOut);
  }
  console.log(JSON.stringify(out));
  await browser.close();
  process.exit(0);
})().catch((e) => { console.error('FATAL', String(e && e.stack || e).slice(0, 500)); process.exit(1); });
