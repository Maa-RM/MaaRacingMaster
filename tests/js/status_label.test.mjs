import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

globalThis.window = {};
const require = createRequire(import.meta.url);
const SL = require('../../apps/MaaRacingMaster.Shell/frontend/js/status-label.js');

test('有阶段：显示阶段名，运行项名不抢位', () => {
  assert.equal(SL.activeLabel({
    current_stage: '驾驶中',
    queue_run: { index: 0, total: 2, current_module: 'speedrush', current_name: '极速狂飙' },
  }), '驾驶中');
});

test('没有阶段：显示运行项名，不显示导航图节点名', () => {
  // 回归锁：debug_stage 是「此刻在匹配哪个控件」的内部细节，如
  // startup.popup.close_marketing；它出现在状态行里等于把排障信息当进度报给用户。
  const d = {
    current_stage: '',
    queue_run: { index: 0, total: 1, current_module: 'launch_game', current_name: '启动游戏' },
    debug_owner: 'nav',
    debug_label: 'startup.boot',
    debug_stage: 'startup.popup.close_marketing',
  };
  assert.equal(SL.activeLabel(d), '启动游戏');
});

test('调试流归属单独存在时不给文案：状态行落回「停止中...」', () => {
  assert.equal(SL.activeLabel({ current_stage: '', queue_run: null, debug_owner: 'nav' }), '');
  assert.equal(SL.activeLabel({ current_stage: '', debug_label: 'startup.boot' }), '');
});

test('旧载荷缺 current_name：不猜 id，交回空串', () => {
  assert.equal(SL.activeLabel({
    current_stage: '',
    queue_run: { index: 0, total: 1, current_module: 'launch_game' },
  }), '');
});

test('空载荷与 null 不抛', () => {
  assert.equal(SL.activeLabel(null), '');
  assert.equal(SL.activeLabel({}), '');
});
