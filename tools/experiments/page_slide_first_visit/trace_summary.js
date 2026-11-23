// trace 停顿窗口摘要（实验区）：读 trace-cold-<kind>.json 的窗口与偏移，
// 从 raw 统计 GPU Flush 段、着色器缓存循环、ANGLE worker、渲染器主线程最大事件。
// 用法：node trace_summary.js data|manage
'use strict';

const path = require('node:path');
const fs = require('node:fs');

const kind = process.argv[2] === 'manage' ? 'manage' : 'data';
const dir = path.join(__dirname, '_out');
const report = JSON.parse(fs.readFileSync(path.join(dir, `trace-cold-${kind}.json`), 'utf8')).report;
const raw = JSON.parse(fs.readFileSync(path.join(dir, `trace-cold-${kind}-raw.json`), 'utf8'));

const off = report.traceOffsetMs;
const [gapStart, gapEnd] = report.windowMs;
const rel = (e) => +(e.ts / 1000 - off - gapStart).toFixed(1);
const inW = (e) => {
  const r = e.ts / 1000 - off;
  return r >= gapStart - 3 && r <= gapEnd + 3;
};

const flush = raw
  .filter((e) => e.name === 'RasterDecoderImpl::DoEndRasterCHROMIUM::Flush' && inW(e))
  .sort((a, b) => b.dur - a.dur)
  .slice(0, 5)
  .map((e) => `${(e.dur / 1000).toFixed(1)}ms@+${rel(e)}`);
const loads = raw.filter((e) => e.name === 'GrShaderCache::load' && inW(e)).length;
const stores = raw.filter((e) => e.name === 'GrShaderCache::store' && inW(e)).length;
const workers = raw.filter((e) => e.name === 'ANGLEPlatformImpl::RunWorkerTask' && inW(e));
const workerBig = workers.filter((e) => e.dur > 5000).length;

// 渲染器进程：主线程窗口内最大事件
const procs = {};
for (const e of raw) if (e.ph === 'M' && e.name === 'process_name') procs[e.pid] = e.args.name;
const renPid = Number(Object.keys(procs).find((p) => procs[p] === 'Renderer'));
const renMain = raw
  .filter((e) => e.pid === renPid && inW(e) && e.ph === 'X' && e.dur > 300)
  .sort((a, b) => b.dur - a.dur)
  .slice(0, 3)
  .map((e) => `${e.name} ${(e.dur / 1000).toFixed(1)}ms@+${rel(e)}`);

console.log(JSON.stringify({
  kind, maxGapMs: report.maxGapMs,
  gpuFlushTop: flush,
  shaderCache: { load: loads, store: stores },
  angleWorkers: { total: workers.length, over5ms: workerBig },
  rendererMainTop: renMain,
}, null, 1));
